"""Tests for src/task_sources.py and routes/task_source_pollers.py."""
import asyncio
import json
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# task_sources.py
# ---------------------------------------------------------------------------

class TestLoadSources:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.task_sources.TASK_SOURCES_FILE",
                            str(tmp_path / "no_such_file.json"))
        from src.task_sources import load_sources
        assert load_sources() == []

    def test_non_list_json_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "sources.json"
        f.write_text('{"not": "a list"}')
        monkeypatch.setattr("src.task_sources.TASK_SOURCES_FILE", str(f))
        from src.task_sources import load_sources
        assert load_sources() == []

    def test_invalid_json_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "sources.json"
        f.write_text("{bad json")
        monkeypatch.setattr("src.task_sources.TASK_SOURCES_FILE", str(f))
        from src.task_sources import load_sources
        assert load_sources() == []

    def test_non_dict_items_filtered(self, tmp_path, monkeypatch):
        f = tmp_path / "sources.json"
        # mix of dict and non-dict
        monkeypatch.setattr("src.task_sources.TASK_SOURCES_FILE", str(f))
        monkeypatch.setattr("src.task_sources.DATA_DIR", str(tmp_path))
        from src.task_sources import save_sources, load_sources
        from src.task_sources import _encrypt_secrets
        # Write raw JSON with non-dict item
        f.write_text(json.dumps([{"id": "s1", "api_token": ""}, "not-a-dict"]))
        result = load_sources()
        assert all(isinstance(s, dict) for s in result)
        assert len(result) == 1


class TestSaveAndCRUD:
    @pytest.fixture(autouse=True)
    def patch_paths(self, tmp_path, monkeypatch):
        src_file = str(tmp_path / "sources.json")
        monkeypatch.setattr("src.task_sources.TASK_SOURCES_FILE", src_file)
        monkeypatch.setattr("src.task_sources.DATA_DIR", str(tmp_path))
        # Patch secret_storage to be identity (no encryption needed in unit tests)
        monkeypatch.setattr("src.task_sources.encrypt", lambda s: "enc:" + s)
        monkeypatch.setattr("src.task_sources.decrypt", lambda s: s[4:] if s.startswith("enc:") else s)
        monkeypatch.setattr("src.task_sources.is_encrypted", lambda s: s.startswith("enc:"))

    def test_add_then_get(self):
        from src.task_sources import add_source, get_source
        src = add_source({
            "type": "todoist",
            "label": "My Todoist",
            "base_url": "https://api.todoist.com/api/v1",
            "api_token": "mytoken",
            "owner": "alice",
        })
        fetched = get_source(src["id"])
        assert fetched is not None
        assert fetched["label"] == "My Todoist"

    def test_add_with_explicit_id(self):
        from src.task_sources import add_source, get_source
        src = add_source({
            "id": "explicit-id",
            "type": "todoist",
            "api_token": "tok",
            "owner": "alice",
        })
        assert src["id"] == "explicit-id"
        assert get_source("explicit-id") is not None

    def test_get_source_not_found(self):
        from src.task_sources import get_source
        assert get_source("ghost") is None

    def test_update_source(self):
        from src.task_sources import add_source, update_source
        src = add_source({"type": "todoist", "api_token": "tok", "owner": "alice"})
        updated = update_source(src["id"], {"label": "Changed"})
        assert updated is not None
        assert updated["label"] == "Changed"

    def test_update_source_not_found(self):
        from src.task_sources import update_source
        assert update_source("ghost", {"label": "X"}) is None

    def test_update_source_ignores_non_allowed_fields(self):
        from src.task_sources import add_source, update_source, get_source
        src = add_source({"type": "todoist", "api_token": "tok", "owner": "alice"})
        update_source(src["id"], {"owner": "hacker", "label": "Legit"})
        fetched = get_source(src["id"])
        assert fetched["owner"] == "alice"
        assert fetched["label"] == "Legit"

    def test_delete_source(self):
        from src.task_sources import add_source, delete_source, get_source
        src = add_source({"type": "todoist", "api_token": "tok", "owner": "alice"})
        assert delete_source(src["id"]) is True
        assert get_source(src["id"]) is None

    def test_delete_source_not_found(self):
        from src.task_sources import delete_source
        assert delete_source("nobody") is False

    def test_get_sources_for_owner(self):
        from src.task_sources import add_source, get_sources_for_owner
        add_source({"type": "todoist", "api_token": "tok", "owner": "alice"})
        add_source({"type": "todoist", "api_token": "tok", "owner": "bob"})
        alice_sources = get_sources_for_owner("alice")
        assert all(s["owner"] == "alice" for s in alice_sources)
        assert len(alice_sources) == 1

    def test_mask_source_secret_with_token(self):
        from src.task_sources import mask_source_secret
        src = {"id": "x", "api_token": "supersecrettoken", "type": "todoist"}
        masked = mask_source_secret(src)
        assert masked["api_token"].endswith("****")
        assert "supersecrettoken" not in masked["api_token"]

    def test_mask_source_secret_empty_token(self):
        from src.task_sources import mask_source_secret
        src = {"id": "x", "api_token": "", "type": "todoist"}
        masked = mask_source_secret(src)
        assert masked["api_token"] == ""

    def test_plaintext_token_migration(self, tmp_path, monkeypatch):
        """load_sources() must encrypt plaintext tokens on load (migration path)."""
        src_file = str(tmp_path / "sources_plain.json")
        # Write sources with plaintext (not "enc:") token
        data = [{"id": "s1", "api_token": "plaintext", "type": "todoist", "owner": "alice"}]
        with open(src_file, "w") as f:
            json.dump(data, f)
        monkeypatch.setattr("src.task_sources.TASK_SOURCES_FILE", src_file)
        monkeypatch.setattr("src.task_sources.DATA_DIR", str(tmp_path))
        monkeypatch.setattr("src.task_sources.encrypt", lambda s: "enc:" + s)
        monkeypatch.setattr("src.task_sources.decrypt", lambda s: s[4:] if s.startswith("enc:") else s)
        monkeypatch.setattr("src.task_sources.is_encrypted", lambda s: s.startswith("enc:"))

        from importlib import import_module
        ts = import_module("src.task_sources")
        result = ts.load_sources()
        # The runtime decrypted value should be "plaintext"
        assert result[0]["api_token"] == "plaintext"
        # Verify file on disk was re-saved with encryption
        with open(src_file) as f:
            saved = json.load(f)
        assert saved[0]["api_token"].startswith("enc:")


# ---------------------------------------------------------------------------
# task_source_pollers.py — _pollers_enabled()
# ---------------------------------------------------------------------------

class TestPollersEnabled:
    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_disabled_values(self, val, monkeypatch):
        monkeypatch.setenv("ODYSSEUS_INPROCESS_POLLERS", val)
        # Reload to pick up env
        import importlib
        import routes.task_source_pollers as p
        importlib.reload(p)
        assert p._pollers_enabled() is False

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ODYSSEUS_INPROCESS_POLLERS", raising=False)
        import importlib
        import routes.task_source_pollers as p
        importlib.reload(p)
        assert p._pollers_enabled() is True


class TestStartTaskPollers:
    def test_disabled_returns_none(self, monkeypatch):
        import routes.task_source_pollers as p
        monkeypatch.setattr(p, "_pollers_enabled", lambda: False)
        # Reset global state
        p._poller_task = None
        result = p.start_task_pollers()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_task_when_enabled(self, monkeypatch):
        import routes.task_source_pollers as p
        monkeypatch.setattr(p, "_pollers_enabled", lambda: True)
        p._poller_task = None

        async def _noop_supervisor():
            await asyncio.sleep(9999)

        monkeypatch.setattr(p, "_poller_supervisor", _noop_supervisor)
        task = p.start_task_pollers()
        try:
            assert task is not None
            assert not task.done()
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        p._poller_task = None

    @pytest.mark.asyncio
    async def test_returns_existing_if_already_running(self, monkeypatch):
        import routes.task_source_pollers as p
        monkeypatch.setattr(p, "_pollers_enabled", lambda: True)

        async def _noop():
            await asyncio.sleep(9999)

        existing = asyncio.create_task(_noop(), name="existing")
        p._poller_task = existing
        try:
            result = p.start_task_pollers()
            assert result is existing
        finally:
            existing.cancel()
            try:
                await existing
            except (asyncio.CancelledError, Exception):
                pass
        p._poller_task = None

    def test_no_event_loop_returns_none(self, monkeypatch):
        import routes.task_source_pollers as p
        monkeypatch.setattr(p, "_pollers_enabled", lambda: True)
        p._poller_task = None

        def _raise(*a, **kw):
            raise RuntimeError("no running event loop")

        monkeypatch.setattr(asyncio, "create_task", _raise)
        result = p.start_task_pollers()
        assert result is None
        # Restore
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# _poll_source_forever — source removed / disabled stop poller
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_source_forever_source_removed(monkeypatch):
    """Poller must break when get_source() returns None."""
    import routes.task_source_pollers as p

    monkeypatch.setattr(p, "get_source", lambda sid: None)
    # Should exit without error
    await asyncio.wait_for(
        p._poll_source_forever({"id": "gone"}, "alice"),
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_poll_source_forever_source_disabled(monkeypatch):
    """Poller must break when source is disabled."""
    import routes.task_source_pollers as p

    monkeypatch.setattr(
        p, "get_source",
        lambda sid: {"id": sid, "enabled": False, "poll_interval_minutes": 1},
    )
    await asyncio.wait_for(
        p._poll_source_forever({"id": "dis"}, "alice"),
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_poll_source_forever_exception_swallowed(monkeypatch):
    """Unexpected exception in poll cycle must be swallowed (logged) and loop continues."""
    import routes.task_source_pollers as p

    call_count = [0]

    def _get_source(sid):
        call_count[0] += 1
        if call_count[0] > 1:
            return None  # exit after first cycle
        return {"id": sid, "enabled": True, "poll_interval_minutes": 1}

    async def _bad_sync(cfg, owner):
        raise RuntimeError("boom")

    monkeypatch.setattr(p, "get_source", _get_source)
    monkeypatch.setattr(p, "sync_source", _bad_sync)

    # Patch sleep to be instant so the loop cycles
    async def _fast_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    await asyncio.wait_for(
        p._poll_source_forever({"id": "s1"}, "alice"),
        timeout=2.0,
    )
    assert call_count[0] >= 2
