"""Tests autonomes du plugin jb_outbound.

Ne nécessitent PAS un environnement Hermes complet : le HTTP loopback et le registre d'outils
sont mockés. Couvre la classification, le mapping, l'interception (pass / propose / block) et le
replay (exécution + idempotence).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

# Rendre le paquet `jb_outbound` importable comme paquet de premier niveau (plugins/ sur le path).
_PLUGINS_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

# Racine du fork sur le path : nécessaire pour importer `tools.mcp_tool` (le nommage RUNTIME
# des outils MCP, source de vérité que ce test importe plutôt que de le supposer — cf.
# test_prefixe_composio_matche_le_nommage_runtime_mcp_tool ci-dessous).
_FORK_ROOT = Path(__file__).resolve().parents[2]
if str(_FORK_ROOT) not in sys.path:
    sys.path.insert(0, str(_FORK_ROOT))

import jb_outbound.classify as classify  # noqa: E402
import jb_outbound.http_client as http_client  # noqa: E402
import jb_outbound.mapping as mapping  # noqa: E402
import jb_outbound.middleware as middleware  # noqa: E402
import jb_outbound.replay as replay  # noqa: E402
import jb_outbound.store as store  # noqa: E402
from tools.mcp_tool import mcp_prefixed_tool_name  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Isole HERMES_HOME pour CHAQUE test → la table managée lue est celle du tmp_path (jamais ~/.hermes)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _write_managed(tmp_path, write_tools: dict) -> None:
    """Dépose la table managée `<HERMES_HOME>/jb_outbound/managed.json` (forme du bundle JB)."""
    d = tmp_path / "jb_outbound"
    d.mkdir(parents=True, exist_ok=True)
    (d / "managed.json").write_text(
        json.dumps({"version": 1, "writeTools": write_tools}), encoding="utf-8"
    )


# Une action MCP additionnelle typique (serveur `x-crm`, outil `create_contact`). Les clés de
# `managed.json` sont posées par le portail avec le nom RUNTIME que Hermes enregistre réellement
# (`tools.mcp_tool.mcp_prefixed_tool_name`) — on la DÉRIVE par import, jamais en littéral, pour ne
# pas rejouer le bug C1 (le portail et le fork divergent silencieusement sur le nommage).
_CRM_TOOL_NAME = mcp_prefixed_tool_name("x-crm", "create_contact")
_CRM_ACTION = {_CRM_TOOL_NAME: {"label": "Créer un contact", "kind": "action"}}


@pytest.fixture
def posts(tmp_path, monkeypatch):
    monkeypatch.setenv("JB_DECISION_PUSH_URL", "http://127.0.0.1:8444/jb/decision")
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    captured: list = []
    monkeypatch.setattr(
        http_client, "post_json",
        lambda url, payload, timeout=10.0: captured.append((url, payload)) or 200,
    )
    return captured


def test_classify():
    assert classify.classify("send_message") == classify.PROPOSE
    assert classify.classify("mcp__composio__GMAIL_SEND_EMAIL") == classify.PROPOSE
    assert classify.classify("mcp__composio__LINKEDIN_CREATE_POST") == classify.PROPOSE
    assert classify.classify("mcp__composio__GMAIL_FETCH_MESSAGES") == classify.PASS
    assert classify.classify("mcp__composio__UNCLASSIFIED_THING") == classify.BLOCK  # fail-closed
    assert classify.classify("write_file") == classify.PASS


def test_prefixe_composio_matche_le_nommage_runtime_mcp_tool():
    """Contrat verrouillé : le préfixe que `classify.py` reconnaît DOIT être exactement celui que
    Hermes assigne réellement aux outils du serveur MCP `composio` (`tools/mcp_tool.py`). Ce test
    IMPORTE `mcp_prefixed_tool_name` au lieu de supposer un format littéral — il devient ROUGE si
    l'un des deux côtés (classify.py ou mcp_tool.py) change son nommage sans l'autre (c'est
    exactement le bug C1 : `mcp_composio_*` vs `mcp__composio__*`)."""
    full_name = mcp_prefixed_tool_name("composio", "GMAIL_SEND_EMAIL")
    assert full_name == "mcp__composio__GMAIL_SEND_EMAIL"  # sanity : ce que Hermes produit vraiment

    # (a) le préfixe que classify.py reconnaît est un préfixe EXACT du nom runtime.
    assert full_name.startswith(classify._COMPOSIO_PREFIX)
    assert classify._COMPOSIO_PREFIX == full_name.removesuffix("GMAIL_SEND_EMAIL")

    # (b) bout en bout : un vrai nom runtime (généré, pas littéral) est classé correctement.
    assert classify.classify(mcp_prefixed_tool_name("composio", "GMAIL_SEND_EMAIL")) == classify.PROPOSE
    assert classify.classify(mcp_prefixed_tool_name("composio", "GMAIL_FETCH_MESSAGES")) == classify.PASS
    assert classify.classify(mcp_prefixed_tool_name("composio", "UNCLASSIFIED_THING")) == classify.BLOCK


def test_classify_browser_lecture_passe_ecriture_propose():
    # Lecture / navigation / observation : aucun changement d'état → passe.
    for read_tool in (
        "browser_navigate",
        "browser_snapshot",
        "browser_vision",
        "browser_get_images",
        "browser_scroll",
        "browser_back",
    ):
        assert classify.classify(read_tool) == classify.PASS, read_tool

    # Écriture / action d'état / soumission : clic, saisie, touche → proposition à valider.
    for write_tool in ("browser_click", "browser_type", "browser_press"):
        assert classify.classify(write_tool) == classify.PROPOSE, write_tool

    # Outils puissants (JS console, CDP, dialogue) et tout `browser_*` inconnu : proposition (fail-safe).
    for risky_tool in ("browser_console", "browser_cdp", "browser_dialog", "browser_unknown_future"):
        assert classify.classify(risky_tool) == classify.PROPOSE, risky_tool


def test_mapping_email_minimise_le_corps():
    d = mapping.to_draft(
        "mcp__composio__GMAIL_SEND_EMAIL",
        {"recipient_email": "marie@ex.fr", "subject": "Votre devis", "body": "Bonjour Marie, ..."},
    )
    assert d["kind"] == "email"
    assert d["to"] == "marie@ex.fr"
    assert "Votre devis" in d["title"]
    assert "Bonjour Marie" in d["preview"]
    assert "body" not in d["payload"] and "marie@ex.fr" not in json.dumps(d["payload"])


def test_mapping_telegram():
    d = mapping.to_draft("send_message", {"chat_id": "123", "content": "Salut"})
    assert d["kind"] == "sms"
    assert d["to"] == "123"


def test_middleware_pass_through_un_outil_interne(posts):
    seen = {}

    def next_call(a):
        seen["args"] = a
        return "TOOL_RESULT"

    out = middleware.make_middleware()(tool_name="write_file", args={"path": "/x"}, next_call=next_call)
    assert out == "TOOL_RESULT"
    assert seen["args"] == {"path": "/x"}
    assert posts == []  # rien n'a été déposé


def test_middleware_propose_court_circuite_et_depose(posts):
    def next_call(_a):
        raise AssertionError("l'outil d'envoi NE doit PAS s'exécuter avant validation")

    out = json.loads(
        middleware.make_middleware()(
            tool_name="send_message", args={"chat_id": "1", "content": "Hi"}, next_call=next_call
        )
    )
    assert out["status"] == "queued_for_approval"
    jb_id = out["id"]

    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/v1/draft")
    assert body["kind"] == "sms"
    assert body["payload"]["jb_id"] == jb_id  # corrélation décision ↔ envoi

    rec = store.load(jb_id)
    assert rec["status"] == "pending"
    assert rec["args"] == {"chat_id": "1", "content": "Hi"}  # args complets gardés localement


def test_middleware_block_fail_closed(posts):
    def next_call(_a):
        raise AssertionError("un envoi non répertorié NE doit PAS s'exécuter")

    out = json.loads(
        middleware.make_middleware()(
            tool_name="mcp__composio__UNCLASSIFIED_THING", args={}, next_call=next_call
        )
    )
    assert out["status"] == "blocked"
    assert posts == []


def test_replay_execute_puis_idempotent(posts, monkeypatch):
    store.save("abc123", "mcp__composio__GMAIL_SEND_EMAIL", {"recipient_email": "m@ex.fr", "body": "hi"}, "email", "m@ex.fr")

    dispatched: list = []
    fake_registry = types.SimpleNamespace(
        dispatch=lambda name, args: dispatched.append((name, args)) or "{}"
    )
    tools_mod = types.ModuleType("tools")
    reg_mod = types.ModuleType("tools.registry")
    reg_mod.registry = fake_registry  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.registry", reg_mod)

    decision = {"id": "prop-1", "kind": "email", "payload": {"jb_id": "abc123"}}
    replay.handle_decision(decision)

    # envoi RÉEL rejoué avec les args exacts
    assert dispatched == [("mcp__composio__GMAIL_SEND_EMAIL", {"recipient_email": "m@ex.fr", "body": "hi"})]
    # résultat remonté : executed, id = id de proposition (round-trip control-plane)
    results = [p for p in posts if p[0].endswith("/v1/result")]
    assert results and results[-1][1] == {"id": "prop-1", "status": "executed", "error": ""}
    assert store.load("abc123")["status"] == "executed"

    # rejouer la même décision → idempotent (aucun nouvel envoi)
    replay.handle_decision(decision)
    assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# MCP additionnels (hors Composio) : pas de blocage ; actions → dashboard.
# ---------------------------------------------------------------------------

def test_classify_action_mcp_additionnel(tmp_path):
    _write_managed(tmp_path, _CRM_ACTION)
    # Action déclarée → proposition (dashboard).
    assert classify.classify(_CRM_TOOL_NAME) == classify.PROPOSE
    # Lecture du MÊME serveur, non déclarée comme action → PASS (aucun blocage, conforme à la consigne).
    assert classify.classify(mcp_prefixed_tool_name("x-crm", "find_contact")) == classify.PASS


def test_classify_sans_table_managee_ne_bloque_pas(tmp_path):
    # Pas de managed.json → un outil de MCP additionnel reste une lecture (PASS), jamais bloqué.
    assert classify.classify(_CRM_TOOL_NAME) == classify.PASS


def test_mapping_action_utilise_le_libelle_white_label(tmp_path):
    _write_managed(tmp_path, _CRM_ACTION)
    d = mapping.to_draft(_CRM_TOOL_NAME, {"name": "Marie", "note": "rappeler le devis"})
    assert d["kind"] == "action"
    assert d["title"] == "Créer un contact"  # libellé opérateur
    # White-label : le nom technique de l'outil ne fuit nulle part dans la proposition.
    assert "x_crm" not in json.dumps(d, ensure_ascii=False)


def test_middleware_action_court_circuite_et_depose(posts, tmp_path):
    _write_managed(tmp_path, _CRM_ACTION)

    def next_call(_a):
        raise AssertionError("une action MCP NE doit PAS s'exécuter avant validation")

    out = json.loads(
        middleware.make_middleware()(
            tool_name=_CRM_TOOL_NAME, args={"name": "Marie"}, next_call=next_call
        )
    )
    assert out["status"] == "queued_for_approval"
    assert len(posts) == 1
    _, body = posts[0]
    assert body["kind"] == "action"
    assert body["title"] == "Créer un contact"

    # Les args complets restent LOCAUX pour le replay ; le payload relayé n'en porte pas (RGPD).
    rec = store.load(out["id"])
    assert rec["args"] == {"name": "Marie"} and rec["kind"] == "action"
    assert "Marie" not in json.dumps(body["payload"], ensure_ascii=False)
