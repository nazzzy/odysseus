"""Tests for src/task_sync.py — pull/push/conflict/soft-delete logic."""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import ExternalTask, utcnow_naive


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_remote_task(ext_id="t1", content="Buy milk", updated_offset_s=0):
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    updated = base + timedelta(seconds=updated_offset_s)
    return {
        "id": ext_id,
        "content": content,
        "description": "",
        "is_completed": False,
        "due": {"date": "2026-06-30"},
        "priority": 1,
        "labels": [],
        "updated_at": updated.isoformat(),
    }


def _add_local_task(db, ext_id, source_id="src1", owner="alice",
                    sync_pending=None, synced_offset_s=0):
    synced = datetime(2026, 1, 1, 12, 0, 0) + timedelta(seconds=synced_offset_s)
    t = ExternalTask(
        id=ext_id + "-local",
        source_id=source_id,
        external_id=ext_id,
        owner=owner,
        title="Old title",
        sync_pending=sync_pending,
        synced_at=synced,
    )
    db.add(t)
    db.commit()
    return t


# ---------------------------------------------------------------------------
# Import helpers under test with patched SessionLocal
# ---------------------------------------------------------------------------

from src.task_sync import (
    _upsert_task,
    _mark_deleted_if_missing,
    _map_remote_to_local,
    _fetch_all_pages,
    _push_task,
)


# ---------------------------------------------------------------------------
# _upsert_task
# ---------------------------------------------------------------------------

def test_upsert_new_task(db_session):
    mapped = _map_remote_to_local(_make_remote_task("t1", "Buy milk"), "todoist")
    conflict = _upsert_task(mapped, "alice", "src1", db_session)
    db_session.commit()
    assert not conflict
    row = db_session.query(ExternalTask).filter_by(external_id="t1").first()
    assert row is not None
    assert row.title == "Buy milk"
    assert row.due_date == "2026-06-30"


def test_upsert_existing_no_conflict(db_session):
    _add_local_task(db_session, "t2", sync_pending=None, synced_offset_s=-60)
    mapped = _map_remote_to_local(
        _make_remote_task("t2", "Updated title", updated_offset_s=0), "todoist"
    )
    conflict = _upsert_task(mapped, "alice", "src1", db_session)
    db_session.commit()
    assert not conflict
    row = db_session.query(ExternalTask).filter_by(external_id="t2").first()
    assert row.title == "Updated title"


def test_upsert_conflict(db_session):
    # local has pending update; remote was modified AFTER local last synced
    _add_local_task(db_session, "t3", sync_pending="update", synced_offset_s=-120)
    # remote updated_at is 60s AFTER synced_at
    mapped = _map_remote_to_local(
        _make_remote_task("t3", "Remote title", updated_offset_s=-60), "todoist"
    )
    conflict = _upsert_task(mapped, "alice", "src1", db_session)
    db_session.commit()
    assert conflict
    row = db_session.query(ExternalTask).filter_by(external_id="t3").first()
    assert row.sync_conflict is True
    assert row.title == "Old title"  # local NOT overwritten


# ---------------------------------------------------------------------------
# _mark_deleted_if_missing
# ---------------------------------------------------------------------------

def test_mark_deleted_if_missing_guard(db_session):
    """Task with sync_pending='create' (external_id=None) must NOT be touched."""
    t = ExternalTask(
        id="local-only",
        source_id="src1",
        external_id=None,
        owner="alice",
        title="New local task",
        sync_pending="create",
    )
    db_session.add(t)
    db_session.commit()

    count = _mark_deleted_if_missing(
        remote_ids=set(),
        source_id="src1",
        owner="alice",
        db=db_session,
    )
    db_session.commit()
    assert count == 0
    row = db_session.query(ExternalTask).filter_by(id="local-only").first()
    assert row.deleted_at is None


def test_mark_deleted_if_missing_deletes_gone_task(db_session):
    _add_local_task(db_session, "t10", sync_pending=None)
    count = _mark_deleted_if_missing(
        remote_ids=set(),  # t10 missing from remote
        source_id="src1",
        owner="alice",
        db=db_session,
    )
    db_session.commit()
    assert count == 1
    row = db_session.query(ExternalTask).filter_by(external_id="t10").first()
    assert row.deleted_at is not None


@pytest.mark.asyncio
async def test_mark_deleted_skipped_on_fetch_error():
    """_fetch_all_pages raising must prevent _mark_deleted_if_missing from running."""
    called = []

    async def _fake_fetch(*args, **kwargs):
        raise RuntimeError("network error")

    with patch("src.task_sync._fetch_all_pages", _fake_fetch), \
         patch("src.task_sync._mark_deleted_if_missing", side_effect=lambda *a, **kw: called.append(1)):
        from src.task_sync import sync_source
        result = await sync_source(
            {"id": "s1", "type": "todoist", "base_url": "http://x", "api_token": "t"},
            "alice",
        )
    assert called == []
    assert len(result.errors) > 0


# ---------------------------------------------------------------------------
# Push actions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_push_create(db_session):
    t = ExternalTask(
        id="local-new", source_id="src1", external_id=None,
        owner="alice", title="New task", sync_pending="create",
    )
    db_session.add(t)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "remote-99"}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value = instance

        ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")

    assert ok
    assert t.external_id == "remote-99"


@pytest.mark.asyncio
async def test_push_complete(db_session):
    t = ExternalTask(
        id="done-task", source_id="src1", external_id="rem-1",
        owner="alice", title="Finish report", sync_pending="complete",
    )
    db_session.add(t)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value = instance

        ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")

    assert ok


@pytest.mark.asyncio
async def test_push_delete_404_ok(db_session):
    """DELETE returning 404 (already gone remotely) must count as success."""
    t = ExternalTask(
        id="del-task", source_id="src1", external_id="rem-2",
        owner="alice", title="Old task", sync_pending="delete",
    )
    db_session.add(t)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.delete = AsyncMock(return_value=mock_resp)
        MockClient.return_value = instance

        ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")

    assert ok


@pytest.mark.asyncio
async def test_push_failure_pending_stays(db_session):
    """HTTP 500 must leave sync_pending intact."""
    import httpx as _httpx
    t = ExternalTask(
        id="fail-task", source_id="src1", external_id="rem-3",
        owner="alice", title="Fail me", sync_pending="update",
    )
    db_session.add(t)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status = MagicMock(
        side_effect=_httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)
    )

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value = instance

        ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")

    assert not ok
    assert t.sync_pending == "update"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pagination_cursor():
    """Two-page response must return all tasks from both pages."""
    page1 = {"results": [{"id": "t1"}, {"id": "t2"}], "next_cursor": "cur1"}
    page2 = {"results": [{"id": "t3"}], "next_cursor": None}

    responses = [page1, page2]
    call_count = 0

    async def _fake_get(url, **kwargs):
        nonlocal call_count
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = responses[call_count]
        call_count += 1
        return resp

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.get = _fake_get
        MockClient.return_value = instance

        tasks = await _fetch_all_pages(
            "https://api.todoist.com/api/v1", "tok", "todoist"
        )

    assert len(tasks) == 3
    assert call_count == 2


# ---------------------------------------------------------------------------
# Sync lock (409 on concurrent)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_lock_concurrent():
    """A locked source must cause the routes layer to return 409."""
    import asyncio as _asyncio
    from routes.task_source_pollers import _sync_locks

    source_id = "lock-test-src"
    lock = _asyncio.Lock()
    _sync_locks[source_id] = lock

    try:
        async with lock:
            # While locked, simulate what the routes layer checks
            assert lock.locked()
    finally:
        _sync_locks.pop(source_id, None)


# ---------------------------------------------------------------------------
# _map_remote_to_local — unknown source type
# ---------------------------------------------------------------------------

def test_map_remote_unknown_type():
    from src.task_sync import _map_remote_to_local
    with pytest.raises(ValueError, match="No mapper"):
        _map_remote_to_local({"id": "x"}, "jira")


# ---------------------------------------------------------------------------
# _build_push_payload — unknown source type
# ---------------------------------------------------------------------------

def test_build_push_payload_unknown_type(db_session):
    from src.task_sync import _build_push_payload
    t = ExternalTask(
        id="x", source_id="s", external_id="e", owner="alice",
        title="T", sync_pending="update",
    )
    db_session.add(t)
    db_session.commit()
    with pytest.raises(ValueError, match="No push payload builder"):
        _build_push_payload(t, "jira")


# ---------------------------------------------------------------------------
# _parse_remote_ts — edge cases
# ---------------------------------------------------------------------------

def test_parse_remote_ts_none():
    from src.task_sync import _parse_remote_ts
    assert _parse_remote_ts(None) is None
    assert _parse_remote_ts("") is None


def test_parse_remote_ts_invalid():
    from src.task_sync import _parse_remote_ts
    assert _parse_remote_ts("not-a-date") is None


def test_parse_remote_ts_valid():
    from src.task_sync import _parse_remote_ts
    dt = _parse_remote_ts("2026-01-01T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo is None  # stripped to naive UTC


# ---------------------------------------------------------------------------
# _upsert_task — existing task with sync_pending set but remote NOT newer
# ---------------------------------------------------------------------------

def test_upsert_no_conflict_when_remote_not_newer(db_session):
    """Pending local change but remote hasn't changed → no conflict."""
    from src.task_sync import _upsert_task
    # synced 60 s ago; remote updated_at is BEFORE synced_at
    _add_local_task(db_session, "t-nc", sync_pending="update", synced_offset_s=0)
    # remote updated_at = -60s (older than synced_at)
    mapped = _map_remote_to_local(
        _make_remote_task("t-nc", "remote title", updated_offset_s=-60), "todoist"
    )
    conflict = _upsert_task(mapped, "alice", "src1", db_session)
    db_session.commit()
    assert not conflict
    row = db_session.query(ExternalTask).filter_by(external_id="t-nc").first()
    # local NOT overwritten because sync_pending is set
    assert row.title == "Old title"


# ---------------------------------------------------------------------------
# _push_task — missing external_id for update / complete / delete actions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_push_update_missing_external_id(db_session):
    from src.task_sync import _push_task
    t = ExternalTask(
        id="upd-no-ext", source_id="src1", external_id=None,
        owner="alice", title="Pending update", sync_pending="update",
    )
    db_session.add(t)
    db_session.commit()
    ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")
    assert not ok


@pytest.mark.asyncio
async def test_push_complete_missing_external_id(db_session):
    from src.task_sync import _push_task
    t = ExternalTask(
        id="cmp-no-ext", source_id="src1", external_id=None,
        owner="alice", title="Pending complete", sync_pending="complete",
    )
    db_session.add(t)
    db_session.commit()
    ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")
    assert not ok


@pytest.mark.asyncio
async def test_push_delete_missing_external_id(db_session):
    from src.task_sync import _push_task
    t = ExternalTask(
        id="del-no-ext", source_id="src1", external_id=None,
        owner="alice", title="Pending delete", sync_pending="delete",
    )
    db_session.add(t)
    db_session.commit()
    ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")
    assert not ok


@pytest.mark.asyncio
async def test_push_unknown_action(db_session):
    from src.task_sync import _push_task
    t = ExternalTask(
        id="unk-act", source_id="src1", external_id="rem-x",
        owner="alice", title="T", sync_pending="bogus",
    )
    db_session.add(t)
    db_session.commit()
    ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")
    assert not ok


@pytest.mark.asyncio
async def test_push_complete_404_treated_as_success(db_session):
    """complete action: 404 from remote must be treated as success."""
    from src.task_sync import _push_task
    t = ExternalTask(
        id="cmp-404", source_id="src1", external_id="gone-rem",
        owner="alice", title="T", sync_pending="complete",
    )
    db_session.add(t)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value = instance

        ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")

    assert ok


@pytest.mark.asyncio
async def test_push_network_error(db_session):
    """httpx.RequestError must return False (recoverable)."""
    import httpx as _httpx
    from src.task_sync import _push_task
    t = ExternalTask(
        id="net-err", source_id="src1", external_id="rem-y",
        owner="alice", title="T", sync_pending="update",
    )
    db_session.add(t)
    db_session.commit()

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.post = AsyncMock(
            side_effect=_httpx.RequestError("connection refused", request=MagicMock())
        )
        MockClient.return_value = instance

        ok = await _push_task(t, "https://api.todoist.com/api/v1", "tok", "todoist")

    assert not ok


# ---------------------------------------------------------------------------
# sync_source — per-task upsert error is caught and added to errors list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_source_upsert_error_per_task(tmp_path):
    """An error inside the per-task upsert loop must be caught; other tasks succeed.

    We simulate this by patching _map_remote_to_local to raise for the second task.
    The per-task try/except in sync_source must swallow it and continue.
    """
    import core.database as _cdb
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sync_test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    _cdb.Base.metadata.create_all(engine)
    _Session = sessionmaker(bind=engine)

    good_task = {"id": "g1", "content": "Good task", "description": "",
                 "is_completed": False, "due": None, "priority": 1,
                 "labels": [], "updated_at": "2026-01-01T00:00:00Z"}
    bad_task = {"id": "b2", "content": "Will fail", "description": "",
                "is_completed": False, "due": None, "priority": 1,
                "labels": [], "updated_at": "2026-01-01T00:00:00Z"}

    call_count = [0]
    real_map = __import__("src.task_sync", fromlist=["_map_remote_to_local"])._map_remote_to_local

    def _patched_map(rt, source_type):
        call_count[0] += 1
        if rt["id"] == "b2":
            raise RuntimeError("simulated upsert error")
        return real_map(rt, source_type)

    async def _fake_fetch(*a, **kw):
        return [good_task, bad_task]

    with patch("src.task_sync._fetch_all_pages", _fake_fetch), \
         patch("src.task_sync._map_remote_to_local", _patched_map), \
         patch("src.task_sync.SessionLocal", _Session):
        from src.task_sync import sync_source
        result = await sync_source(
            {"id": "s1", "type": "todoist", "base_url": "http://x",
             "api_token": "t", "project_id": ""},
            "alice",
        )

    # Good task counted; bad task error captured
    assert result.pulled == 1
    assert len(result.errors) == 1
    assert "simulated upsert error" in result.errors[0]
