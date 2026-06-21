"""Tests for routes/external_tasks_routes.py — owner scoping, CRUD, sync."""
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import ExternalTask, utcnow_naive
import routes.external_tasks_routes as er

_PEER = ("203.0.113.7", 54321)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Identity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            state = scope.setdefault("state", {})
            user = headers.get(b"x-test-user")
            if user:
                state["current_user"] = user.decode()
        await self.app(scope, receive, send)


def _build_app(tmp_path, sync_locks=None, owned_sources=None):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Routes import SessionLocal at module level, so patch the module-level name
    er.SessionLocal = Session

    # Patch source-ownership lookup so tests can declare which sources exist
    # without needing a real task_sources.json on disk.
    _sources = owned_sources or [{"id": "src1", "owner": "alice"}]
    er.get_sources_for_owner = lambda owner: [s for s in _sources if s.get("owner") == owner]

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=False)
    app.include_router(er.setup_external_tasks_routes(sync_locks=sync_locks or {}))
    return _Identity(app), Session


def _client(app):
    transport = httpx.ASGITransport(app=app, client=_PEER)
    return httpx.AsyncClient(transport=transport, base_url="http://test.local")


def _add_task(Session, owner="alice", source_id="src1", title="Test task",
               sync_pending=None, sync_conflict=False):
    db = Session()
    t = ExternalTask(
        id=uuid.uuid4().hex,
        source_id=source_id,
        external_id=uuid.uuid4().hex,
        owner=owner,
        title=title,
        sync_pending=sync_pending,
        sync_conflict=sync_conflict,
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
async def test_get_tasks_owner_scope(tmp_path):
    app, Session = _build_app(tmp_path)
    _add_task(Session, owner="alice", title="Alice task")
    _add_task(Session, owner="bob", title="Bob task")

    async with _client(app) as c:
        r = await c.get("/api/external-tasks", headers={"x-test-user": "alice"})
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert all(t["owner"] == "alice" for t in tasks)
    assert not any(t["title"] == "Bob task" for t in tasks)


@pytest.mark.asyncio
async def test_create_task_sets_sync_pending(tmp_path):
    app, Session = _build_app(tmp_path)
    async with _client(app) as c:
        r = await c.post(
            "/api/external-tasks",
            json={"source_id": "src1", "title": "New task"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 201
    data = r.json()
    assert data["sync_pending"] == "create"
    assert data["owner"] == "alice"


@pytest.mark.asyncio
async def test_patch_whitelist(tmp_path):
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice")
    async with _client(app) as c:
        r = await c.patch(
            f"/api/external-tasks/{tid}",
            json={"title": "Updated", "owner": "hacker"},  # owner not in whitelist
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Updated"
    assert data["owner"] == "alice"  # not overwritten


@pytest.mark.asyncio
async def test_complete_task(tmp_path):
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice")
    async with _client(app) as c:
        r = await c.post(
            f"/api/external-tasks/{tid}/complete",
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "completed"
    assert data["sync_pending"] == "complete"


@pytest.mark.asyncio
async def test_resolve_conflict_local(tmp_path, monkeypatch):
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice", sync_pending="update", sync_conflict=True)

    # Patch sync_source so we don't hit network
    async def _noop_sync(source, owner):
        from src.task_sync import SyncResult
        return SyncResult(source_id="src1")

    monkeypatch.setattr("routes.external_tasks_routes.sync_source", _noop_sync)
    monkeypatch.setattr("src.task_sources.get_sources_for_owner", lambda owner: [])

    async with _client(app) as c:
        r = await c.post(
            f"/api/external-tasks/{tid}/resolve-conflict",
            json={"keep": "local"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 200
    assert not r.json()["sync_conflict"]


@pytest.mark.asyncio
async def test_resolve_conflict_remote(tmp_path):
    app, Session = _build_app(tmp_path)
    # Task with remote_data set
    db = Session()
    t = ExternalTask(
        id=uuid.uuid4().hex,
        source_id="src1",
        external_id="rem-x",
        owner="alice",
        title="Local version",
        sync_pending="update",
        sync_conflict=True,
        remote_data={"content": "Remote version", "description": "", "is_completed": False,
                     "priority": 1, "labels": [], "due": None},
    )
    db.add(t)
    db.commit()
    tid = t.id
    db.close()

    async with _client(app) as c:
        r = await c.post(
            f"/api/external-tasks/{tid}/resolve-conflict",
            json={"keep": "remote"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Remote version"
    assert not data["sync_conflict"]
    assert data["sync_pending"] is None


@pytest.mark.asyncio
async def test_delete_soft(tmp_path):
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice")
    async with _client(app) as c:
        r = await c.delete(
            f"/api/external-tasks/{tid}",
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 204

    db = Session()
    row = db.query(ExternalTask).filter_by(id=tid).first()
    db.close()
    assert row.deleted_at is not None
    assert row.sync_pending == "delete"


@pytest.mark.asyncio
async def test_source_owner_scope(tmp_path, monkeypatch):
    """User alice cannot delete a source owned by bob."""
    app, Session = _build_app(tmp_path)
    # Bob's source
    monkeypatch.setattr(
        "routes.external_tasks_routes.get_sources_for_owner",
        lambda owner: [] if owner == "alice" else [{"id": "bob-src"}],
    )
    async with _client(app) as c:
        r = await c.delete(
            "/api/external-tasks/sources/bob-src",
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_manual_sync_trigger(tmp_path, monkeypatch):
    app, Session = _build_app(tmp_path)
    from src.task_sync import SyncResult

    async def _fake_sync(source, owner):
        return SyncResult(source_id=source["id"], pulled=3)

    monkeypatch.setattr("routes.external_tasks_routes.sync_source", _fake_sync)
    monkeypatch.setattr(
        "routes.external_tasks_routes.get_sources_for_owner",
        lambda owner: [{"id": "s1", "type": "todoist", "base_url": "x",
                        "api_token": "t", "project_id": ""}],
    )

    async with _client(app) as c:
        r = await c.post(
            "/api/external-tasks/sync",
            json={},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["pulled"] == 3


@pytest.mark.asyncio
async def test_add_source_triggers_immediate_sync(tmp_path, monkeypatch):
    """POST /sources must trigger a background sync (we verify the bg task is queued)."""
    app, Session = _build_app(tmp_path)
    synced = []

    async def _fake_sync(source, owner):
        synced.append(source["type"])
        from src.task_sync import SyncResult
        return SyncResult(source_id=source["id"])

    monkeypatch.setattr("routes.external_tasks_routes.sync_source", _fake_sync)
    monkeypatch.setattr("src.task_sources.add_source", lambda data: {**data, "id": "new-s"})

    async with _client(app) as c:
        r = await c.post(
            "/api/external-tasks/sources",
            json={"type": "todoist", "api_token": "tok123"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 201
    assert synced == ["todoist"], "background sync must fire for the new source"


# ---------------------------------------------------------------------------
# GET /presets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_presets(tmp_path):
    app, _ = _build_app(tmp_path)
    async with _client(app) as c:
        r = await c.get("/api/external-tasks/presets")
    assert r.status_code == 200
    body = r.json()
    assert "presets" in body
    assert any(p["type"] == "todoist" for p in body["presets"])


# ---------------------------------------------------------------------------
# GET /?include_deleted and ?status filters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_tasks_include_deleted(tmp_path):
    app, Session = _build_app(tmp_path)
    # One live task, one soft-deleted task
    db = Session()
    live = ExternalTask(
        id=uuid.uuid4().hex, source_id="src1", external_id=uuid.uuid4().hex,
        owner="alice", title="Live task",
    )
    gone = ExternalTask(
        id=uuid.uuid4().hex, source_id="src1", external_id=uuid.uuid4().hex,
        owner="alice", title="Deleted task", deleted_at=utcnow_naive(),
    )
    db.add_all([live, gone])
    db.commit()
    db.close()

    async with _client(app) as c:
        # Default: deleted excluded
        r1 = await c.get("/api/external-tasks", headers={"x-test-user": "alice"})
        assert r1.status_code == 200
        titles = [t["title"] for t in r1.json()["tasks"]]
        assert "Deleted task" not in titles

        # include_deleted=true: both returned
        r2 = await c.get(
            "/api/external-tasks?include_deleted=true",
            headers={"x-test-user": "alice"},
        )
        assert r2.status_code == 200
        titles2 = [t["title"] for t in r2.json()["tasks"]]
        assert "Deleted task" in titles2
        assert "Live task" in titles2


@pytest.mark.asyncio
async def test_list_tasks_status_filter(tmp_path):
    app, Session = _build_app(tmp_path)
    db = Session()
    db.add(ExternalTask(
        id=uuid.uuid4().hex, source_id="src1", external_id=uuid.uuid4().hex,
        owner="alice", title="Open task", status="open",
    ))
    db.add(ExternalTask(
        id=uuid.uuid4().hex, source_id="src1", external_id=uuid.uuid4().hex,
        owner="alice", title="Done task", status="completed",
    ))
    db.commit()
    db.close()

    async with _client(app) as c:
        r = await c.get(
            "/api/external-tasks?status=completed",
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert all(t["status"] == "completed" for t in tasks)
    assert any(t["title"] == "Done task" for t in tasks)
    assert not any(t["title"] == "Open task" for t in tasks)


# ---------------------------------------------------------------------------
# GET /{task_id} — not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_task_not_found(tmp_path):
    app, _ = _build_app(tmp_path)
    async with _client(app) as c:
        r = await c.get(
            "/api/external-tasks/doesnotexist",
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{task_id} — not found; complete with sync_pending=="create"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_task_not_found(tmp_path):
    app, _ = _build_app(tmp_path)
    async with _client(app) as c:
        r = await c.patch(
            "/api/external-tasks/ghostid",
            json={"title": "New"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_task_preserves_create_pending(tmp_path):
    """PATCH on a task that has sync_pending='create' must keep it as 'create'."""
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice", sync_pending="create")
    async with _client(app) as c:
        r = await c.patch(
            f"/api/external-tasks/{tid}",
            json={"title": "Renamed before push"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 200
    assert r.json()["sync_pending"] == "create"


# ---------------------------------------------------------------------------
# POST /{task_id}/complete — sync_pending=="create" stays "create"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_task_keeps_create_pending(tmp_path):
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice", sync_pending="create")
    async with _client(app) as c:
        r = await c.post(
            f"/api/external-tasks/{tid}/complete",
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "completed"
    # Must NOT be overwritten to "complete" — it was never pushed
    assert data["sync_pending"] == "create"


# ---------------------------------------------------------------------------
# DELETE /{task_id} — hard-delete when sync_pending=="create"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_hard_when_never_pushed(tmp_path):
    """Tasks with sync_pending='create' (never pushed) must be hard-deleted."""
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice", sync_pending="create")
    async with _client(app) as c:
        r = await c.delete(
            f"/api/external-tasks/{tid}",
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 204

    db = Session()
    row = db.query(ExternalTask).filter_by(id=tid).first()
    db.close()
    assert row is None  # hard-deleted, not soft-deleted


# ---------------------------------------------------------------------------
# POST /{task_id}/resolve-conflict — invalid keep value; no conflict; source absent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_conflict_invalid_keep(tmp_path):
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice", sync_conflict=True, sync_pending="update")
    async with _client(app) as c:
        r = await c.post(
            f"/api/external-tasks/{tid}/resolve-conflict",
            json={"keep": "neither"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_resolve_conflict_no_conflict_on_task(tmp_path):
    """Trying to resolve a conflict that doesn't exist must 409."""
    app, Session = _build_app(tmp_path)
    tid = _add_task(Session, owner="alice", sync_conflict=False)
    async with _client(app) as c:
        r = await c.post(
            f"/api/external-tasks/{tid}/resolve-conflict",
            json={"keep": "local"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# POST /sync — source_id not found → 404; lock held → 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_sync_source_not_found(tmp_path, monkeypatch):
    app, _ = _build_app(tmp_path)
    monkeypatch.setattr(
        "routes.external_tasks_routes.get_sources_for_owner",
        lambda owner: [],
    )
    async with _client(app) as c:
        r = await c.post(
            "/api/external-tasks/sync",
            json={"source_id": "ghost"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_manual_sync_returns_409_when_locked(tmp_path, monkeypatch):
    import asyncio
    lock = asyncio.Lock()
    await lock.acquire()  # pre-lock it
    locks = {"s1": lock}

    app, _ = _build_app(tmp_path, sync_locks=locks)
    monkeypatch.setattr(
        "routes.external_tasks_routes.get_sources_for_owner",
        lambda owner: [{"id": "s1", "type": "todoist", "base_url": "x",
                        "api_token": "t", "project_id": ""}],
    )
    try:
        async with _client(app) as c:
            r = await c.post(
                "/api/external-tasks/sync",
                json={},
                headers={"x-test-user": "alice"},
            )
        assert r.status_code == 409
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# PATCH /sources/{id} — not found (update_source returns None)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_source_not_found(tmp_path, monkeypatch):
    app, _ = _build_app(tmp_path)
    # Source is "owned" (passes owner check) but update_source returns None
    monkeypatch.setattr(
        "routes.external_tasks_routes.get_sources_for_owner",
        lambda owner: [{"id": "src1", "owner": "alice"}],
    )
    monkeypatch.setattr("routes.external_tasks_routes.update_source", lambda sid, u: None)
    async with _client(app) as c:
        r = await c.patch(
            "/api/external-tasks/sources/src1",
            json={"label": "New label"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /sources/{id} — not found (delete_source returns False)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_source_not_found(tmp_path, monkeypatch):
    app, _ = _build_app(tmp_path)
    monkeypatch.setattr(
        "routes.external_tasks_routes.get_sources_for_owner",
        lambda owner: [{"id": "src1", "owner": "alice"}],
    )
    monkeypatch.setattr("routes.external_tasks_routes.delete_source", lambda sid: False)
    async with _client(app) as c:
        r = await c.delete(
            "/api/external-tasks/sources/src1",
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /sources — unknown source type is rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_source_unknown_type_no_base_url(tmp_path, monkeypatch):
    app, _ = _build_app(tmp_path)
    async with _client(app) as c:
        r = await c.post(
            "/api/external-tasks/sources",
            json={"type": "mylegacytool", "api_token": "tok"},
            headers={"x-test-user": "alice"},
        )
    assert r.status_code == 400
    assert "Unsupported source type" in r.json()["detail"]
