"""Tests for do_manage_external_tasks in src/tool_implementations.py."""
import json
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import ExternalTask


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_SOURCES = [{"id": "src1", "owner": "alice"}]


@pytest.fixture(autouse=True)
def patch_session(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool_test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", Session)
    # Also patch the reference inside tool_implementations import path
    import core.database as _cdb_ref
    monkeypatch.setattr(_cdb_ref, "SessionLocal", Session)
    # Patch source lookup so tests don't need a real task_sources.json
    import src.task_sources as _ts
    monkeypatch.setattr(_ts, "get_sources_for_owner", lambda owner: [
        s for s in _FAKE_SOURCES if s.get("owner") == owner
    ])
    return Session


def _args(**kwargs):
    return json.dumps(kwargs)


def _add_task(monkeypatch, title="Test task", owner="alice", source_id="src1",
               sync_pending=None):
    # Add directly via the patched session
    import core.database as _cd
    db = _cd.SessionLocal()
    t = ExternalTask(
        id=uuid.uuid4().hex,
        source_id=source_id,
        external_id=uuid.uuid4().hex,
        owner=owner,
        title=title,
        sync_pending=sync_pending,
    )
    db.add(t)
    db.commit()
    tid = t.id
    db.close()
    return tid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_list(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    _add_task(monkeypatch, title="Alpha task", owner="alice")
    _add_task(monkeypatch, title="Beta task", owner="bob")

    result = await do_manage_external_tasks(_args(action="list"), owner="alice")
    assert result.get("exit_code") == 0
    text = result.get("results", "")
    assert "Alpha task" in text
    assert "Beta task" not in text


@pytest.mark.asyncio
async def test_tool_create_sets_pending(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks

    result = await do_manage_external_tasks(
        _args(action="create", source_id="src1", title="New task"),
        owner="alice",
    )
    assert result.get("exit_code") == 0
    assert "will sync" in result.get("response", "").lower()

    # Verify DB state
    import core.database as _cd
    db = _cd.SessionLocal()
    rows = db.query(ExternalTask).filter_by(owner="alice", sync_pending="create").all()
    db.close()
    assert len(rows) == 1
    assert rows[0].title == "New task"


@pytest.mark.asyncio
async def test_tool_complete(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    tid = _add_task(monkeypatch, title="Finish report", owner="alice")

    result = await do_manage_external_tasks(
        _args(action="complete", id=tid[:8]),
        owner="alice",
    )
    assert result.get("exit_code") == 0

    import core.database as _cd
    db = _cd.SessionLocal()
    row = db.query(ExternalTask).filter_by(id=tid).first()
    db.close()
    assert row.status == "completed"
    assert row.sync_pending == "complete"


@pytest.mark.asyncio
async def test_tool_sync(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    from src.task_sync import SyncResult

    async def _fake_sync(source, owner):
        return SyncResult(source_id=source["id"], pulled=5, pushed=1)

    monkeypatch.setattr("src.task_sync.sync_source", _fake_sync)
    monkeypatch.setattr(
        "src.task_sources.get_sources_for_owner",
        lambda owner: [{"id": "s1", "type": "todoist", "base_url": "x",
                        "api_token": "tok", "project_id": ""}],
    )

    result = await do_manage_external_tasks(_args(action="sync"), owner="alice")
    assert result.get("exit_code") == 0
    assert "pulled=5" in result.get("response", "")


@pytest.mark.asyncio
async def test_tool_invalid_json(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks("{not valid json", owner="alice")
    assert result.get("exit_code") == 1
    assert "Invalid JSON" in result.get("error", "")


@pytest.mark.asyncio
async def test_tool_unknown_action(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks(_args(action="wipe_everything"), owner="alice")
    assert result.get("exit_code") == 1
    assert "Unknown action" in result.get("error", "")


@pytest.mark.asyncio
async def test_tool_list_empty(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks(_args(action="list"), owner="alice")
    assert result.get("exit_code") == 0
    assert "No external tasks" in result.get("response", "")


@pytest.mark.asyncio
async def test_tool_create_missing_source_id(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks(
        _args(action="create", title="Task without source"),
        owner="alice",
    )
    assert result.get("exit_code") == 1
    assert "source_id" in result.get("error", "")


@pytest.mark.asyncio
async def test_tool_create_source_not_owned(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    # src_not_mine belongs to bob, not alice
    import src.task_sources as _ts
    monkeypatch.setattr(_ts, "get_sources_for_owner", lambda owner: [])
    result = await do_manage_external_tasks(
        _args(action="create", source_id="src_not_mine", title="Sneaky task"),
        owner="alice",
    )
    assert result.get("exit_code") == 1
    assert "not owned" in result.get("error", "").lower() or "not found" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_tool_create_missing_title(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks(
        _args(action="create", source_id="src1"),
        owner="alice",
    )
    assert result.get("exit_code") == 1
    assert "title" in result.get("error", "")


@pytest.mark.asyncio
async def test_tool_complete_id_too_short(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks(
        _args(action="complete", id="abc"),  # < 6 chars
        owner="alice",
    )
    assert result.get("exit_code") == 1
    assert "6 characters" in result.get("error", "")


@pytest.mark.asyncio
async def test_tool_complete_not_found(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks(
        _args(action="complete", id="notexist"),
        owner="alice",
    )
    assert result.get("exit_code") == 1
    assert "not found" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_tool_complete_preserves_create_pending(monkeypatch):
    """Completing a task with sync_pending='create' must keep it as 'create'."""
    from src.tool_implementations import do_manage_external_tasks
    tid = _add_task(monkeypatch, title="Unpushed task", owner="alice", sync_pending="create")

    result = await do_manage_external_tasks(
        _args(action="complete", id=tid[:8]),
        owner="alice",
    )
    assert result.get("exit_code") == 0

    import core.database as _cd
    db = _cd.SessionLocal()
    row = db.query(_cd.ExternalTask).filter_by(id=tid).first()
    db.close()
    assert row.status == "completed"
    assert row.sync_pending == "create"  # not overwritten to "complete"


@pytest.mark.asyncio
async def test_tool_update(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    tid = _add_task(monkeypatch, title="Old title", owner="alice")

    result = await do_manage_external_tasks(
        _args(action="update", id=tid[:8], title="New title", due_date="2026-12-31"),
        owner="alice",
    )
    assert result.get("exit_code") == 0
    assert "Updated" in result.get("response", "")

    import core.database as _cd
    db = _cd.SessionLocal()
    row = db.query(_cd.ExternalTask).filter_by(id=tid).first()
    db.close()
    assert row.title == "New title"
    assert row.due_date == "2026-12-31"
    assert row.sync_pending == "update"


@pytest.mark.asyncio
async def test_tool_update_id_too_short(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks(
        _args(action="update", id="xx", title="New"),
        owner="alice",
    )
    assert result.get("exit_code") == 1
    assert "6 characters" in result.get("error", "")


@pytest.mark.asyncio
async def test_tool_update_not_found(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    result = await do_manage_external_tasks(
        _args(action="update", id="notexist", title="New"),
        owner="alice",
    )
    assert result.get("exit_code") == 1
    assert "not found" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_tool_update_preserves_create_pending(monkeypatch):
    """Updating a task with sync_pending='create' must keep it as 'create'."""
    from src.tool_implementations import do_manage_external_tasks
    tid = _add_task(monkeypatch, title="Draft", owner="alice", sync_pending="create")

    result = await do_manage_external_tasks(
        _args(action="update", id=tid[:8], title="Renamed draft"),
        owner="alice",
    )
    assert result.get("exit_code") == 0

    import core.database as _cd
    db = _cd.SessionLocal()
    row = db.query(_cd.ExternalTask).filter_by(id=tid).first()
    db.close()
    assert row.title == "Renamed draft"
    assert row.sync_pending == "create"  # preserved


@pytest.mark.asyncio
async def test_tool_sync_specific_source(monkeypatch):
    """sync with source_id filter must only sync that source."""
    from src.tool_implementations import do_manage_external_tasks
    from src.task_sync import SyncResult

    synced_ids = []

    async def _fake_sync(source, owner):
        synced_ids.append(source["id"])
        return SyncResult(source_id=source["id"], pulled=2)

    monkeypatch.setattr("src.task_sync.sync_source", _fake_sync)
    import src.task_sources as _ts
    monkeypatch.setattr(_ts, "get_sources_for_owner", lambda owner: [
        {"id": "s1", "type": "todoist", "base_url": "x", "api_token": "tok", "project_id": ""},
        {"id": "s2", "type": "todoist", "base_url": "x", "api_token": "tok", "project_id": ""},
    ])

    result = await do_manage_external_tasks(
        _args(action="sync", source_id="s1"),
        owner="alice",
    )
    assert result.get("exit_code") == 0
    assert synced_ids == ["s1"]


@pytest.mark.asyncio
async def test_tool_sync_no_sources(monkeypatch):
    from src.tool_implementations import do_manage_external_tasks
    import src.task_sources as _ts
    monkeypatch.setattr(_ts, "get_sources_for_owner", lambda owner: [])

    result = await do_manage_external_tasks(_args(action="sync"), owner="alice")
    assert result.get("exit_code") == 0
    assert "No task sources" in result.get("response", "")


@pytest.mark.asyncio
async def test_tool_list_with_filters(monkeypatch):
    """list with source_id and status filters must narrow results."""
    from src.tool_implementations import do_manage_external_tasks
    import core.database as _cd

    _add_task(monkeypatch, title="Open in src1", owner="alice", source_id="src1")
    db = _cd.SessionLocal()
    import uuid as _uuid
    done = _cd.ExternalTask(
        id=_uuid.uuid4().hex, source_id="src1", external_id=_uuid.uuid4().hex,
        owner="alice", title="Done in src1", status="completed",
    )
    db.add(done)
    db.commit()
    db.close()

    result = await do_manage_external_tasks(
        _args(action="list", source_id="src1", status="completed"),
        owner="alice",
    )
    assert result.get("exit_code") == 0
    text = result.get("results", "")
    assert "Done in src1" in text
    assert "Open in src1" not in text
