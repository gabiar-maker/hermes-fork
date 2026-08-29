"""Parité ``hermes jb-mcp-probe`` (commande plugin, F2 lot 3) ↔ ``hermes mcp probe`` (hunk cœur).

Tant que les deux coexistent (transition : le daemon Go bascule de chaîne en lane D, puis le hunk
cœur ``hermes_cli/mcp_config.py`` + ``subcommands/mcp.py`` est retiré), chaque cas exécute LES DEUX
et compare stdout, stderr et code de sortie — ce que le daemon parse doit être byte-identique.
Réseau jamais touché : ``_probe_single_server`` est monkeypatché sur le module cœur (résolu par
attribut à l'appel dans les deux implémentations).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

_PLUGINS_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

import jb_outbound.mcp_probe as mcp_probe  # noqa: E402

import hermes_cli.mcp_config as mcp_config  # noqa: E402
from hermes_cli.subcommands.mcp import build_mcp_parser  # noqa: E402


def _run(fn, args) -> tuple[str, str, int | None]:
    """Exécute un handler CLI et capture (stdout, stderr, code de sortie ou None)."""
    out, err = io.StringIO(), io.StringIO()
    code = None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            fn(args)
        except SystemExit as exc:
            code = exc.code
    return out.getvalue(), err.getvalue(), code


def _both(args) -> tuple[tuple, tuple]:
    plugin = _run(mcp_probe.run, args)
    core = _run(mcp_config.cmd_mcp_probe, args)
    return plugin, core


def _plugin_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser(mcp_probe.COMMAND_NAME, help=mcp_probe.COMMAND_HELP)
    mcp_probe.setup_parser(p)
    p.set_defaults(func=mcp_probe.run)
    return parser


def _core_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_mcp_parser(sub, cmd_mcp=lambda a: None)
    return parser


# ── enregistrement par le plugin (seam natif register_cli_command) ───────────

def test_register_declare_la_commande_cli():
    import jb_outbound

    seen: list = []

    class FakeCtx:
        def register_middleware(self, *a, **k):
            pass

        def register_hook(self, *a, **k):
            pass

        def register_tool(self, **kw):
            pass

        def register_auxiliary_task(self, *a, **k):
            pass

        def register_web_search_provider(self, *a, **k):
            pass

        def register_cli_command(self, **kw):
            seen.append(kw)

    jb_outbound.register(FakeCtx())
    assert [c["name"] for c in seen] == ["jb-mcp-probe"]
    cmd = seen[0]
    assert cmd["setup_fn"] is mcp_probe.setup_parser and cmd["handler_fn"] is mcp_probe.run
    assert cmd["help"] and cmd["description"]


def test_register_via_le_vrai_plugin_context(tmp_path, monkeypatch):
    """Le PluginContext réel enregistre la commande dans ``_cli_commands`` (lu par hermes_cli/main.py)."""
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    manager = PluginManager(scope_key=str(tmp_path))
    manifest = PluginManifest(name="jb_outbound", version="0.1.0", description="", author="")
    ctx = PluginContext(manifest, manager)
    ctx.register_cli_command(
        name=mcp_probe.COMMAND_NAME, help=mcp_probe.COMMAND_HELP,
        setup_fn=mcp_probe.setup_parser, handler_fn=mcp_probe.run,
        description=mcp_probe.COMMAND_DESCRIPTION,
    )
    entry = manager._cli_commands["jb-mcp-probe"]
    assert entry["setup_fn"] is mcp_probe.setup_parser and entry["handler_fn"] is mcp_probe.run


# ── parité de l'analyseur d'arguments ────────────────────────────────────────

def test_parser_parite_url_et_headers():
    plugin = _plugin_parser().parse_args(
        ["jb-mcp-probe", "--url", "https://x.example", "--header", "Authorization: Bearer k", "--header", "X-T: a"]
    )
    core = _core_parser().parse_args(
        ["mcp", "probe", "--url", "https://x.example", "--header", "Authorization: Bearer k", "--header", "X-T: a"]
    )
    assert (plugin.url, plugin.header) == (core.url, core.header) == ("https://x.example", ["Authorization: Bearer k", "X-T: a"])
    assert plugin.func is mcp_probe.run

    # Zéro --header → liste vide dans les deux cas.
    assert _plugin_parser().parse_args(["jb-mcp-probe", "--url", "u"]).header == []
    assert _core_parser().parse_args(["mcp", "probe", "--url", "u"]).header == []


def test_parser_url_obligatoire_des_deux_cotes():
    with pytest.raises(SystemExit):
        _plugin_parser().parse_args(["jb-mcp-probe"])
    with pytest.raises(SystemExit):
        _core_parser().parse_args(["mcp", "probe"])


# ── parité de la sortie (stdout JSON, stderr scrubbé, codes de sortie) ────────

def test_parite_sortie_nominale(monkeypatch):
    captured: list = []

    def fake_probe(name, config, **kwargs):
        captured.append((name, config))
        return [("get_ticket", "desc"), ("search_tickets", "")]

    monkeypatch.setattr(mcp_config, "_probe_single_server", fake_probe)
    args = argparse.Namespace(url="https://x.example", header=["Authorization: Bearer k"])

    plugin, core = _both(args)
    assert plugin == core
    out, err, code = plugin
    assert code is None and err == ""
    assert json.loads(out) == {
        "tools": [
            {"name": "get_ticket", "description": "desc"},
            {"name": "search_tickets", "description": ""},
        ]
    }
    # Même serveur jetable, même config ad hoc (jamais persistée) des deux côtés.
    assert captured == [("__probe__", {"url": "https://x.example", "headers": {"Authorization": "Bearer k"}})] * 2


def test_parite_liste_vide(monkeypatch):
    monkeypatch.setattr(mcp_config, "_probe_single_server", lambda name, config, **kw: [])
    plugin, core = _both(argparse.Namespace(url="https://x.example", header=[]))
    assert plugin == core and json.loads(plugin[0]) == {"tools": []}


def test_parite_url_absente_exit_2():
    plugin, core = _both(argparse.Namespace(url=None, header=[]))
    assert plugin == core
    assert plugin[2] == 2 and plugin[0] == "" and "error" in plugin[1]


def test_parite_header_invalide_exit_2():
    plugin, core = _both(argparse.Namespace(url="https://x.example", header=["no-colon-here"]))
    assert plugin == core
    assert plugin[2] == 2 and plugin[0] == ""


def test_parite_echec_de_sonde_exit_1_secrets_jamais_renvoyes(monkeypatch):
    def boom(name, config, **kw):
        raise RuntimeError("401 Unauthorized at https://secret.internal with Bearer SUPERSECRET")

    monkeypatch.setattr(mcp_config, "_probe_single_server", boom)
    args = argparse.Namespace(url="https://secret.internal", header=["Authorization: Bearer SUPERSECRET"])

    plugin, core = _both(args)
    assert plugin == core
    out, err, code = plugin
    assert code == 1 and out == ""
    assert "SUPERSECRET" not in err and "secret.internal" not in err and "RuntimeError" in err


def test_jamais_de_persistance(monkeypatch):
    def _boom(*args, **kwargs):
        pytest.fail("probe must not write config (save_config called)")

    monkeypatch.setattr(mcp_config, "save_config", _boom)
    monkeypatch.setattr(mcp_config, "_probe_single_server", lambda name, config, **kw: [("t", "d")])
    out, _, _ = _run(mcp_probe.run, argparse.Namespace(url="https://x.example", header=[]))
    assert json.loads(out) == {"tools": [{"name": "t", "description": "d"}]}
