from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.callbacks = {}
        self._active_sessions = {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SimpleNamespace(success=True)

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self.callbacks[session_key] = (generation, callback)


def _goal_continuation_event(source, goal="finish the task"):
    return MessageEvent(
        text=CONTINUATION_PROMPT_TEMPLATE.format(goal=goal),
        message_type=MessageType.TEXT,
        source=source,
    )


@pytest.mark.asyncio
async def test_goal_status_notice_defers_until_post_delivery_callback():
    """Regression: goal status must appear after the agent's visible reply.

    _post_turn_goal_continuation runs before BasePlatformAdapter sends the
    returned final response. It should therefore register a post-delivery
    callback, not send the judge status immediately.
    """
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="parent-channel",
        thread_id="thread-123",
        user_id="user-1",
    )

    await runner._defer_goal_status_notice_after_delivery(source, "✓ Goal achieved: done")

    assert adapter.calls == []
    assert len(adapter.callbacks) == 1

    _, callback = next(iter(adapter.callbacks.values()))
    result = callback()
    if hasattr(result, "__await__"):
        await result

    assert adapter.calls == [
        {
            "chat_id": "parent-channel",
            "content": "✓ Goal achieved: done",
            "reply_to": None,
            "metadata": {"thread_id": "thread-123"},
        }
    ]


# ──────────────────────────────────────────────────────────────────────
# [jb] Managed missions on the push-less api_server adapter (F2, 2026-08-29)
#
# gateway/run.py is UPSTREAM here (the former ``supports_push_send`` guard is
# resorbed): ``_send_goal_status_notice`` calls ``adapter.send()`` and logs a
# WARNING on failure. The silence now lives on the adapter — ``send()`` is a
# silent success for a conversation the control daemon drives, and a
# contractual failure for anything else. Both halves are pinned below, on the
# REAL APIServerAdapter, so removing either the adapter seam or the guard-free
# upstream path shows up here.
# ──────────────────────────────────────────────────────────────────────


def _real_api_server_adapter():
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter.__new__(APIServerAdapter)
    adapter.platform = Platform.API_SERVER  # feeds .name, as __init__ would
    return adapter


@pytest.mark.asyncio
async def test_goal_status_notice_is_silent_on_managed_mission(caplog, monkeypatch):
    """A managed mission's status notice: no send failure, no WARNING, run.py untouched.

    The conversationId is known to the adapter (armed through POST /v1/message),
    so ``send()`` reports a silent success and the upstream notice code has
    nothing to warn about. Goal state is read over GET /v1/goals instead.
    """
    adapter = _real_api_server_adapter()
    adapter._jb_managed_conversations = {"mission:42"}

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.API_SERVER: adapter}
    source = SessionSource(platform=Platform.API_SERVER, chat_id="mission:42")

    with caplog.at_level("DEBUG", logger="gateway.run"):
        await runner._send_goal_status_notice(source, "✓ Goal achieved: done")

    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


@pytest.mark.asyncio
async def test_goal_status_notice_still_warns_for_unmanaged_chat(caplog, monkeypatch):
    """Control: the silence is the adapter seam, not a swallowed failure.

    For a chat the daemon does not drive (and with no goal row for it), the
    adapter keeps its contractual failure and the UPSTREAM notice code logs
    the WARNING — proving run.py is guard-free and that ``send()`` does not
    lie for arbitrary targets.
    """
    adapter = _real_api_server_adapter()
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda cid: None)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.API_SERVER: adapter}
    source = SessionSource(platform=Platform.API_SERVER, chat_id="not-a-mission")

    with caplog.at_level("DEBUG", logger="gateway.run"):
        await runner._send_goal_status_notice(source, "✓ Goal achieved: done")

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "status send failed" in warnings[0].getMessage()


def test_api_server_adapter_declares_no_push_send():
    """Contract (documentation seam): the adapter states it has no push channel."""
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter.__new__(APIServerAdapter)
    assert adapter.supports_push_send is False


def test_gateway_run_has_no_push_send_special_case():
    """Resorbed hunk: gateway/run.py must not consult ``supports_push_send`` anymore."""
    import inspect

    import gateway.run as run

    assert "supports_push_send" not in inspect.getsource(run)
