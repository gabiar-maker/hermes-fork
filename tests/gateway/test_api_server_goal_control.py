"""Jean-Billie managed goal control — POST /v1/goal-control (pause/resume).

Covers the reversible pause/resume loopback endpoint, WITHOUT a live LLM:

* nominal pause → ``200 {"status": "paused"}`` and the goal row flips to
  ``paused`` (the continuation hook and the watchdog both skip it);
* nominal resume → ``200 {"status": "resumed"}`` with the turn budget
  PRESERVED (``resume(reset_budget=False)`` — resuming is "keep going",
  never a fresh allowance);
* best-effort/idempotent: absent mission → ``200 {"status": "absent"}``
  (never an error); re-pause / re-resume are no-op echoes; a ``done`` or
  ``cleared`` mission reports ``absent`` and is NEVER resurrected
  (``/v1/clear`` keeps the row for audit — resume must not undo a stop);
* auth (401 when a Bearer key is configured), malformed bodies (400),
  unexpected GoalManager failure (502);
* negative control: the gateway loop (``handle_message``, the path that
  arms goals) and the agent runner are NEVER touched — goal-control only
  mutates goal state, it drives no turn.

Patterns mirror tests/gateway/test_watchdog_and_clear.py (hermes_home
fixture with goal-row wipe, minimal aiohttp app, TestClient/TestServer).
"""

from __future__ import annotations

import time
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
    """Isolated HERMES_HOME so goal ``state_meta`` writes never touch the real DB.

    Same caveat as test_watchdog_and_clear.py: ``hermes_state.DEFAULT_DB_PATH``
    is frozen at import time, so within one pytest process every ``SessionDB()``
    opens the SAME file regardless of the per-test ``HERMES_HOME``. We wipe the
    ``goal:`` rows on entry/exit so per-key assertions stay deterministic.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    def _wipe_goal_rows():
        goals._DB_CACHE.clear()
        db = goals._get_session_db()
        if db is None:
            return
        try:
            db._execute_write(
                lambda conn: conn.execute("DELETE FROM state_meta WHERE key LIKE 'goal:%'")
            )
        except Exception:
            pass
        goals._DB_CACHE.clear()

    _wipe_goal_rows()
    yield home
    _wipe_goal_rows()


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {"key": api_key} if api_key else {}
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra=extra))
    # Spies for the negative control: goal-control must NEVER reach the gateway
    # loop (the goal-arming path) nor spin an agent turn.
    adapter.handle_message = AsyncMock()
    adapter._run_agent = AsyncMock()
    return adapter


def _control_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/goal-control", adapter._handle_goal_control)
    return app


def _set_goal(conversation_id: str, *, status="active", turns_used=0, max_turns=20):
    """Write a goal directly to the DB with full control over its progress fields."""
    from hermes_cli.goals import GoalState, save_goal

    state = GoalState(
        goal=f"mission {conversation_id}",
        status=status,
        turns_used=turns_used,
        max_turns=max_turns,
        created_at=time.time(),
        last_turn_at=time.time(),
    )
    save_goal(conversation_id, state)
    return state


def _load(conversation_id: str):
    from hermes_cli.goals import load_goal

    return load_goal(conversation_id)


# ──────────────────────────────────────────────────────────────────────
# Nominal pause / resume
# ──────────────────────────────────────────────────────────────────────


class TestGoalControlNominal:
    @pytest.mark.asyncio
    async def test_pause_active_mission(self, hermes_home):
        _set_goal("mission:abc", status="active", turns_used=3)

        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:abc", "action": "pause"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"goalId": "mission:abc", "status": "paused"}

        state = _load("mission:abc")
        assert state is not None and state.status == "paused"
        assert state.paused_reason  # explicit user pause, reason recorded

        # Negative control: no gateway turn, no agent run — pure state flip.
        adapter.handle_message.assert_not_awaited()
        adapter._run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_paused_mission_preserves_budget(self, hermes_home):
        """Resume re-activates with the turn budget PRESERVED (reset_budget=False):
        the mission keeps going where it stopped, it never gets a fresh allowance."""
        _set_goal("mission:abc", status="paused", turns_used=5, max_turns=20)

        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:abc", "action": "resume"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"goalId": "mission:abc", "status": "resumed"}

        state = _load("mission:abc")
        assert state is not None and state.status == "active"
        assert state.turns_used == 5  # budget preserved — the reset_budget=False proof
        assert state.paused_reason is None

        adapter.handle_message.assert_not_awaited()
        adapter._run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_then_resume_round_trip(self, hermes_home):
        """« Rendre la main » : the pair is reversible — active → paused → active,
        with progress intact across the round trip."""
        _set_goal("mission:abc", status="active", turns_used=3, max_turns=12)

        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            pause = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:abc", "action": "pause"}
            )
            assert (await pause.json())["status"] == "paused"
            resume = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:abc", "action": "resume"}
            )
            assert (await resume.json())["status"] == "resumed"

        state = _load("mission:abc")
        assert state is not None and state.status == "active"
        assert state.turns_used == 3


# ──────────────────────────────────────────────────────────────────────
# Idempotence / absent semantics (best-effort, never an error)
# ──────────────────────────────────────────────────────────────────────


class TestGoalControlIdempotence:
    @pytest.mark.asyncio
    async def test_pause_already_paused_is_noop_echo(self, hermes_home):
        _set_goal("mission:abc", status="paused", turns_used=4)

        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:abc", "action": "pause"}
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "paused"

        state = _load("mission:abc")
        assert state is not None and state.status == "paused"
        assert state.turns_used == 4

    @pytest.mark.asyncio
    async def test_resume_already_active_is_noop_echo(self, hermes_home):
        _set_goal("mission:abc", status="active", turns_used=7)

        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:abc", "action": "resume"}
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "resumed"

        state = _load("mission:abc")
        assert state is not None and state.status == "active"
        assert state.turns_used == 7  # still no budget reset

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["pause", "resume"])
    async def test_absent_mission_is_absent_not_an_error(self, hermes_home, action):
        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:nope", "action": action}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {"goalId": "mission:nope", "status": "absent"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", ["done", "cleared"])
    @pytest.mark.parametrize("action", ["pause", "resume"])
    async def test_terminal_missions_report_absent_and_are_never_resurrected(
        self, hermes_home, terminal, action
    ):
        """/v1/clear keeps the row for audit (status=cleared) and the judge marks
        finished missions done — goal-control must treat both as ABSENT: resume
        must never restart a stopped/finished mission, pause must not relabel it."""
        _set_goal("mission:abc", status=terminal, turns_used=9)

        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:abc", "action": action}
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "absent"

        # The row is untouched: no resurrection, no relabeling.
        state = _load("mission:abc")
        assert state is not None and state.status == terminal
        assert state.turns_used == 9


# ──────────────────────────────────────────────────────────────────────
# Auth and input validation
# ──────────────────────────────────────────────────────────────────────


class TestGoalControlValidation:
    @pytest.mark.asyncio
    async def test_auth_required_when_key_configured(self, hermes_home):
        _set_goal("mission:abc", status="active")

        adapter = _make_adapter(api_key="sk-secret")
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/goal-control", json={"conversationId": "mission:abc", "action": "pause"}
            )
            assert resp.status == 401
            ok = await cli.post(
                "/v1/goal-control",
                json={"conversationId": "mission:abc", "action": "pause"},
                headers={"Authorization": "Bearer sk-secret"},
            )
            assert ok.status == 200

        # The 401 left the goal untouched; only the authorized call paused it.
        state = _load("mission:abc")
        assert state is not None and state.status == "paused"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"action": "pause"},  # missing conversationId
            {"conversationId": "   ", "action": "pause"},  # blank conversationId
            {"conversationId": 42, "action": "pause"},  # conversationId not a string
            {"conversationId": "mission:abc"},  # missing action
            {"conversationId": "mission:abc", "action": ""},  # blank action
            {"conversationId": "mission:abc", "action": "stop"},  # unknown action
            {"conversationId": "mission:abc", "action": 42},  # action not a string
            ["conversationId", "action"],  # body not a JSON object
        ],
    )
    async def test_malformed_bodies_are_400(self, hermes_home, body):
        _set_goal("mission:abc", status="active", turns_used=2)

        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post("/v1/goal-control", json=body)
            assert resp.status == 400

        # Validation rejects BEFORE touching goal state.
        state = _load("mission:abc")
        assert state is not None and state.status == "active"
        assert state.turns_used == 2

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, hermes_home):
        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            resp = await cli.post(
                "/v1/goal-control", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400


# ──────────────────────────────────────────────────────────────────────
# Failure guard + negative control
# ──────────────────────────────────────────────────────────────────────


class TestGoalControlGuards:
    @pytest.mark.asyncio
    async def test_goal_manager_failure_is_502(self, hermes_home):
        """An unexpected GoalManager failure surfaces as a clean 502 (the daemon
        relays non-2xx as `fork a répondu N` and the portal shows an honest error)."""
        adapter = _make_adapter()
        with patch(
            "hermes_cli.goals.GoalManager", MagicMock(side_effect=RuntimeError("boom"))
        ):
            async with TestClient(TestServer(_control_app(adapter))) as cli:
                resp = await cli.post(
                    "/v1/goal-control", json={"conversationId": "mission:abc", "action": "pause"}
                )
                assert resp.status == 502
                data = await resp.json()
                assert data["error"]["code"] == "goal_control_failed"

    @pytest.mark.asyncio
    async def test_gateway_loop_and_agent_are_never_touched(self, hermes_home):
        """Negative control across the whole surface: pause, resume, absent and
        invalid calls all leave handle_message (the goal-arming path — see
        test_managed_goal_arm) and _run_agent strictly untouched."""
        _set_goal("mission:abc", status="active")

        adapter = _make_adapter()
        async with TestClient(TestServer(_control_app(adapter))) as cli:
            for body in (
                {"conversationId": "mission:abc", "action": "pause"},
                {"conversationId": "mission:abc", "action": "resume"},
                {"conversationId": "mission:nope", "action": "pause"},
                {"conversationId": "mission:abc", "action": "stop"},
            ):
                await cli.post("/v1/goal-control", json=body)

        adapter.handle_message.assert_not_awaited()
        adapter._run_agent.assert_not_awaited()
