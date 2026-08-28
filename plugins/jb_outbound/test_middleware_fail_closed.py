"""Le middleware « rien ne part sans accord » est FAIL-CLOSED face à ses propres pannes.

Le runner du cœur (`hermes_cli/middleware.py::_run_execution_chain`) est fail-open : si une
callback lève AVANT d'avoir appelé `next_call`, il exécute l'outil lui-même (« if next_called:
raise ; return call_at(index + 1, payload) »). Avant le correctif, une exception dans classify /
mapping / store faisait donc PARTIR l'envoi. Ces tests prouvent que :
  (i)  une panne de `classify` → `next_call` n'est PAS appelé, résultat bloquant ;
  (ii) une panne du store (écriture du brouillon) → idem ; (mapping et `store.mark` aussi)
  (iii) contrôle positif : un outil non-sortant passe, `next_call` appelé exactement une fois ;
  (iv) contrôle positif : le chemin nominal `queued_for_approval` est byte-identique.
Un dernier test fait passer la callback par le VRAI runner du cœur : sur l'ancien code, l'outil
terminal était exécuté (fail-open) ; désormais il ne l'est pas.

Sur le code d'avant : (i)/(ii) levaient l'exception hors de la callback (aucun résultat bloquant →
rouge) ; via le runner du cœur, le terminal s'exécutait (→ rouge). (iii)/(iv) passaient déjà :
ce sont les gardes de non-régression du correctif.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

import pytest

_PLUGINS_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

# Racine du fork : pour importer le runner de middleware du cœur (le vrai, pas une imitation).
_FORK_ROOT = Path(__file__).resolve().parents[2]
if str(_FORK_ROOT) not in sys.path:
    sys.path.insert(0, str(_FORK_ROOT))

import jb_outbound.classify as classify  # noqa: E402
import jb_outbound.http_client as http_client  # noqa: E402
import jb_outbound.mapping as mapping  # noqa: E402
import jb_outbound.middleware as middleware  # noqa: E402
import jb_outbound.store as store  # noqa: E402
from hermes_cli.middleware import _run_execution_chain  # noqa: E402

_SECRET_BODY = "Bonjour Marie, voici le devis CONFIDENTIEL-4711"
_SEND_ARGS = {"chat_id": "42", "content": _SECRET_BODY}


class _Boom(RuntimeError):
    """Panne interne simulée (nom distinctif pour vérifier la journalisation)."""


@pytest.fixture(autouse=True)
def _on_box(tmp_path, monkeypatch):
    """Box Jean-Billie simulée : plugin actif, HERMES_HOME isolé, dépôt HTTP capturé."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("JB_DECISION_PUSH_URL", "http://127.0.0.1:8444/jb/decision")
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")


@pytest.fixture
def posts(monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        http_client, "post_json",
        lambda url, payload, timeout=10.0: captured.append((url, payload)) or 200,
    )
    return captured


class _NextCall:
    """Faux `next_call` : compte ses appels et retient les args reçus."""

    def __init__(self, result="TOOL_RESULT"):
        self.calls: list = []
        self.result = result

    def __call__(self, args=None):
        self.calls.append(args)
        return self.result


def _raise_boom(*_a, **_k):
    raise _Boom("panne interne simulée")


def _assert_blocked(out: str) -> None:
    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert "rien n'est parti" in payload["message"]
    # White-label : le message destiné au modèle ne nomme aucune techno ni détail interne.
    assert not re.search(r"(?i)exception|traceback|python|hermes|plugin", payload["message"])


# ---------------------------------------------------------------------------
# (i) / (ii) : une panne de NOTRE logique → bloqué, l'outil ne s'exécute pas.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "seam",
    [
        pytest.param((classify, "classify"), id="classify"),
        pytest.param((mapping, "to_draft"), id="mapping"),
        pytest.param((store, "save"), id="store.save"),
    ],
)
def test_panne_interne_bloque_sans_executer_l_outil(posts, monkeypatch, caplog, seam):
    module, name = seam
    monkeypatch.setattr(module, name, _raise_boom)
    nxt = _NextCall()

    with caplog.at_level(logging.ERROR):
        out = middleware.make_middleware()(tool_name="send_message", args=dict(_SEND_ARGS), next_call=nxt)

    _assert_blocked(out)
    assert nxt.calls == [], "l'outil d'envoi NE doit PAS s'exécuter quand notre vérification échoue"
    assert posts == [], "aucune proposition n'est déposée sur une panne interne"

    # Journalisé en ERROR avec le nom de l'outil et le TYPE d'exception — jamais les arguments.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "send_message" in message and "_Boom" in message
    assert _SECRET_BODY not in message and "42" not in message


def test_panne_du_store_apres_depot_impossible_bloque_aussi(monkeypatch):
    """Cas imbriqué : le dépôt échoue (chemin `error` existant) ET `store.mark` lève dans le
    `except` → avant, l'exception sortait de la callback (fail-open du cœur)."""
    monkeypatch.setattr(http_client, "post_json", _raise_boom)
    monkeypatch.setattr(store, "mark", _raise_boom)
    nxt = _NextCall()

    out = middleware.make_middleware()(tool_name="send_message", args=dict(_SEND_ARGS), next_call=nxt)

    _assert_blocked(out)
    assert nxt.calls == []


# ---------------------------------------------------------------------------
# (iii) contrôle positif : outil non-sortant → pass-through strictement inchangé.
# ---------------------------------------------------------------------------

def test_outil_non_sortant_passe_exactement_une_fois(posts):
    nxt = _NextCall(result="TOOL_RESULT")
    args = {"path": "/x"}

    out = middleware.make_middleware()(tool_name="write_file", args=args, next_call=nxt)

    assert out == "TOOL_RESULT"
    assert nxt.calls == [args] and nxt.calls[0] is args  # mêmes args, transmis tels quels
    assert posts == []


def test_exception_de_l_outil_reel_remonte_telle_quelle(posts):
    """La garde n'encadre PAS `next_call` : une panne de l'outil réel reste visible du cœur
    (qui la marque `_DownstreamExecutionError` et la relance à l'identique)."""
    def next_call(_a):
        raise _Boom("l'outil réel a planté")

    with pytest.raises(_Boom):
        middleware.make_middleware()(tool_name="write_file", args={"path": "/x"}, next_call=next_call)


def test_plugin_passif_hors_box_passe_meme_si_la_decision_planterait(monkeypatch):
    """Hors box (JB_DECISION_PUSH_URL absent) : pass-through, la classification n'est même pas appelée."""
    monkeypatch.delenv("JB_DECISION_PUSH_URL")
    monkeypatch.setattr(classify, "classify", _raise_boom)
    nxt = _NextCall()

    out = middleware.make_middleware()(tool_name="send_message", args=dict(_SEND_ARGS), next_call=nxt)

    assert out == "TOOL_RESULT" and len(nxt.calls) == 1


# ---------------------------------------------------------------------------
# (iv) contrôle positif : chemin nominal `queued_for_approval` byte-identique.
# ---------------------------------------------------------------------------

def test_chemin_nominal_queued_for_approval_inchange(posts):
    nxt = _NextCall()

    out = middleware.make_middleware()(tool_name="send_message", args=dict(_SEND_ARGS), next_call=nxt)

    payload = json.loads(out)
    jb_id = payload["id"]
    assert re.fullmatch(r"[0-9a-f]{32}", jb_id)
    # Byte-identique : même clés, même ordre, même texte, même sérialisation (ensure_ascii=False).
    assert out == json.dumps(
        {
            "status": "queued_for_approval",
            "id": jb_id,
            "message": "C'est prêt : j'ai préparé la proposition. Rien ne part tant que vous n'avez pas validé.",
        },
        ensure_ascii=False,
    )
    assert nxt.calls == []
    assert len(posts) == 1 and posts[0][1]["payload"]["jb_id"] == jb_id
    assert store.load(jb_id)["status"] == "pending"


# ---------------------------------------------------------------------------
# Par le VRAI runner du cœur : là où le fail-open mordait.
# ---------------------------------------------------------------------------

def _run_through_core(callback, tool_name: str, args: dict, terminal):
    """Rejoue exactement `run_tool_execution_middleware` avec notre seule callback enregistrée."""
    return _run_execution_chain(
        "tool_execution", [callback], terminal, tool_name=tool_name, args=args, original_args=args
    )


def test_via_le_runner_du_coeur_une_panne_interne_n_execute_pas_l_outil(posts, monkeypatch):
    monkeypatch.setattr(classify, "classify", _raise_boom)
    terminal = _NextCall(result="ENVOI PARTI")

    out = _run_through_core(middleware.make_middleware(), "send_message", dict(_SEND_ARGS), terminal)

    # Ancien code : la callback levait, le cœur appelait `call_at(index + 1)` → "ENVOI PARTI".
    assert out != "ENVOI PARTI"
    _assert_blocked(out)
    assert terminal.calls == []


def test_via_le_runner_du_coeur_le_pass_through_execute_l_outil(posts):
    terminal = _NextCall(result="LECTURE OK")

    out = _run_through_core(middleware.make_middleware(), "write_file", {"path": "/x"}, terminal)

    assert out == "LECTURE OK"
    assert terminal.calls == [{"path": "/x"}]
