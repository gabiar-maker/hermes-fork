"""Tests de l'attribution (stamp department/skill_id/job_id) et du fil d'activité — hooks natifs.

Autonomes comme test_jb_outbound.py : HTTP loopback mocké, pas d'environnement Hermes complet (sauf
les deux tests « par le vrai runner » et « par le vrai store cron », qui prouvent les seams du cœur).
Couvre : stamp présent en contexte job / absent hors contexte, résolution de la casquette depuis
le front-matter des skills (``casquette:`` gold, ``department:`` custom), gate ``JB_ACTIVITY_EVENTS``,
innocuité des échecs réseau, le filtrage ``platform == "cron"`` + format de session amont, la
rotation de session (alias ``turn_id``), et le pont hooks natifs → plugin (``on_session_start`` /
``on_session_end``), y compris à travers le runner BORNÉ du cœur (thread de travail, contexte copié).
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

# Rendre le paquet `jb_outbound` importable comme paquet de premier niveau (plugins/ sur le path).
_PLUGINS_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

import jb_outbound.activity as activity  # noqa: E402
import jb_outbound.http_client as http_client  # noqa: E402
import jb_outbound.job_context as job_context  # noqa: E402
import jb_outbound.middleware as middleware  # noqa: E402

# Id de session tel que l'amont le forge : f"cron_{job_id}_{%Y%m%d_%H%M%S}" (cron/scheduler.py).
JOB_ID = "a1b2c3d4e5f6"
SID = f"cron_{JOB_ID}_20260829_070000"
TURN = f"{SID}:task-1:deadbeef"
# Enfant de compression amont : f"{%Y%m%d_%H%M%S}_{uuid4().hex[:6]}" — plus de préfixe cron_.
CHILD_SID = "20260829_071500_ab12cd"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Isole HERMES_HOME, neutralise les gates JB et vide le registre pour CHAQUE test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("JB_DECISION_PUSH_URL", raising=False)
    monkeypatch.delenv("JB_ACTIVITY_EVENTS", raising=False)
    job_context._reset_for_tests()
    yield
    job_context._reset_for_tests()


@pytest.fixture
def posts(monkeypatch):
    """Active la boucle de proposition et capture tous les POST loopback (drafts + activité)."""
    monkeypatch.setenv("JB_DECISION_PUSH_URL", "http://127.0.0.1:8444/jb/decision")
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    captured: list = []
    monkeypatch.setattr(
        http_client, "post_json",
        lambda url, payload, timeout=10.0: captured.append((url, payload)) or 200,
    )
    return captured


@pytest.fixture
def store(monkeypatch):
    """Store cron simulé : `_load_job` (lecture seule) rend le job enregistré, sinon None."""
    jobs: dict = {}
    monkeypatch.setattr(job_context, "_load_job", lambda job_id: jobs.get(job_id))
    return jobs


def _write_skill(tmp_path, name: str, body: str) -> None:
    d = tmp_path / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


_GOLD = "---\nname: relance-devis\ncasquette: Le Commercial\n---\n\n# Relance devis\n"
_CUSTOM = "---\nname: veille-presse\ndepartment: Le Marketing\n---\n\n# Veille presse\n"
_SANS = "---\nname: tri-boite\ndescription: Trie la boîte mail.\n---\n\n# Tri\n"


def _job(**over) -> dict:
    job = {"id": JOB_ID, "name": "Relances du matin", "skills": ["relance-devis"]}
    job.update(over)
    return job


def _start(store: dict, job: dict | None = None, session_id: str = SID) -> None:
    """Le job existe dans le store, puis le cœur ouvre sa session cron (hook on_session_start)."""
    job = job if job is not None else _job()
    store[job["id"]] = job
    job_context.on_session_start(session_id=session_id, model="m", platform="cron")


def _end(session_id: str = SID, turn_id: str = "", **outcome) -> None:
    outcome = {"completed": True, "failed": False, "interrupted": False, **outcome}
    job_context.on_session_end(
        session_id=session_id, platform="cron", turn_id=turn_id, task_id="t", model="m",
        turn_exit_reason="text_response()", **outcome,
    )


def _propose(
    tool: str = "send_message",
    args: dict | None = None,
    session_id: str | None = SID,
    turn_id: str | None = None,
) -> dict:
    """Passe un appel d'envoi dans le middleware (court-circuit attendu) et rend le résultat.

    `session_id` / `turn_id` = ce que le cœur transmet au middleware (agent/tool_executor.py).
    """
    def next_call(_a):
        raise AssertionError("l'outil d'envoi NE doit PAS s'exécuter avant validation")

    return json.loads(
        middleware.make_middleware()(
            tool_name=tool, args=args or {"chat_id": "1", "content": "Hi"}, next_call=next_call,
            session_id=session_id, turn_id=turn_id, task_id="t", tool_call_id="c1",
        )
    )


def _drafts(posts) -> list:
    return [p[1] for p in posts if p[0].endswith("/v1/draft")]


def _activities(posts) -> list:
    return [p[1] for p in posts if p[0].endswith("/v1/activity")]


# ---------------------------------------------------------------------------
# Tâche 1 — stamp d'attribution sur les drafts
# ---------------------------------------------------------------------------

def test_stamp_draft_en_contexte_job(posts, store, tmp_path):
    _write_skill(tmp_path, "relance-devis", _GOLD)
    _start(store)

    _propose()
    draft = _drafts(posts)[-1]
    assert draft["department"] == "Le Commercial"
    assert draft["skill_id"] == "relance-devis"
    assert draft["job_id"] == JOB_ID
    # L'attribution est portée au premier niveau du DraftRequest, pas dans le payload round-trip.
    assert "department" not in draft["payload"]

    _end()


def test_stamp_absent_hors_contexte(posts):
    _propose(session_id="telegram-session-1")
    draft = _drafts(posts)[-1]
    for key in ("department", "skill_id", "job_id"):
        assert key not in draft  # chat libre → champs OMIS, pas de null


def test_stamp_efface_apres_job(posts, store, tmp_path):
    _write_skill(tmp_path, "relance-devis", _GOLD)
    _start(store)
    _end()

    _propose()
    assert "department" not in _drafts(posts)[-1]
    assert job_context.current(SID) is None


def test_deux_jobs_concurrents_ne_se_melangent_pas(posts, store, tmp_path):
    """Pool cron PARALLÈLE : chaque session ne voit que SA casquette (registre par session)."""
    _write_skill(tmp_path, "relance-devis", _GOLD)
    _write_skill(tmp_path, "veille-presse", _CUSTOM)
    other_sid = "cron_ffffffffffff_20260829_070001"
    _start(store)
    _start(store, _job(id="ffffffffffff", name="Veille", skills=["veille-presse"]), session_id=other_sid)

    _propose(session_id=other_sid)
    _propose(session_id=SID)
    a, b = _drafts(posts)[-2:]
    assert a["department"] == "Le Marketing" and a["job_id"] == "ffffffffffff"
    assert b["department"] == "Le Commercial" and b["job_id"] == JOB_ID


def test_casquette_gold_prioritaire_sur_department(posts, store, tmp_path):
    # Un skill qui porte les DEUX champs : `casquette:` (gold) gagne.
    _write_skill(tmp_path, "relance-devis", "---\ncasquette: Le Commercial\ndepartment: Autre\n---\n")
    _start(store)
    assert job_context.current(SID)["department"] == "Le Commercial"


def test_department_custom(posts, store, tmp_path):
    _write_skill(tmp_path, "veille-presse", _CUSTOM)
    _start(store, _job(skills=["veille-presse"]))
    ctx = job_context.current(SID)
    assert ctx["department"] == "Le Marketing"
    assert ctx["skill_id"] == "veille-presse"


def test_skill_sans_casquette_stamp_partiel(posts, store, tmp_path):
    _write_skill(tmp_path, "tri-boite", _SANS)
    _start(store, _job(skills=["tri-boite"]))

    _propose()
    draft = _drafts(posts)[-1]
    assert "department" not in draft  # pas de casquette déclarée → champ omis
    assert draft["skill_id"] == "tri-boite"  # attribution partielle conservée
    assert draft["job_id"] == JOB_ID


def test_resolution_skill_en_categorie(posts, store, tmp_path):
    # Skill rangé sous une catégorie (ex. casquettes/relance-devis), référencé par nom nu.
    _write_skill(tmp_path, "casquettes/relance-devis", _GOLD)
    _start(store)
    assert job_context.current(SID)["department"] == "Le Commercial"


def test_job_absent_du_store_stamp_job_id_seul(posts, store):
    """Le store ne connaît pas (plus) le job : on garde l'id (issu du session_id), rien d'autre."""
    job_context.on_session_start(session_id=SID, model="m", platform="cron")
    _propose()
    draft = _drafts(posts)[-1]
    assert draft["job_id"] == JOB_ID
    assert "department" not in draft and "skill_id" not in draft


def test_passif_sans_box_ni_activite(store):
    # Ni JB_DECISION_PUSH_URL ni JB_ACTIVITY_EVENTS → le hook n'enregistre rien.
    _start(store)
    assert job_context.current(SID) is None


# ---------------------------------------------------------------------------
# Filtrage : plateforme cron + format de session amont
# ---------------------------------------------------------------------------

def test_session_non_cron_ignoree(posts, store):
    store[JOB_ID] = _job()
    for platform in ("", "telegram", "subagent", "api_server"):
        job_context.on_session_start(session_id=SID, model="m", platform=platform)
    assert job_context.current(SID) is None
    assert _activities(posts) == []


def test_session_cron_au_format_inattendu_ignoree(posts, store):
    store[JOB_ID] = _job()
    for sid in ("", "cron_", f"cron_{JOB_ID}", CHILD_SID, "sess-1234"):
        job_context.on_session_start(session_id=sid, model="m", platform="cron")
        assert job_context.current(sid) is None


@pytest.mark.parametrize(
    "session_id, expected",
    [
        (SID, JOB_ID),
        ("cron_job_avec_underscores_20260829_070000", "job_avec_underscores"),  # suffixe = ancre
        ("cron_x_20260829_070000", "x"),
        (CHILD_SID, None),
        ("cron_a1b2_2026082_070000", None),  # horodatage incomplet
        (None, None),
        ("", None),
    ],
)
def test_job_id_from_session_id(session_id, expected):
    assert job_context.job_id_from_session_id(session_id) == expected


# ---------------------------------------------------------------------------
# Tâche 2 — fil d'activité (début/fin de job), gated par JB_ACTIVITY_EVENTS
# ---------------------------------------------------------------------------

def test_activity_off_par_defaut(posts, store, tmp_path):
    _write_skill(tmp_path, "relance-devis", _GOLD)
    _start(store)
    _end()
    assert _activities(posts) == []  # gate fermé → aucun évènement


def test_activity_on_emet_started_puis_finished(posts, store, tmp_path, monkeypatch):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    _write_skill(tmp_path, "relance-devis", _GOLD)

    _start(store)
    _end()

    events = _activities(posts)
    assert [e["phase"] for e in events] == ["started", "finished"]
    started, finished = events
    assert started["status"] == "ok" and finished["status"] == "ok"
    for e in events:
        assert e["department"] == "Le Commercial"
        assert e["skill_id"] == "relance-devis"
        assert e["job_id"] == JOB_ID
        assert e["label"] == "Relances du matin"  # nom lisible du job (jobs.json)
        assert e["correlation_id"] == JOB_ID


def test_activity_sans_boucle_de_proposition(monkeypatch, store, tmp_path):
    """JB_ACTIVITY_EVENTS seul (pas de JB_DECISION_PUSH_URL) suffit à émettre les signaux."""
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    captured: list = []
    monkeypatch.setattr(http_client, "post_json", lambda url, payload, timeout=10.0: captured.append((url, payload)) or 200)
    _start(store)
    _end()
    assert [e["phase"] for e in _activities(captured)] == ["started", "finished"]


@pytest.mark.parametrize(
    "outcome",
    [
        {"failed": True},
        {"interrupted": True},
        {"completed": False},  # tour sans réponse finale (budget épuisé…)
    ],
)
def test_activity_status_error_si_echec(posts, store, monkeypatch, outcome):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    _start(store, _job(skills=[]))
    _end(**outcome)

    finished = _activities(posts)[-1]
    assert finished["phase"] == "finished" and finished["status"] == "error"
    assert "department" not in finished and "skill_id" not in finished  # job sans skill → omis


def test_activity_fin_sans_debut_ignoree(posts, monkeypatch):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    _end(session_id="cron_inconnu_20260829_070000")
    assert _activities(posts) == []


def test_activity_echec_reseau_avale(monkeypatch, store, tmp_path):
    # Daemon injoignable (route /v1/activity inexistante, conteneur down…) : le job continue.
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    _write_skill(tmp_path, "relance-devis", _GOLD)

    def _boom(url, payload, timeout=10.0):
        raise ConnectionError("connexion refusée")

    monkeypatch.setattr(http_client, "post_json", _boom)
    _start(store)  # ne lève pas
    assert job_context.current(SID)["department"] == "Le Commercial"  # le contexte reste posé
    _end()  # ne lève pas
    assert job_context.current(SID) is None


def test_activity_emit_sans_contexte_ne_leve_pas(posts, monkeypatch):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    activity.emit("started", "ok", None)
    assert _activities(posts) == [{"phase": "started", "status": "ok"}]


# ---------------------------------------------------------------------------
# Rotation de session à la compression : alias turn_id
# ---------------------------------------------------------------------------

def test_rotation_de_session_retrouvee_par_turn_id(posts, store, tmp_path, monkeypatch):
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    _write_skill(tmp_path, "relance-devis", _GOLD)
    _start(store)

    # 1er appel d'outil (lecture, pass-through) : le middleware pose l'alias turn → session.
    middleware.make_middleware()(
        tool_name="read_file", args={"path": "/x"}, next_call=lambda a: "ok",
        session_id=SID, turn_id=TURN,
    )
    # Compression : l'amont fait tourner agent.session_id vers l'enfant ; le turn_id, lui, ne bouge pas.
    _propose(session_id=CHILD_SID, turn_id=TURN)
    assert _drafts(posts)[-1]["department"] == "Le Commercial"

    _end(session_id=CHILD_SID, turn_id=TURN)
    assert [e["phase"] for e in _activities(posts)] == ["started", "finished"]
    assert job_context.current(SID) is None
    assert job_context.current(turn_id=TURN) is None  # alias nettoyé avec la session


def test_rotation_sans_alias_perd_l_attribution(posts, store, tmp_path):
    """Limite documentée : rotation AVANT tout appel d'outil → pas d'alias → draft non stampé."""
    _write_skill(tmp_path, "relance-devis", _GOLD)
    _start(store)
    _propose(session_id=CHILD_SID, turn_id=TURN)
    assert "department" not in _drafts(posts)[-1]


def test_bind_turn_ignore_session_inconnue_et_ne_leve_pas():
    job_context.bind_turn("inconnue", TURN)
    job_context.bind_turn(None, TURN)
    job_context.bind_turn(SID, None)
    assert job_context.current(turn_id=TURN) is None


def test_eviction_ttl_du_registre(posts, store, monkeypatch):
    """Un job jamais « fini » (callback suspendue par le cœur…) ne fuit pas indéfiniment."""
    _start(store)
    monkeypatch.setattr(job_context, "_TTL_SECONDS", 0)
    with job_context._lock:
        ctx, _ = job_context._by_session[SID]
        job_context._by_session[SID] = (ctx, -10.0)
    _start(store, _job(id="ffffffffffff"), session_id="cron_ffffffffffff_20260829_070001")
    assert job_context.current(SID) is None


# ---------------------------------------------------------------------------
# Par le VRAI runner du cœur : hooks BORNÉS = thread de travail + contexte copié
# ---------------------------------------------------------------------------

def test_via_le_runner_du_coeur_le_registre_est_visible_du_thread_agent(posts, store, tmp_path, monkeypatch):
    """Le PluginManager exécute on_session_start/end dans un thread (copy_context) : une ContextVar
    posée là serait invisible du thread de l'agent — le registre par session, lui, l'est."""
    monkeypatch.setenv("JB_ACTIVITY_EVENTS", "1")
    _write_skill(tmp_path, "relance-devis", _GOLD)
    store[JOB_ID] = _job()

    from hermes_cli.plugins import PluginManager

    seen: dict = {}

    def _spy_start(**kw):
        seen["thread"] = threading.current_thread().name
        seen["kwargs"] = set(kw)
        return job_context.on_session_start(**kw)

    mgr = PluginManager(scope_key=str(tmp_path))
    mgr._hooks["on_session_start"] = [_spy_start]
    mgr._hooks["on_session_end"] = [job_context.on_session_end]

    # Kwargs EXACTS du site d'appel amont (agent/conversation_loop.py).
    mgr.invoke_hook("on_session_start", session_id=SID, model="m", platform="cron")

    assert seen["thread"] != threading.current_thread().name  # bien hors du thread appelant
    assert {"session_id", "model", "platform", "telemetry_schema_version"} <= seen["kwargs"]
    assert job_context.current(SID)["department"] == "Le Commercial"  # visible d'ICI (thread agent)
    _propose()
    assert _drafts(posts)[-1]["department"] == "Le Commercial"

    # Kwargs EXACTS du site d'appel amont (agent/turn_finalizer.py).
    mgr.invoke_hook(
        "on_session_end", session_id=SID, task_id="t", turn_id=TURN, completed=True, failed=False,
        interrupted=False, turn_exit_reason="text_response(1)", model="m", platform="cron",
    )
    assert [e["phase"] for e in _activities(posts)] == ["started", "finished"]
    assert job_context.current(SID) is None


def test_via_le_vrai_store_cron_get_job_lecture_seule(posts, tmp_path):
    """Sans mock de `_load_job` : le job est lu dans <HERMES_HOME>/cron/jobs.json par cron.jobs.get_job."""
    _write_skill(tmp_path, "relance-devis", _GOLD)
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    record = {
        "id": JOB_ID, "name": "Relances du matin", "skills": ["relance-devis"],
        "prompt": "relance", "schedule": {"kind": "cron", "expr": "0 7 * * *"}, "enabled": True,
    }
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": [record]}), encoding="utf-8")
    before = (cron_dir / "jobs.json").read_bytes()

    job_context.on_session_start(session_id=SID, model="m", platform="cron")

    ctx = job_context.current(SID)
    assert ctx["department"] == "Le Commercial" and ctx["label"] == "Relances du matin"
    assert (cron_dir / "jobs.json").read_bytes() == before  # lecture SEULE : rien réécrit
