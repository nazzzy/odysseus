"""
task_sync.py — Pull/push sync engine for external task sources.

One entry point: sync_source(source_cfg, owner) → SyncResult.

Pull strategy:
  - remote wins when sync_pending is None (overwrite local)
  - if sync_pending is set AND remote changed → sync_conflict=True,
    local changes are preserved, push runs next cycle

Push strategy:
  - local wins: push sync_pending actions then clear the flag

Safety rules (from design doc):
  - All HTTP calls: timeout=30s
  - _mark_deleted_if_missing() runs ONLY when _fetch_all_pages() succeeds fully
  - Delete guard filter: external_id IS NOT NULL, deleted_at IS NULL, sync_pending != 'create'
  - Conflict detection: remote.updated_at > local.synced_at
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import httpx
from sqlalchemy import or_

from core.database import ExternalTask, SessionLocal, utcnow_naive
from src.task_sources import TASK_SOURCE_PRESETS

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 30  # seconds — all outbound calls


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    source_id: str
    pulled: int = 0
    pushed: int = 0
    conflicts: int = 0
    deleted: int = 0
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider-specific mapping (Todoist v1)
# ---------------------------------------------------------------------------

def _get_preset(source_type: str) -> Dict[str, Any]:
    for p in TASK_SOURCE_PRESETS:
        if p["type"] == source_type:
            return p
    raise ValueError(f"Unknown source type: {source_type!r}")


def _map_remote_to_local(rt: Dict[str, Any], source_type: str) -> Dict[str, Any]:
    """Normalise a Todoist API v1 task object to our internal schema."""
    if source_type == "todoist":
        due = rt.get("due") or {}
        return {
            "external_id": str(rt["id"]),
            "title": rt.get("content", ""),
            "body": rt.get("description", ""),
            "status": "completed" if rt.get("is_completed") else "open",
            "due_date": due.get("date") if isinstance(due, dict) else None,
            "priority": rt.get("priority"),
            "labels": rt.get("labels", []),
            "remote_data": rt,
            "remote_updated_at": rt.get("updated_at"),
        }
    raise ValueError(f"No mapper for source type: {source_type!r}")


def _build_push_payload(task: ExternalTask, source_type: str) -> Dict[str, Any]:
    """Build the request body for a push action."""
    if source_type == "todoist":
        return {
            "content": task.title,
            "description": task.body or "",
            "due_string": task.due_date or "",
            "priority": task.priority or 1,
            "labels": task.labels or [],
        }
    raise ValueError(f"No push payload builder for: {source_type!r}")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _fetch_all_pages(
    base_url: str,
    api_token: str,
    source_type: str,
    project_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch all pages from the remote tasks endpoint using cursor pagination.

    Raises on HTTP or network error — caller must NOT soft-delete on exception.
    """
    tasks: List[Dict[str, Any]] = []
    params: Dict[str, Any] = {}
    if project_id:
        params["project_id"] = project_id

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        cursor: Optional[str] = None
        while True:
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(
                f"{base_url}/tasks",
                headers={"Authorization": f"Bearer {api_token}"},
                params=params,
            )
            resp.raise_for_status()
            body = resp.json()
            page = body.get("results", [])
            tasks.extend(page)
            cursor = body.get("next_cursor") or None
            if not cursor:
                break

    return tasks


async def _push_task(
    task: ExternalTask,
    base_url: str,
    api_token: str,
    source_type: str,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """Execute the pending push action for a single task.

    Returns True on success, False on recoverable error (pending stays).
    404 on delete/complete is treated as success (already gone on remote).
    Pass an existing client to reuse its connection pool across multiple calls.
    """
    headers = {"Authorization": f"Bearer {api_token}"}
    action = task.sync_pending

    async def _exec(c: httpx.AsyncClient) -> bool:
        try:
            if action == "create":
                payload = _build_push_payload(task, source_type)
                resp = await c.post(f"{base_url}/tasks", headers=headers, json=payload)
                resp.raise_for_status()
                new_id = resp.json().get("id")
                if new_id:
                    task.external_id = str(new_id)

            elif action == "update":
                if not task.external_id:
                    log.warning("update pending but external_id is None for task %s", task.id)
                    return False
                payload = _build_push_payload(task, source_type)
                resp = await c.post(
                    f"{base_url}/tasks/{task.external_id}",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()

            elif action == "complete":
                if not task.external_id:
                    return False
                resp = await c.post(
                    f"{base_url}/tasks/{task.external_id}/close",
                    headers=headers,
                )
                if resp.status_code == 404:
                    pass  # already gone — treat as success
                else:
                    resp.raise_for_status()

            elif action == "delete":
                if not task.external_id:
                    return False
                resp = await c.delete(
                    f"{base_url}/tasks/{task.external_id}",
                    headers=headers,
                )
                if resp.status_code == 404:
                    pass  # already deleted remotely
                else:
                    resp.raise_for_status()

            else:
                log.warning("Unknown sync_pending value %r for task %s", action, task.id)
                return False

        except httpx.HTTPStatusError as exc:
            log.warning("Push failed for task %s (action=%s): %s", task.id, action, exc)
            return False
        except httpx.RequestError as exc:
            log.warning("Network error pushing task %s: %s", task.id, exc)
            return False

        return True

    if client is not None:
        return await _exec(client)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        return await _exec(c)


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def _parse_remote_ts(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _upsert_task(
    mapped: Dict[str, Any],
    owner: str,
    source_id: str,
    db,
) -> bool:
    """Insert or update a local task row from remote data.

    Returns True if a conflict was detected.
    """
    conflict = False
    remote_updated = _parse_remote_ts(mapped.get("remote_updated_at"))
    external_id = mapped["external_id"]

    existing = (
        db.query(ExternalTask)
        .filter_by(source_id=source_id, external_id=external_id, owner=owner)
        .first()
    )

    if existing is None:
        task = ExternalTask(
            source_id=source_id,
            external_id=external_id,
            owner=owner,
            title=mapped["title"],
            body=mapped.get("body"),
            status=mapped.get("status", "open"),
            due_date=mapped.get("due_date"),
            priority=mapped.get("priority"),
            labels=mapped.get("labels", []),
            remote_data=mapped.get("remote_data", {}),
            synced_at=utcnow_naive(),
        )
        db.add(task)
    else:
        if existing.sync_pending is not None:
            # Detect conflict: remote changed after we last synced
            if remote_updated and existing.synced_at and remote_updated > existing.synced_at:
                existing.sync_conflict = True
                conflict = True
                log.info("Conflict on task %s (source=%s)", external_id, source_id)
            # Don't overwrite local changes
        else:
            # No pending local change — remote wins
            existing.title = mapped["title"]
            existing.body = mapped.get("body")
            existing.status = mapped.get("status", "open")
            existing.due_date = mapped.get("due_date")
            existing.priority = mapped.get("priority")
            existing.labels = mapped.get("labels", [])
            existing.remote_data = mapped.get("remote_data", {})
            existing.deleted_at = None
            existing.synced_at = utcnow_naive()

    return conflict


def _mark_deleted_if_missing(
    remote_ids: Set[str],
    source_id: str,
    owner: str,
    db,
) -> int:
    """Soft-delete local tasks that are no longer present in the full remote list.

    Skips tasks with external_id=None (sync_pending='create') and already-deleted rows.
    Only called when _fetch_all_pages() succeeded completely.
    """
    candidates = (
        db.query(ExternalTask)
        .filter(
            ExternalTask.source_id == source_id,
            ExternalTask.owner == owner,
            ExternalTask.external_id.isnot(None),
            ExternalTask.deleted_at.is_(None),
            # NULL != 'create' is NULL in SQL, so include NULL rows explicitly
            or_(ExternalTask.sync_pending.is_(None), ExternalTask.sync_pending != "create"),
        )
        .all()
    )
    count = 0
    now = utcnow_naive()
    for task in candidates:
        if task.external_id not in remote_ids:
            task.deleted_at = now
            task.status = "completed"
            count += 1
    return count


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def sync_source(source_cfg: Dict[str, Any], owner: str) -> SyncResult:
    """Pull then push for a single task source configuration.

    source_cfg must include: id, type, base_url, api_token, project_id (optional).
    """
    result = SyncResult(source_id=source_cfg["id"])
    source_type = source_cfg["type"]
    base_url = source_cfg.get("base_url", "").rstrip("/")
    api_token = source_cfg.get("api_token", "")
    project_id = source_cfg.get("project_id") or None

    # --- 1. Fetch all remote tasks (raises on error) ---
    try:
        remote_tasks = await _fetch_all_pages(base_url, api_token, source_type, project_id)
    except Exception as exc:
        msg = f"Fetch failed for source {source_cfg['id']}: {exc}"
        log.warning(msg)
        result.errors.append(msg)
        return result

    remote_ids: Set[str] = {str(t["id"]) for t in remote_tasks}

    db = SessionLocal()
    try:
        # --- 2. Upsert pulled tasks ---
        for rt in remote_tasks:
            try:
                mapped = _map_remote_to_local(rt, source_type)
                conflict = _upsert_task(mapped, owner, source_cfg["id"], db)
                result.pulled += 1
                if conflict:
                    result.conflicts += 1
            except Exception as exc:
                log.warning("Upsert error for remote task %s: %s", rt.get("id"), exc)
                result.errors.append(str(exc))

        # --- 3. Soft-delete tasks missing from remote (only after full fetch) ---
        result.deleted = _mark_deleted_if_missing(remote_ids, source_cfg["id"], owner, db)

        db.commit()

        # --- 4. Push pending local changes ---
        pending = (
            db.query(ExternalTask)
            .filter(
                ExternalTask.source_id == source_cfg["id"],
                ExternalTask.owner == owner,
                ExternalTask.sync_pending.isnot(None),
                ExternalTask.sync_conflict.is_(False),
            )
            .all()
        )
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as push_client:
            for task in pending:
                ok = await _push_task(task, base_url, api_token, source_type, client=push_client)
                if ok:
                    task.sync_pending = None
                    task.sync_conflict = False
                    task.synced_at = utcnow_naive()
                    db.commit()  # commit per-task so external_id survives a later crash
                    result.pushed += 1
                else:
                    result.errors.append(f"Push failed for task {task.id}")

    except Exception as exc:
        db.rollback()
        log.error("sync_source error for %s: %s", source_cfg["id"], exc)
        result.errors.append(str(exc))
    finally:
        db.close()

    return result
