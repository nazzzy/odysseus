"""routes/task_source_pollers.py — Background poller for external task sources.

One asyncio task per enabled source.  A shared dict of per-source asyncio.Locks
is passed back to the routes layer so POST /sync can honour the same lock and
return 409 instead of running two syncs concurrently for the same source.

Start-up: call start_task_pollers() from app.py _startup_event().
"""

import asyncio
import logging
import os
from typing import Dict, Optional

from src.task_sources import get_source, get_sources_for_owner, load_sources
from src.task_sync import sync_source

logger = logging.getLogger(__name__)

# Shared per-source locks — also wired into setup_external_tasks_routes().
# Key: source_id (str), value: asyncio.Lock
_sync_locks: Dict[str, asyncio.Lock] = {}

_poller_task: Optional[asyncio.Task] = None
_MIN_POLL_INTERVAL_SECONDS = 60  # floor: never poll faster than 1/minute


def _pollers_enabled() -> bool:
    raw = os.environ.get("ODYSSEUS_INPROCESS_POLLERS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def get_sync_locks() -> Dict[str, asyncio.Lock]:
    """Return the shared lock dict for injection into the routes layer."""
    return _sync_locks


async def _poll_source_forever(source_cfg: dict, owner: str) -> None:
    """Infinite loop: sync one source then sleep for its configured interval."""
    source_id = source_cfg["id"]

    if source_id not in _sync_locks:
        _sync_locks[source_id] = asyncio.Lock()
    lock = _sync_locks[source_id]

    while True:
        # Re-read config each cycle to pick up token rotations and interval changes.
        fresh_cfg = get_source(source_id)
        if fresh_cfg is None:
            logger.info("Source %s removed — stopping poller", source_id)
            break
        if not fresh_cfg.get("enabled", True):
            logger.info("Source %s disabled — stopping poller", source_id)
            break
        interval = max(
            _MIN_POLL_INTERVAL_SECONDS,
            int(fresh_cfg.get("poll_interval_minutes", 5)) * 60,
        )

        try:
            if lock.locked():
                logger.debug("Skipping poll for %s — lock held", source_id)
            else:
                async with lock:
                    logger.debug("Polling source %s (owner=%s)", source_id, owner)
                    result = await sync_source(fresh_cfg, owner)
                    if result.errors:
                        logger.warning(
                            "Sync errors for source %s: %s", source_id, result.errors
                        )
                    else:
                        logger.debug(
                            "Source %s synced: pulled=%d pushed=%d conflicts=%d deleted=%d",
                            source_id, result.pulled, result.pushed,
                            result.conflicts, result.deleted,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Unexpected error polling source %s: %s", source_id, exc)

        await asyncio.sleep(interval)


async def _poller_supervisor() -> None:
    """Load all sources and spawn a per-source poller task.

    Re-checks the source list every 60 s so newly added sources get polled
    without a restart.
    """
    active: Dict[str, asyncio.Task] = {}

    while True:
        try:
            all_sources = load_sources()
            enabled = [s for s in all_sources if s.get("enabled", True)]

            # Start pollers for new sources
            for src in enabled:
                sid = src["id"]
                if sid not in active or active[sid].done():
                    owner = src.get("owner", "")
                    logger.info("Starting poller for source %s (%s)", sid, src.get("label", ""))
                    if sid not in _sync_locks:
                        _sync_locks[sid] = asyncio.Lock()
                    active[sid] = asyncio.create_task(
                        _poll_source_forever(src, owner),
                        name=f"task-poller-{sid}",
                    )

            # Cancel pollers for sources that were removed or disabled
            enabled_ids = {s["id"] for s in enabled}
            for sid, task in list(active.items()):
                if sid not in enabled_ids and not task.done():
                    logger.info("Stopping poller for removed/disabled source %s", sid)
                    task.cancel()
                    active.pop(sid, None)
                    _sync_locks.pop(sid, None)

        except asyncio.CancelledError:
            for task in active.values():
                task.cancel()
            raise
        except Exception as exc:
            logger.warning("Poller supervisor error: %s", exc)

        await asyncio.sleep(60)


def start_task_pollers() -> Optional[asyncio.Task]:
    """Launch the supervisor task.  Call once from _startup_event() in app.py.

    Returns the supervisor Task so the caller can keep a strong reference
    (prevents GC before the loop exits).  Returns None if pollers are disabled.
    """
    global _poller_task

    if not _pollers_enabled():
        logger.info(
            "Task source pollers disabled (ODYSSEUS_INPROCESS_POLLERS=0)"
        )
        return None

    if _poller_task is not None and not _poller_task.done():
        logger.debug("Task poller supervisor already running")
        return _poller_task

    try:
        loop = asyncio.get_running_loop()
        _poller_task = loop.create_task(
            _poller_supervisor(), name="task-source-poller-supervisor"
        )
        logger.info("Started task source poller supervisor")
        return _poller_task
    except RuntimeError:
        logger.warning("No running event loop — task pollers will not start")
        return None
