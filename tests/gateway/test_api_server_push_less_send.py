from __future__ import annotations

import types
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.platforms.api_server import APIServerAdapter


def _adapter() -> APIServerAdapter:
    adapter = APIServerAdapter.__new__(APIServerAdapter)
    adapter.platform = Platform.API_SERVER  # feeds .name, as __init__ would
    return adapter


@pytest.mark.asyncio
async def test_send_with_retry_short_circuits_on_push_less_adapter(caplog, monkeypatch):
    """Regression: no WARN/ERROR ladder from _send_with_retry on api_server.

    BasePlatformAdapter._send_with_retry treats the adapter's contractual
    failure as a formatting error: it logs "[Api_Server] Send failed … —
    trying plain-text fallback" (WARNING), attempts a second send, then logs
    "Fallback send also failed" (ERROR).  On a managed box every mission turn
    delivers its final answer through this path, so errors.log — the cockpit's
    log source — grew one WARNING+ERROR pair per turn (observed live
    2026-08-09: 7 pairs for 5 turns).  The adapter's override must attempt
    send() exactly ONCE, return the contractual failure unchanged, and log
    nothing above DEBUG.  Remove the override and BOTH assertions break
    (two send() calls recorded, WARNING+ERROR captured).
    """
    adapter = _adapter()
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda cid: None)  # "api" is no mission

    calls = []
    real_send = APIServerAdapter.send

    async def counting_send(self, chat_id, content, reply_to=None, metadata=None):
        calls.append(content)
        return await real_send(self, chat_id, content, reply_to=reply_to, metadata=metadata)

    adapter.send = types.MethodType(counting_send, adapter)

    with caplog.at_level("DEBUG"):
        result = await adapter._send_with_retry(
            chat_id="api",
            content="final mission answer",
        )

    assert result.success is False
    assert result.error == "API server uses HTTP request/response, not send()"
    assert calls == ["final mission answer"]
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


# ──────────────────────────────────────────────────────────────────────
# [jb] send() — silent success ONLY for managed missions (F2, 2026-08-29)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_is_contractual_failure_for_unmanaged_chat(monkeypatch):
    """No mission behind ``chat_id`` → the honest failure (a send_message tool call
    aimed at this platform must never be reported as delivered)."""
    adapter = _adapter()
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda cid: None)

    result = await adapter.send("some-chat", "hello")

    assert result.success is False
    assert result.error == "API server uses HTTP request/response, not send()"


@pytest.mark.asyncio
async def test_send_is_silent_success_for_armed_mission(monkeypatch):
    """Armed in this process (POST /v1/message fills the cache) → silent success, no DB read."""
    adapter = _adapter()
    adapter._jb_managed_conversations = {"mission:1"}

    def _no_db(cid):  # pragma: no cover - must not be reached
        raise AssertionError("cache hit must not read the goal store")

    monkeypatch.setattr("hermes_cli.goals.load_goal", _no_db)

    result = await adapter.send("mission:1", "✓ Goal achieved: done")
    assert result.success is True
    assert (await adapter._send_with_retry(chat_id="mission:1", content="final answer")).success is True


@pytest.mark.asyncio
async def test_send_recognises_mission_from_goal_row_after_restart(tmp_path, monkeypatch):
    """Gateway restarted (cache empty): the goal row keyed by the anchor is the durable truth.

    Uses a REAL goal row (``save_goal``) in an isolated HERMES_HOME so the seam
    ``hermes_cli.goals.load_goal`` is exercised, not mocked; a second send hits
    the positive cache.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    try:
        goals.save_goal("mission:restart", goals.GoalState(goal="finish", max_turns=3))

        adapter = _adapter()
        assert adapter._is_managed_conversation("mission:restart") is True
        assert "mission:restart" in adapter._jb_managed_conversations  # cached for the next sends
        assert (await adapter.send("mission:restart", "⏸ Goal paused")).success is True
        # Sibling chat with no goal row: still the contractual failure.
        assert (await adapter.send("mission:other", "x")).success is False
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_send_goal_store_failure_reads_as_not_managed(monkeypatch):
    """A store error must never turn into a silent lie: contractual failure."""
    adapter = _adapter()

    def _boom(cid):
        raise RuntimeError("db down")

    monkeypatch.setattr("hermes_cli.goals.load_goal", _boom)
    assert (await adapter.send("mission:x", "x")).success is False
    assert adapter._is_managed_conversation("") is False
    assert adapter._is_managed_conversation(None) is False
