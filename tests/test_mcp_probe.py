"""Tests for the non-interactive ``hermes mcp probe`` subcommand.

Covers the frozen ``C1`` contract consumed by downstream lanes (fleet daemon,
TS classifier): read-only discovery, strict ``{tools:[{name,description}]}``
JSON on stdout, diagnostics on stderr, secrets never echoed, zero persistence.

Network is never touched: ``_probe_single_server`` is monkeypatched.
"""

from __future__ import annotations

import argparse
import json

import pytest

import hermes_cli.mcp_config as mcp_config
from hermes_cli.subcommands.mcp import build_mcp_parser


def _build_parser() -> argparse.ArgumentParser:
    """Build a real top-level parser with the ``mcp`` subcommand attached."""
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_mcp_parser(subparsers, cmd_mcp=lambda args: None)
    return parser


# ── GATE 2: the parser accepts `probe` with the expected shape ────────────────

def test_parser_accepts_probe():
    parser = _build_parser()
    ns = parser.parse_args(
        ["mcp", "probe", "--url", "https://x.example", "--header", "Authorization: Bearer k"]
    )
    assert ns.mcp_action == "probe"
    assert ns.url == "https://x.example"
    assert ns.header == ["Authorization: Bearer k"]


def test_parser_header_is_repeatable_and_optional():
    parser = _build_parser()

    # Zero headers → default empty list.
    ns = parser.parse_args(["mcp", "probe", "--url", "https://x.example"])
    assert ns.header == []

    # Repeated --header accumulates in order.
    ns = parser.parse_args(
        [
            "mcp", "probe", "--url", "https://x.example",
            "--header", "Authorization: Bearer k",
            "--header", "X-Tenant: acme",
        ]
    )
    assert ns.header == ["Authorization: Bearer k", "X-Tenant: acme"]


def test_parser_url_is_required():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["mcp", "probe"])


# ── GATE 3 + 5: JSON shape on stdout, parsable by a single json.loads ─────────

def test_probe_shape_and_clean_stdout(monkeypatch, capsys):
    captured = {}

    def fake_probe(name, config, **kwargs):
        # `tools/call` is structurally impossible here: the shared transport only
        # ever lists tools, and this mock returns a static (name, description) list.
        captured["name"] = name
        captured["config"] = config
        return [("get_ticket", "desc"), ("search_tickets", "")]

    monkeypatch.setattr(mcp_config, "_probe_single_server", fake_probe)

    args = argparse.Namespace(url="https://x.example", header=["Authorization: Bearer k"])
    mcp_config.cmd_mcp_probe(args)

    out = capsys.readouterr()
    # GATE 5: stdout is parsable by a single json.loads — no banner, no extra line.
    payload = json.loads(out.out)
    assert payload == {
        "tools": [
            {"name": "get_ticket", "description": "desc"},
            # Empty description is rendered as "" — present and a string, never null.
            {"name": "search_tickets", "description": ""},
        ]
    }
    assert payload["tools"][1]["description"] == ""
    assert out.err == ""

    # Ad-hoc, throwaway server name + parsed headers passed to the shared probe.
    assert captured["name"] == "__probe__"
    assert captured["config"] == {
        "url": "https://x.example",
        "headers": {"Authorization": "Bearer k"},
    }


def test_probe_empty_tool_list_is_valid(monkeypatch, capsys):
    monkeypatch.setattr(mcp_config, "_probe_single_server", lambda name, config, **kw: [])
    args = argparse.Namespace(url="https://x.example", header=[])
    mcp_config.cmd_mcp_probe(args)
    out = capsys.readouterr()
    assert json.loads(out.out) == {"tools": []}


# ── GATE 4: zero persistence ──────────────────────────────────────────────────

def test_probe_never_persists(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        pytest.fail("probe must not write config (save_config called)")

    monkeypatch.setattr(mcp_config, "save_config", _boom)
    monkeypatch.setattr(
        mcp_config, "_probe_single_server", lambda name, config, **kw: [("t", "d")]
    )
    args = argparse.Namespace(url="https://x.example", header=[])
    mcp_config.cmd_mcp_probe(args)
    assert json.loads(capsys.readouterr().out) == {"tools": [{"name": "t", "description": "d"}]}


# ── Error discipline: non-zero exit, scrubbed stderr, no partial JSON ─────────

def test_probe_missing_url_exits_2(monkeypatch, capsys):
    args = argparse.Namespace(url=None, header=[])
    with pytest.raises(SystemExit) as ei:
        mcp_config.cmd_mcp_probe(args)
    assert ei.value.code == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "error" in out.err


def test_probe_invalid_header_exits_2(capsys):
    args = argparse.Namespace(url="https://x.example", header=["no-colon-here"])
    with pytest.raises(SystemExit) as ei:
        mcp_config.cmd_mcp_probe(args)
    assert ei.value.code == 2
    out = capsys.readouterr()
    assert out.out == ""


def test_probe_failure_scrubs_secrets(monkeypatch, capsys):
    def boom(name, config, **kw):
        raise RuntimeError(
            "401 Unauthorized at https://secret.internal with Bearer SUPERSECRET"
        )

    monkeypatch.setattr(mcp_config, "_probe_single_server", boom)
    args = argparse.Namespace(
        url="https://secret.internal",
        header=["Authorization: Bearer SUPERSECRET"],
    )
    with pytest.raises(SystemExit) as ei:
        mcp_config.cmd_mcp_probe(args)
    assert ei.value.code == 1

    out = capsys.readouterr()
    # No partial JSON on stdout on failure.
    assert out.out == ""
    # Secrets (key + internal host) never echoed; only the exception type.
    assert "SUPERSECRET" not in out.err
    assert "secret.internal" not in out.err
    assert "RuntimeError" in out.err
