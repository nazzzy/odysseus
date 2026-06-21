"""
task_sources.py — Task source registry: presets, CRUD, and secret management.

A "task source" is a configured connection to an external task provider
(e.g. Todoist).  Sources are stored in TASK_SOURCES_FILE (JSON list).
The `api_token` field is encrypted at rest using secret_storage.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR, TASK_SOURCES_FILE
from src.secret_storage import decrypt, encrypt, is_encrypted

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Preset definitions — one entry per supported provider
# ---------------------------------------------------------------------------

TASK_SOURCE_PRESETS: List[Dict[str, Any]] = [
    {
        "type": "todoist",
        "label": "Todoist",
        "base_url": "https://api.todoist.com/api/v1",
        "auth_type": "bearer",
        "poll_interval_minutes": 5,
        "fields": [
            {"name": "api_token", "label": "API Token", "secret": True},
            {"name": "project_id", "label": "Project ID (optional)", "secret": False},
        ],
    },
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _encrypt_secrets(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    safe = []
    for src in sources:
        copy = dict(src)
        token = copy.get("api_token", "")
        if token:
            copy["api_token"] = encrypt(str(token))
        safe.append(copy)
    return safe


def _decrypt_secrets(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for src in sources:
        copy = dict(src)
        token = copy.get("api_token", "")
        if token:
            copy["api_token"] = decrypt(str(token))
        result.append(copy)
    return result


def _has_plaintext_token(sources: List[Dict[str, Any]]) -> bool:
    return any(
        bool(s.get("api_token")) and not is_encrypted(str(s.get("api_token")))
        for s in sources
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_sources() -> List[Dict[str, Any]]:
    """Return all task sources with secrets decrypted for runtime use."""
    if not os.path.exists(TASK_SOURCES_FILE):
        return []
    try:
        with open(TASK_SOURCES_FILE, "r", encoding="utf-8") as f:
            sources = json.load(f)
        if not isinstance(sources, list):
            log.error("task_sources.json: expected a list")
            return []
        sources = [s for s in sources if isinstance(s, dict)]
        if _has_plaintext_token(sources):
            save_sources(sources)
        return _decrypt_secrets(sources)
    except (json.JSONDecodeError, IOError) as exc:
        log.error("Failed to load task sources: %s", exc)
        return []


def save_sources(sources: List[Dict[str, Any]]) -> None:
    """Persist task sources with api_token encrypted at rest."""
    _ensure_data_dir()
    atomic_write_json(TASK_SOURCES_FILE, _encrypt_secrets(sources), indent=2)


def get_source(source_id: str) -> Optional[Dict[str, Any]]:
    for s in load_sources():
        if s.get("id") == source_id:
            return s
    return None


def get_sources_for_owner(owner: str) -> List[Dict[str, Any]]:
    return [s for s in load_sources() if s.get("owner") == owner]


def add_source(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new task source and persist it.  Returns the created source."""
    sources = load_sources()
    source = {
        "id": data.get("id") or uuid.uuid4().hex,
        "owner": data.get("owner", ""),
        "type": data["type"],
        "label": data.get("label", data["type"].capitalize()),
        "base_url": data.get("base_url", ""),
        "api_token": data.get("api_token", ""),
        "project_id": data.get("project_id", ""),
        "poll_interval_minutes": int(data.get("poll_interval_minutes", 5)),
        "enabled": bool(data.get("enabled", True)),
    }
    sources.append(source)
    save_sources(sources)
    return source


def update_source(source_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply a partial update to an existing source.  Returns updated source or None."""
    sources = load_sources()
    for i, s in enumerate(sources):
        if s.get("id") == source_id:
            allowed = {"label", "api_token", "project_id", "poll_interval_minutes", "enabled"}
            for k, v in updates.items():
                if k in allowed:
                    s[k] = v
            sources[i] = s
            save_sources(sources)
            return s
    return None


def delete_source(source_id: str) -> bool:
    """Remove a task source.  Returns True if found and deleted."""
    sources = load_sources()
    filtered = [s for s in sources if s.get("id") != source_id]
    if len(filtered) == len(sources):
        return False
    save_sources(filtered)
    return True


def mask_source_secret(source: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy safe for API responses (token masked)."""
    copy = dict(source)
    token = copy.get("api_token", "")
    if token:
        copy["api_token"] = f"{str(token)[:4]}****"
    return copy
