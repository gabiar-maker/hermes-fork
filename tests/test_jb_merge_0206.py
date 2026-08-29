"""Tests-pièges de la fusion amont v2026.8.27 (Hermes 0.20.6) — fork Jean-Billie.

Chaque test pince UN hunk de la dette cœur dont l'oubli au rebase casse la box en
SILENCE (rapport S1, base de contexte « Upstream Hermes 0.20 », 2026-08-28) :

* H3  cron/scheduler.py     — RÉSORBÉ (F2, 2026-08-29) : plus de wrapper ``run_job`` ; le
                              fichier doit rester BYTE-IDENTIQUE au tag amont (l'attribution
                              cron vit dans le plugin via on_session_start/on_session_end).
* H6  api_server.py         — ``_create_agent(stateless=True)`` = ``session_db=None`` +
                              ``skip_memory=True`` (sinon /v1/reply lit/écrit la mémoire
                              du client pour un TIERS).
* H8  api_server.py         — ``_run_agent`` re-transmet ``enabled_toolsets_override`` +
                              ``stateless`` à ``_create_agent``.
* H2  Dockerfile            — ``CMD ["gateway", "run"]`` conservé sous l'ENTRYPOINT amont
                              (sinon la box démarre un REPL : compose.ts ne pose aucun
                              ``command:``).
* H13 pyproject.toml        — ``pytest-timeout`` présent dans le groupe dev (nos
                              ``addopts --timeout`` cassent TOUTE la suite sans lui).
* H18 tools/delegate_tool.py — ``_finalize_child_results`` passe ``child_department`` +
                              ``child_subagent_id`` au hook ``subagent_stop`` (sinon
                              attribution par casquette perdue en silence).
"""

from __future__ import annotations

import inspect
import re
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────
# H3 — cron/scheduler.py est REDEVENU amont (F2) : aucun patch jb ne doit y revenir
# ──────────────────────────────────────────────────────────────────────


UPSTREAM_TAG = "v2026.8.27"


class TestCronSchedulerIsUpstream:
    def test_no_jb_marker_in_scheduler(self):
        """Piège de refusion (sans dépendre du tag git) : aucune trace du wrapper résorbé."""
        text = (REPO_ROOT / "cron" / "scheduler.py").read_text(encoding="utf-8")
        for marker in ("_jb_job_hooks", "_run_job_impl", "jb_outbound", "job_context", "[jb]"):
            assert marker not in text, f"marqueur jb réintroduit dans cron/scheduler.py : {marker}"
        import cron.scheduler as scheduler

        assert not hasattr(scheduler, "_run_job_impl")
        assert not hasattr(scheduler, "_jb_job_hooks")

    def test_scheduler_is_byte_identical_to_upstream_tag(self):
        """Critère de réussite du lot F2-1 : `git diff v2026.8.27 -- cron/scheduler.py` vide.

        Skippé (pas un échec) quand le tag n'est pas résolvable (checkout CI peu profond) : le test
        précédent et le garde-fou d'allowlist (jb-guard.yml) restent les gardes en CI.
        """
        import subprocess

        probe = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", f"{UPSTREAM_TAG}^{{commit}}"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if probe.returncode != 0:
            pytest.skip(f"tag {UPSTREAM_TAG} indisponible dans ce checkout")
        upstream = subprocess.run(
            ["git", "show", f"{UPSTREAM_TAG}:cron/scheduler.py"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout.replace(b"\r\n", b"\n")
        local = (REPO_ROOT / "cron" / "scheduler.py").read_bytes().replace(b"\r\n", b"\n")
        assert local == upstream, "cron/scheduler.py diverge du tag amont : dette cœur réintroduite"

    def test_plugin_registers_the_native_session_hooks(self):
        """Le remplaçant du wrapper : on_session_start/on_session_end (hooks VALID_HOOKS, bornés)."""
        import sys

        from hermes_cli.plugins import VALID_HOOKS, _HOOK_TIMEOUT_BOUNDED_HOOKS

        plugins_dir = REPO_ROOT / "plugins"
        if str(plugins_dir) not in sys.path:
            sys.path.insert(0, str(plugins_dir))
        import jb_outbound

        hooks: dict = {}

        class _Ctx:
            def register_middleware(self, *a, **k):
                pass

            def register_tool(self, **kw):
                pass

            def register_auxiliary_task(self, *a, **k):
                pass

            def register_web_search_provider(self, *a, **k):
                pass

            def register_cli_command(self, **kw):
                pass

            def register_hook(self, name, cb):
                hooks[name] = cb

        jb_outbound.register(_Ctx())
        assert {"on_session_start", "on_session_end"} <= set(hooks)
        assert {"on_session_start", "on_session_end"} <= VALID_HOOKS
        # Hooks BORNÉS = thread de travail + contexte copié : la raison du registre par session.
        assert {"on_session_start", "on_session_end"} <= _HOOK_TIMEOUT_BOUNDED_HOOKS


# ──────────────────────────────────────────────────────────────────────
# H6 + H8 — /v1/reply sans état (gateway/platforms/api_server.py)
# ──────────────────────────────────────────────────────────────────────


def _make_adapter():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


_RUNTIME_KWARGS = {
    "api_key": "test-key",
    "base_url": None,
    "provider": None,
    "api_mode": None,
    "command": None,
    "args": [],
}


class TestCreateAgentStateless:
    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    @pytest.mark.parametrize("stateless", [True, False])
    def test_stateless_flag_drives_session_db_and_skip_memory(self, stateless):
        """stateless=True → session_db None + skip_memory True ; contrôle positif False."""
        adapter = _make_adapter()
        sentinel_db = object()
        adapter._ensure_session_db = MagicMock(return_value=sentinel_db)

        # api_server importe AIAgent PARESSEUSEMENT dans _create_agent
        # (`from run_agent import AIAgent`) : le seam à pincer est run_agent.AIAgent.
        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_kwargs.return_value = dict(_RUNTIME_KWARGS)
            mock_model.return_value = "test/model"
            mock_config.return_value = {"platform_toolsets": {"api_server": ["web"]}}
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent(enabled_toolsets_override=[], stateless=stateless)

            mock_agent_cls.assert_called_once()
            kwargs = mock_agent_cls.call_args.kwargs

        assert "skip_memory" in kwargs, "H6 perdu : skip_memory absent des kwargs AIAgent"
        assert kwargs["enabled_toolsets"] == []
        if stateless:
            assert kwargs["session_db"] is None
            assert kwargs["skip_memory"] is True
            adapter._ensure_session_db.assert_not_called()
        else:
            assert kwargs["session_db"] is sentinel_db
            assert kwargs["skip_memory"] is False

    @pytest.mark.asyncio
    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    async def test_run_agent_forwards_override_and_stateless_to_create_agent(self):
        """H8 : le corps `_run()` amont (profile_scope, browser_control) garde nos 2 kwargs."""
        adapter = _make_adapter()
        fake_agent = MagicMock()
        fake_agent.run_conversation.return_value = {"final_response": "ok"}
        fake_agent.session_prompt_tokens = 1
        fake_agent.session_completion_tokens = 1
        fake_agent.session_total_tokens = 2

        with patch.object(adapter, "_create_agent", return_value=fake_agent) as mock_create:
            result, _usage = await adapter._run_agent(
                user_message="bonjour",
                conversation_history=[],
                session_id=None,
                gateway_session_key=None,
                enabled_toolsets_override=[],
                stateless=True,
            )

        assert result["final_response"] == "ok"
        create_kwargs = mock_create.call_args.kwargs
        assert create_kwargs["enabled_toolsets_override"] == []
        assert create_kwargs["stateless"] is True


# ──────────────────────────────────────────────────────────────────────
# H2 — Dockerfile : ENTRYPOINT amont + CMD fork
# ──────────────────────────────────────────────────────────────────────


class TestDockerfileKeepsGatewayCmd:
    def test_cmd_gateway_run_survives_under_upstream_dispatcher(self):
        text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        cmd_lines = [ln for ln in text.splitlines() if re.match(r"^\s*CMD\b", ln)]
        assert len(cmd_lines) == 1, f"un seul CMD attendu, trouvé : {cmd_lines}"
        assert re.search(r'^\s*CMD\s*\[\s*"gateway"\s*,\s*"run"\s*\]\s*$', cmd_lines[0]), cmd_lines[0]

        entry_lines = [ln for ln in text.splitlines() if re.match(r"^\s*ENTRYPOINT\b", ln)]
        assert len(entry_lines) == 1
        assert "entrypoint-dispatch.sh" in entry_lines[0], entry_lines[0]
        # Le CMD doit suivre l'ENTRYPOINT (un CMD posé AVANT serait écrasé par `CMD [ ]` amont).
        assert text.index(entry_lines[0]) < text.index(cmd_lines[0])

    def test_dispatcher_forwards_cmd_to_main_wrapper(self):
        """Le dispatcher amont transmet « "$@" » (donc notre `gateway run`) au wrapper."""
        text = (REPO_ROOT / "docker" / "entrypoint-dispatch.sh").read_text(encoding="utf-8")
        assert 'exec /init /opt/hermes/docker/main-wrapper.sh "$@"' in text
        assert 'exec /opt/hermes/docker/main-wrapper.sh "$@"' in text


# ──────────────────────────────────────────────────────────────────────
# H13 — pyproject.toml : pytest-timeout
# ──────────────────────────────────────────────────────────────────────


class TestPytestTimeoutDependency:
    def test_pytest_timeout_importable(self):
        import pytest_timeout  # noqa: F401

    def test_pyproject_declares_pytest_timeout_in_dev_group(self):
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dev = data["project"]["optional-dependencies"]["dev"]
        pins = [d for d in dev if re.match(r"^pytest-timeout\s*==", d)]
        assert pins, f"pytest-timeout absent du groupe dev : {dev}"
        # La raison d'être du pin : nos addopts exigent le plugin.
        addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
        assert "--timeout=" in addopts


# ──────────────────────────────────────────────────────────────────────
# H18 — _finalize_child_results → subagent_stop porte la casquette
# ──────────────────────────────────────────────────────────────────────


class TestFinalizeChildResultsCarriesDepartment:
    def test_subagent_stop_receives_child_department_and_subagent_id(self, monkeypatch):
        from tools import delegate_tool

        hook_calls: list = []

        def _fake_invoke_hook(name, **kwargs):
            hook_calls.append((name, kwargs))

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_invoke_hook)

        child = SimpleNamespace(
            session_id="child-sess",
            _subagent_id="sa-0-abcd1234",
            _jb_department="comptable",
        )
        untagged = SimpleNamespace(session_id="child-2")  # ni _subagent_id ni _jb_department
        parent = SimpleNamespace(session_id="parent-sess", _current_turn_id="t1")
        results = [
            {"task_index": 0, "summary": "fait", "status": "completed", "_child_role": "leaf",
             "duration_seconds": 1.5},
            {"task_index": 1, "summary": "raté", "status": "failed", "_child_role": "leaf"},
        ]
        task_list = [{"goal": "rapprocher les factures"}, {"goal": "autre"}]
        children = [(0, task_list[0], child), (1, task_list[1], untagged)]

        delegate_tool._finalize_child_results(results, task_list, children, parent)

        stops = [kw for name, kw in hook_calls if name == "subagent_stop"]
        assert len(stops) == 2
        by_session = {kw["child_session_id"]: kw for kw in stops}

        tagged = by_session["child-sess"]
        assert tagged["child_department"] == "comptable"
        assert tagged["child_subagent_id"] == "sa-0-abcd1234"
        assert tagged["parent_session_id"] == "parent-sess"
        assert tagged["child_status"] == "completed"
        assert tagged["duration_ms"] == 1500

        # Enfant non tagué : les clés existent (contrat stable côté plugin) et valent None.
        plain = by_session["child-2"]
        assert plain["child_department"] is None
        assert plain["child_subagent_id"] is None
