"""routes/external_tasks_routes.py — External Tasks Hub API."""

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from core.database import ExternalTask, SessionLocal, utcnow_naive
from src.auth_helpers import require_user
from src.task_sources import (
    TASK_SOURCE_PRESETS,
    add_source,
    delete_source,
    get_sources_for_owner,
    mask_source_secret,
    update_source,
)
from src.task_sync import SyncResult, sync_source

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class TaskCreate(BaseModel):
    source_id: str
    title: str
    body: Optional[str] = None
    due_date: Optional[str] = None
    priority: Optional[int] = None
    labels: Optional[List[str]] = None


class TaskPatch(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    due_date: Optional[str] = None
    priority: Optional[int] = None
    labels: Optional[List[str]] = None
    status: Optional[str] = None


class ResolveConflict(BaseModel):
    keep: str  # "local" | "remote"


class SourceCreate(BaseModel):
    type: str
    label: Optional[str] = None
    base_url: Optional[str] = None
    api_token: str
    project_id: Optional[str] = None
    poll_interval_minutes: int = Field(5, ge=1, le=1440)


class SourcePatch(BaseModel):
    label: Optional[str] = None
    api_token: Optional[str] = None
    project_id: Optional[str] = None
    poll_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    enabled: Optional[bool] = None


class ManualSyncRequest(BaseModel):
    source_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------

def _task_to_dict(t: ExternalTask) -> Dict[str, Any]:
    return {
        "id": t.id,
        "source_id": t.source_id,
        "external_id": t.external_id,
        "owner": t.owner,
        "title": t.title,
        "body": t.body,
        "status": t.status,
        "due_date": t.due_date,
        "priority": t.priority,
        "labels": t.labels or [],
        "sync_pending": t.sync_pending,
        "sync_conflict": t.sync_conflict,
        "deleted_at": t.deleted_at.isoformat() if t.deleted_at else None,
        "synced_at": t.synced_at.isoformat() if t.synced_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _sync_result_to_dict(r: SyncResult) -> Dict[str, Any]:
    return {
        "source_id": r.source_id,
        "pulled": r.pulled,
        "pushed": r.pushed,
        "conflicts": r.conflicts,
        "deleted": r.deleted,
        "errors": r.errors,
    }


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def setup_external_tasks_routes(sync_locks: Optional[Dict[str, Any]] = None) -> APIRouter:
    """Return the external-tasks router.

    sync_locks is the shared asyncio.Lock dict from task_source_pollers — passed in
    so manual POST /sync can check and honour the same per-source lock.
    """
    router = APIRouter(prefix="/api/external-tasks", tags=["external-tasks"])

    _locks: Dict[str, Any] = sync_locks if sync_locks is not None else {}

    def _owner(request: Request) -> Optional[str]:
        return require_user(request) or None

    # -----------------------------------------------------------------------
    # Tasks CRUD
    # -----------------------------------------------------------------------

    @router.get("", summary="List tasks")
    def list_tasks(
        request: Request,
        source_id: Optional[str] = None,
        status: Optional[str] = None,
        include_deleted: bool = False,
    ):
        owner = _owner(request)
        db = SessionLocal()
        try:
            q = db.query(ExternalTask)
            if owner is not None:
                q = q.filter(ExternalTask.owner == owner)
            if source_id:
                q = q.filter(ExternalTask.source_id == source_id)
            if status:
                q = q.filter(ExternalTask.status == status)
            if not include_deleted:
                q = q.filter(ExternalTask.deleted_at.is_(None))
            tasks = q.order_by(ExternalTask.created_at.desc()).all()
            return {"tasks": [_task_to_dict(t) for t in tasks]}
        finally:
            db.close()

    @router.get("/presets", summary="List source presets")
    def list_presets():
        return {"presets": TASK_SOURCE_PRESETS}

    @router.get("/sources", summary="List task sources")
    def list_sources(request: Request):
        owner = _owner(request)
        sources = get_sources_for_owner(owner or "")
        return {"sources": [mask_source_secret(s) for s in sources]}

    @router.post("/sources", summary="Add a task source", status_code=201)
    async def create_source(
        request: Request,
        body: SourceCreate,
        background_tasks: BackgroundTasks,
    ):
        owner = _owner(request)

        preset = next((p for p in TASK_SOURCE_PRESETS if p["type"] == body.type), None)
        if preset is None:
            raise HTTPException(status_code=400, detail="Unsupported source type")

        source = add_source({
            "owner": owner or "",
            "type": body.type,
            "label": body.label or body.type.capitalize(),
            "base_url": preset["base_url"],
            "api_token": body.api_token,
            "project_id": body.project_id or "",
            "poll_interval_minutes": body.poll_interval_minutes,
        })

        # Trigger an immediate first sync in the background
        background_tasks.add_task(_run_sync_bg_locked, source, owner or "")
        return {"source": mask_source_secret(source)}

    @router.patch("/sources/{source_id}", summary="Update a task source")
    def patch_source(request: Request, source_id: str, body: SourcePatch):
        owner = _owner(request)
        _assert_source_owner(source_id, owner)
        updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
        updated = update_source(source_id, updates)
        if updated is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return {"source": mask_source_secret(updated)}

    @router.delete("/sources/{source_id}", summary="Remove a task source", status_code=204)
    def remove_source(request: Request, source_id: str):
        owner = _owner(request)
        _assert_source_owner(source_id, owner)
        if not delete_source(source_id):
            raise HTTPException(status_code=404, detail="Source not found")

    @router.post("/sync", summary="Trigger manual sync")
    async def manual_sync(request: Request, body: ManualSyncRequest):
        owner = _owner(request)
        sources = get_sources_for_owner(owner or "")
        if body.source_id:
            sources = [s for s in sources if s["id"] == body.source_id]
            if not sources:
                raise HTTPException(status_code=404, detail="Source not found")

        results = []
        for source in sources:
            sid = source["id"]
            lock = _locks.get(sid)
            if lock is None:
                # New source not yet seen by the poller — create lock on demand
                _locks[sid] = lock = asyncio.Lock()
            if lock.locked():
                raise HTTPException(
                    status_code=409,
                    detail=f"Sync already running for source {sid}",
                )
            async with lock:
                result = await sync_source(source, owner or "")
            results.append(_sync_result_to_dict(result))
        return {"results": results}

    @router.get("/{task_id}", summary="Get one task")
    def get_task(request: Request, task_id: str):
        owner = _owner(request)
        t = _get_task_or_404(task_id, owner)
        return _task_to_dict(t)

    @router.post("", summary="Create a local task", status_code=201)
    def create_task(request: Request, body: TaskCreate):
        owner = _owner(request)
        _assert_source_owner(body.source_id, owner)
        db = SessionLocal()
        try:
            task = ExternalTask(
                id=uuid.uuid4().hex,
                source_id=body.source_id,
                owner=owner or "",
                title=body.title,
                body=body.body,
                due_date=body.due_date,
                priority=body.priority,
                labels=body.labels or [],
                sync_pending="create",
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            return _task_to_dict(task)
        finally:
            db.close()

    @router.patch("/{task_id}", summary="Update a task")
    def patch_task(request: Request, task_id: str, body: TaskPatch):
        owner = _owner(request)
        db = SessionLocal()
        try:
            t = db.query(ExternalTask).filter_by(id=task_id).first()
            if t is None or (owner and t.owner != owner):
                raise HTTPException(status_code=404, detail="Task not found")
            allowed = {"title", "body", "due_date", "priority", "labels", "status"}
            for k, v in body.model_dump(exclude_none=True).items():
                if k in allowed:
                    setattr(t, k, v)
            if t.sync_pending != "create":
                t.sync_pending = "update"
            db.commit()
            db.refresh(t)
            return _task_to_dict(t)
        finally:
            db.close()

    @router.post("/{task_id}/complete", summary="Mark task complete")
    def complete_task(request: Request, task_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            t = db.query(ExternalTask).filter_by(id=task_id).first()
            if t is None or (owner and t.owner != owner):
                raise HTTPException(status_code=404, detail="Task not found")
            t.status = "completed"
            if t.sync_pending != "create":
                t.sync_pending = "complete"
            db.commit()
            db.refresh(t)
            return _task_to_dict(t)
        finally:
            db.close()

    @router.post("/{task_id}/resolve-conflict", summary="Resolve a sync conflict")
    async def resolve_conflict(request: Request, task_id: str, body: ResolveConflict):
        if body.keep not in ("local", "remote"):
            raise HTTPException(status_code=400, detail="keep must be 'local' or 'remote'")
        owner = _owner(request)
        db = SessionLocal()
        try:
            t = db.query(ExternalTask).filter_by(id=task_id).first()
            if t is None or (owner and t.owner != owner):
                raise HTTPException(status_code=404, detail="Task not found")
            if not t.sync_conflict:
                raise HTTPException(status_code=409, detail="No conflict on this task")

            if body.keep == "local":
                # Push local version — clear conflict flag, leave sync_pending
                t.sync_conflict = False
                db.commit()
                # Trigger push immediately, serialised behind the per-source lock
                source = next(
                    (s for s in get_sources_for_owner(owner or "") if s["id"] == t.source_id),
                    None,
                )
                if source:
                    sid = source["id"]
                    lock = _locks.get(sid)
                    if lock is None:
                        _locks[sid] = lock = asyncio.Lock()
                    async with lock:
                        await sync_source(source, owner or "")
            else:
                # Pull remote — overwrite local with remote_data
                rd = t.remote_data or {}
                raw_title = rd.get("content", t.title)
                t.title = raw_title if isinstance(raw_title, str) else t.title
                t.body = rd.get("description", t.body)
                due = rd.get("due") or {}
                t.due_date = due.get("date") if isinstance(due, dict) else t.due_date
                raw_priority = rd.get("priority", t.priority)
                t.priority = raw_priority if isinstance(raw_priority, (int, type(None))) else t.priority
                raw_labels = rd.get("labels", t.labels)
                t.labels = raw_labels if isinstance(raw_labels, list) else t.labels
                t.status = "completed" if rd.get("is_completed") else "open"
                t.sync_pending = None
                t.sync_conflict = False
                t.synced_at = utcnow_naive()
                db.commit()

            db.refresh(t)
            return _task_to_dict(t)
        finally:
            db.close()

    @router.delete("/{task_id}", summary="Soft-delete a task", status_code=204)
    def delete_task(request: Request, task_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            t = db.query(ExternalTask).filter_by(id=task_id).first()
            if t is None or (owner and t.owner != owner):
                raise HTTPException(status_code=404, detail="Task not found")
            if t.sync_pending == "create":
                # Never pushed to remote — hard-delete to avoid phantom create on next sync
                db.delete(t)
            else:
                t.deleted_at = utcnow_naive()
                t.sync_pending = "delete"
            db.commit()
        finally:
            db.close()

    async def _run_sync_bg_locked(source: Dict[str, Any], owner: str) -> None:
        """Background sync that serialises behind the per-source lock."""
        sid = source.get("id", "")
        lock = _locks.get(sid)
        if lock is None:
            _locks[sid] = lock = asyncio.Lock()
        try:
            async with lock:
                await sync_source(source, owner)
        except Exception as exc:
            logger.warning("Background sync failed for source %s: %s", sid, exc)

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_task_or_404(task_id: str, owner: Optional[str]) -> ExternalTask:
    db = SessionLocal()
    try:
        t = db.query(ExternalTask).filter_by(id=task_id).first()
        if t is None or (owner and t.owner != owner):
            raise HTTPException(status_code=404, detail="Task not found")
        db.expunge(t)
        return t
    finally:
        db.close()


def _assert_source_owner(source_id: str, owner: Optional[str]) -> None:
    sources = get_sources_for_owner(owner or "")
    if not any(s["id"] == source_id for s in sources):
        raise HTTPException(status_code=403, detail="Not your source")

