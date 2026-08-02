"""Jean-Billie managed reply surface — fork R5.

``POST /v1/reply`` serves ONE bounded, stateless reply turn for messages coming
from unknown third parties (WhatsApp webhook, inbound phone standard). It is the
synchronous counterpart of ``POST /v1/message`` (which arms an autonomous
background mission) and exists precisely to close that collision: a stranger's
message must never become a mission of the box (DECISIONS §A.3).

Covered WITHOUT a live LLM:

* nominal turn → ``200 {"reply": …}``, with the run flagged stateless + zero
  toolsets, and — the negative control — the gateway loop (``handle_message``,
  the path that arms goals) is NEVER touched;
* caller-relayed history (``context.history``) is validated and passed through
  verbatim;
* auth (401), malformed bodies (400), oversized text/history (400);
* latency guard → 504, concurrency cap → 429, empty/failed run → 502
  (fail-closed: the portal falls back honestly, nothing is fabricated);
* ``_create_agent`` honours ``enabled_toolsets_override=[]`` (the empty list
  means NO toolsets — the gates test ``is None``, not falsiness) and
  ``stateless=True`` (no session DB, ``skip_memory=True``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


# ──────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so nothing ever touches the real DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home


def _reply_app(adapter: APIServerAdapter) -> web.Application:
    """Minimal aiohttp app exposing only the reply route (mirrors test_managed_goal_arm)."""
    app = web.Application()
    app.router.add_post("/v1/reply", adapter._handle_reply)
    return app


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {"key": api_key} if api_key else {}
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra=extra))
    # Spy on the goal-arming path: /v1/reply must NEVER reach the gateway loop.
    adapter.handle_message = AsyncMock()
    # Stub the agent run; individual tests override the return value as needed.
    adapter._run_agent = AsyncMock(return_value=({"final_response": "Bonjour !"}, {}))
    return adapter


VALID_BODY = {"text": "Bonjour, vous êtes ouverts demain ?", "conversationId": "whatsapp"}


# ──────────────────────────────────────────────────────────────────────
# Nominal turn
# ──────────────────────────────────────────────────────────────────────


class TestReplyNominal:
    @pytest.mark.asyncio
    async def test_reply_returns_200_with_reply_text(self, hermes_home):
        adapter = _make_adapter()
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post("/v1/reply", json=VALID_BODY)
            assert resp.status == 200
            data = await resp.json()
            assert data == {"reply": "Bonjour !"}

        # The run is stateless, tool-less, and session-less by construction.
        adapter._run_agent.assert_awaited_once()
        kwargs = adapter._run_agent.await_args.kwargs
        assert kwargs["user_message"] == VALID_BODY["text"]
        assert kwargs["conversation_history"] == []
        assert kwargs["session_id"] is None
        assert kwargs["gateway_session_key"] is None
        assert kwargs["enabled_toolsets_override"] == []
        assert kwargs["stateless"] is True

        # Negative control: the goal-arming path was never touched. The same
        # body POSTed to /v1/message injects a synthetic ``/goal`` turn (see
        # test_managed_goal_arm) — THAT is the collision this route closes.
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_caller_history_is_relayed_verbatim(self, hermes_home):
        adapter = _make_adapter()
        history = [
            {"role": "user", "content": "Bonjour"},
            {"role": "assistant", "content": "Bonjour, que puis-je faire ?"},
        ]
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/reply", json={**VALID_BODY, "context": {"history": history}}
            )
            assert resp.status == 200

        kwargs = adapter._run_agent.await_args.kwargs
        assert kwargs["conversation_history"] == history

    @pytest.mark.asyncio
    async def test_extra_context_fields_are_tolerated(self, hermes_home):
        """The portal transport already sends firstName/casquettes/mode — the
        route must accept the existing envelope shape without a contract bump."""
        adapter = _make_adapter()
        context = {"firstName": "Ada", "casquettes": ["commercial"], "mode": "standard-entrant"}
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post("/v1/reply", json={**VALID_BODY, "context": context})
            assert resp.status == 200


# ──────────────────────────────────────────────────────────────────────
# Auth and input validation
# ──────────────────────────────────────────────────────────────────────


class TestReplyValidation:
    @pytest.mark.asyncio
    async def test_auth_required_when_key_configured(self, hermes_home):
        adapter = _make_adapter(api_key="sekret")
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post("/v1/reply", json=VALID_BODY)
            assert resp.status == 401
            ok = await cli.post(
                "/v1/reply", json=VALID_BODY, headers={"Authorization": "Bearer sekret"}
            )
            assert ok.status == 200
        assert adapter._run_agent.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"conversationId": "whatsapp"},  # missing text
            {"text": "   ", "conversationId": "whatsapp"},  # blank text
            {"text": "Bonjour"},  # missing conversationId
            {**VALID_BODY, "context": "nope"},  # context not a dict
            {**VALID_BODY, "context": {"history": "nope"}},  # history not a list
            {**VALID_BODY, "context": {"history": [{"role": "system", "content": "x"}]}},
            {**VALID_BODY, "context": {"history": [{"role": "user", "content": 42}]}},
            {**VALID_BODY, "context": {"history": ["nope"]}},
        ],
    )
    async def test_malformed_bodies_are_400(self, hermes_home, body):
        adapter = _make_adapter()
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post("/v1/reply", json=body)
            assert resp.status == 400
        adapter._run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oversized_text_is_400(self, hermes_home):
        adapter = _make_adapter()
        big = "x" * (APIServerAdapter._REPLY_MAX_TEXT_CHARS + 1)
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/reply", json={"text": big, "conversationId": "whatsapp"}
            )
            assert resp.status == 400
        adapter._run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_too_many_history_turns_is_400(self, hermes_home):
        adapter = _make_adapter()
        turns = [
            {"role": "user", "content": "hi"}
        ] * (APIServerAdapter._REPLY_MAX_HISTORY_TURNS + 1)
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/reply", json={**VALID_BODY, "context": {"history": turns}}
            )
            assert resp.status == 400
        adapter._run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oversized_history_chars_is_400(self, hermes_home):
        adapter = _make_adapter()
        chunk = "x" * (APIServerAdapter._REPLY_MAX_HISTORY_CHARS // 2 + 1)
        turns = [
            {"role": "user", "content": chunk},
            {"role": "assistant", "content": chunk},
        ]
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/reply", json={**VALID_BODY, "context": {"history": turns}}
            )
            assert resp.status == 400
        adapter._run_agent.assert_not_awaited()


# ──────────────────────────────────────────────────────────────────────
# Latency / volume / failure guards
# ──────────────────────────────────────────────────────────────────────


class TestReplyGuards:
    @pytest.mark.asyncio
    async def test_timeout_is_504(self, hermes_home, monkeypatch):
        adapter = _make_adapter()

        async def _slow(**_kwargs):
            await asyncio.sleep(0.2)
            return {"final_response": "trop tard"}, {}

        adapter._run_agent = _slow
        monkeypatch.setattr(APIServerAdapter, "_REPLY_TIMEOUT_SECONDS", 0.05)
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post("/v1/reply", json=VALID_BODY)
            assert resp.status == 504
            data = await resp.json()
            assert data["error"]["code"] == "reply_timeout"

    @pytest.mark.asyncio
    async def test_concurrency_cap_is_429(self, hermes_home):
        adapter = _make_adapter()
        adapter._max_concurrent_runs = 1
        adapter._inflight_agent_runs = 1
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post("/v1/reply", json=VALID_BODY)
            assert resp.status == 429
        adapter._run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_failure_is_502(self, hermes_home):
        adapter = _make_adapter()
        adapter._run_agent = AsyncMock(side_effect=RuntimeError("boom"))
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post("/v1/reply", json=VALID_BODY)
            assert resp.status == 502
            data = await resp.json()
            assert data["error"]["code"] == "reply_failed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("final", [None, "", "   "])
    async def test_empty_reply_is_502_fail_closed(self, hermes_home, final):
        adapter = _make_adapter()
        adapter._run_agent = AsyncMock(return_value=({"final_response": final}, {}))
        async with TestClient(TestServer(_reply_app(adapter))) as cli:
            resp = await cli.post("/v1/reply", json=VALID_BODY)
            assert resp.status == 502
            data = await resp.json()
            assert data["error"]["code"] == "reply_empty"


# ──────────────────────────────────────────────────────────────────────
# _create_agent: reduced toolset + statelessness (mirrors
# TestApiServerAdapterToolset in test_api_server_toolset.py)
# ──────────────────────────────────────────────────────────────────────


class TestCreateAgentReplyMode:
    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_empty_override_means_no_toolsets_and_no_session(self):
        adapter = APIServerAdapter(PlatformConfig())

        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            mock_model.return_value = "test/model"
            # Even a permissive config must not leak toolsets past the override.
            mock_config.return_value = {
                "platform_toolsets": {"api_server": ["web", "terminal", "memory"]}
            }
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent(enabled_toolsets_override=[], stateless=True)

            mock_agent_cls.assert_called_once()
            kwargs = mock_agent_cls.call_args.kwargs
            # [] must survive as [] (the agent_init gates test ``is None``).
            assert kwargs.get("enabled_toolsets") == []
            # Stateless: nothing persisted, owner memory neither read nor written.
            assert kwargs.get("session_db") is None
            assert kwargs.get("skip_memory") is True

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_default_path_is_unchanged(self):
        """No override → config resolution and session DB exactly as before."""
        adapter = APIServerAdapter(PlatformConfig())
        sentinel_db = object()
        adapter._ensure_session_db = MagicMock(return_value=sentinel_db)

        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            mock_model.return_value = "test/model"
            mock_config.return_value = {
                "platform_toolsets": {"api_server": ["web", "terminal"]}
            }
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent()

            kwargs = mock_agent_cls.call_args.kwargs
            assert sorted(kwargs.get("enabled_toolsets")) == ["terminal", "web"]
            assert kwargs.get("session_db") is sentinel_db
            assert kwargs.get("skip_memory") is False
