"""Arrêt propre du listener de décisions au déchargement du plugin (`ctx.on_unload`).

En 0.20.6, le cache des plugins par profil (`discover_plugins(force=True)`) purge le module : sans
arrêt enregistré, le thread `jb-outbound-listener` survivrait sur l'ancien code, port tenu, et le
nouveau module se croirait « déjà démarré ». On prouve : (a) un `ctx` qui offre `on_unload` reçoit
notre callback ; l'appeler ferme le serveur (port libéré) et un second `start()` re-binde ;
(b) un `ctx` SANS `on_unload` (0.18.2, base actuelle du fork) charge sans erreur ;
(c) `stop()` est idempotent.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

_PLUGINS_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

import jb_outbound  # noqa: E402
import jb_outbound.listener as listener  # noqa: E402

_PORT = 18446  # distinct du e2e (18444) : les deux suites peuvent tourner dans le même process


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(autouse=True)
def _on_box(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:18442")
    monkeypatch.setenv("JB_DECISION_PUSH_URL", f"http://127.0.0.1:{_PORT}/jb/decision")
    listener.stop()  # état propre quel que soit l'ordre des tests (le e2e laisse un serveur vivant)
    yield
    listener.stop()


class _BaseCtx:
    """Faux PluginContext minimal : tout ce que `register()` appelle, sans effet."""

    def register_middleware(self, *a, **k):
        pass

    def register_hook(self, *a, **k):
        pass

    def register_tool(self, *a, **k):
        pass

    def register_auxiliary_task(self, *a, **k):
        pass

    def register_web_search_provider(self, *a, **k):
        pass


class _CtxWithUnload(_BaseCtx):
    """PluginContext 0.20.6 : offre `on_unload` et capture le callback."""

    def __init__(self):
        self.unload_callbacks: list = []

    def on_unload(self, callback):
        assert callable(callback)
        self.unload_callbacks.append(callback)


def test_on_unload_ferme_le_serveur_et_un_second_start_rebinde():
    ctx = _CtxWithUnload()
    jb_outbound.register(ctx)

    assert listener._started is True
    assert _port_open(_PORT), "le listener écoute après register()"
    assert len(ctx.unload_callbacks) == 1

    ctx.unload_callbacks[0]()  # le cœur décharge le plugin

    assert listener._started is False
    assert listener._server is None
    assert not _port_open(_PORT), "le port est libéré après on_unload"

    listener.start()  # le nouveau module (ou un rechargement) re-binde le MÊME port
    assert listener._started is True
    assert _port_open(_PORT)


def test_sans_on_unload_le_plugin_charge_quand_meme():
    """0.18.2 : pas de `on_unload` sur le contexte → aucune erreur, listener démarré."""
    ctx = _BaseCtx()
    assert not hasattr(ctx, "on_unload")

    jb_outbound.register(ctx)

    assert listener._started is True
    assert _port_open(_PORT)


def test_stop_est_idempotent_et_sans_serveur_no_op():
    listener.stop()  # jamais démarré dans ce test → no-op
    assert listener._started is False and listener._server is None

    listener.start()
    assert _port_open(_PORT)
    listener.stop()
    listener.stop()  # deuxième arrêt : no-op, pas d'exception
    assert listener._started is False and not _port_open(_PORT)
