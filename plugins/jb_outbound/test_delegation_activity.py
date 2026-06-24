"""Tests D2.2 — bus de délégation → fil d'activité (D2) + contributeurs du draft (D3).

Autonomes (HTTP loopback mocké, pas d'environnement Hermes complet). Couvre : émission started/finished
par sous-agent TAGUÉ + correlation_id ; sanitization du département (white-label) ; gate
JB_ACTIVITY_EVENTS (D2) vs enregistrement des contributeurs (D3, NON gaté) ; accumulation lead+supports
dans le draft (règle ≥ 2) + reset ; promotion du lead en chat libre ; correlation_id = job_id côté cron (L4).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Rendre `jb_outbound` importable comme paquet de premier niveau (plugins/ sur le path).
_PLUGINS_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

import jb_outbound.activity as activity  # noqa: E402,F401
import jb_outbound.contributions as contributions  # noqa: E402
import jb_outbound.delegation_activity as da  # noqa: E402
import jb_outbound.http_client as http_client  # noqa: E402
import jb_outbound.job_context as job_context  # noqa: E402
import jb_outbound.middleware as middleware  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("JB_DECISION_PUSH_URL", raising=False)
    monkeypatch.delenv("JB_ACTIVITY_EVENTS", raising=False)
    job_context._JOB_CTX.set(None)
    contributions.reset()


@pytest.fixture
def posts(monkeypatch):
    monkeypatch.setenv("JB_DECISION_PUSH_URL", "http://127.0.0.1:8444/jb/decision")
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    captured: list = []
    monkeypatch.setattr(
        http_client, "post_json",
        lambda url, payload, timeout=10.0: captured.append((url, payload)) or 200,
    )
    return captured


def _activities(posts):
    return [p[1] for p in posts if p[0].endswith("/v1/activity")]


def _drafts(posts):
    return [p[1] for p in posts if p[0].endswith("/v1/draft")]


def _write_skill(tmp_path, name, body):
    d = tmp_path / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def _propose(tool="send_message", args=None):
    def next_call(_a):
        raise AssertionError("l'outil d'envoi NE doit PAS s'exécuter avant validation")

    return json.loads(
        middleware.make_middleware()(
            tool_name=tool, args=args or {"chat_id": "1", "content": "Hi"}, next_call=next_call
        )
    )


# --------------------------- D2 : émission sur le bus ---------------------------

def test_subagent_start_emet_started_avec_correlation(posts, monkeypatch):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    da.on_subagent_start(child_subagent_id="sa-0-abcd1234", child_department="Comptable")
    ev = _activities(posts)
    assert len(ev) == 1
    assert ev[0] == {
        "phase": "started",
        "status": "ok",
        "department": "comptable",  # sanitisé en minuscules
        "correlation_id": "sa-0-abcd1234",
    }


def test_subagent_stop_status_mapping(posts, monkeypatch):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    da.on_subagent_stop(child_subagent_id="sa-0", child_department="comptable", child_status="completed")
    da.on_subagent_stop(child_subagent_id="sa-1", child_department="comptable", child_status="failed")
    ev = _activities(posts)
    assert ev[0]["phase"] == "finished" and ev[0]["status"] == "ok"
    assert ev[1]["status"] == "error"  # tout sauf completed/None → error


def test_departement_non_tague_ou_exotique_ignore(posts, monkeypatch):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    da.on_subagent_start(child_subagent_id="sa-0", child_department=None)
    da.on_subagent_start(child_subagent_id="sa-1", child_department="n'importe quoi !")
    assert _activities(posts) == []  # ni event…
    assert contributions.snapshot() == []  # …ni contributeur (pas de casquette inventée)


def test_gate_off_pas_activite_mais_contributeur_enregistre(posts):
    # JB_ACTIVITY_EVENTS absent → PAS d'activité (D2 gaté) MAIS le contributeur est enregistré (D3 non gaté).
    da.on_subagent_start(child_subagent_id="sa-0", child_department="comptable")
    assert _activities(posts) == []
    assert contributions.snapshot() == [{"department": "comptable", "role": "support"}]


# --------------------------- D3 : contributeurs du draft ---------------------------

def test_draft_contributors_lead_plus_support(posts, tmp_path):
    _write_skill(tmp_path, "relance-devis", "---\nname: relance-devis\ncasquette: commercial\n---\n")
    token = job_context.job_started({"id": "j1", "name": "Relances", "skills": ["relance-devis"]})
    da.on_subagent_start(child_subagent_id="sa-0", child_department="comptable")  # une délégation a contribué
    _propose()
    draft = _drafts(posts)[-1]
    assert draft["department"] == "commercial"  # lead (legacy, inchangé)
    assert draft["contributors"] == [
        {"department": "commercial", "role": "lead"},
        {"department": "comptable", "role": "support"},
    ]
    # reset : un 2e envoi sans nouvelle délégation → plus de contributors (lead seul < 2)
    _propose()
    assert "contributors" not in _drafts(posts)[-1]
    job_context.job_finished({"id": "j1"}, token=token)


def test_draft_mono_casquette_pas_de_contributors(posts, tmp_path):
    _write_skill(tmp_path, "relance-devis", "---\ncasquette: commercial\n---\n")
    token = job_context.job_started({"id": "j1", "skills": ["relance-devis"]})
    _propose()  # aucune délégation
    assert "contributors" not in _drafts(posts)[-1]
    job_context.job_finished({"id": "j1"}, token=token)


def test_chat_libre_promotion_premier_support_en_lead(posts):
    # Pas de job_context (chat libre) + 2 délégations taguées → 1er promu lead, l'autre support.
    da.on_subagent_start(child_subagent_id="sa-0", child_department="comptable")
    da.on_subagent_start(child_subagent_id="sa-1", child_department="commercial")
    _propose()
    assert _drafts(posts)[-1]["contributors"] == [
        {"department": "comptable", "role": "lead"},
        {"department": "commercial", "role": "support"},
    ]


# --------------------------- L4 : correlation_id côté cron ---------------------------

def test_cron_event_porte_correlation_id_egal_job_id(posts, tmp_path, monkeypatch):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    _write_skill(tmp_path, "relance-devis", "---\ncasquette: commercial\n---\n")
    job = {"id": "job-42", "name": "Relances", "skills": ["relance-devis"]}
    token = job_context.job_started(job)
    job_context.job_finished(job, token=token)
    assert [e["correlation_id"] for e in _activities(posts)] == ["job-42", "job-42"]
