"""Contexte d'attribution du job cron courant (casquette / skill / job) — hooks natifs 0.20.6.

Alimenté par les hooks plugin ``on_session_start`` / ``on_session_end`` du cœur (filtrés sur
``platform == "cron"``), lu par le middleware (``middleware.py``) pour estampiller les DraftRequest
avec le « département » de la tâche : ``department`` / ``skill_id`` / ``job_id``. Plus AUCUN patch
de ``cron/scheduler.py`` (F2, 2026-08-29) : le fichier est redevenu byte-identique à l'amont.

Comment on retrouve le job : le scheduler amont nomme la session ``cron_{job_id}_{YYYYmmdd_HHMMSS}``
(``cron/scheduler.py``, ``_cron_session_id``). ``on_session_start`` reçoit cet id et ``platform``,
en extrait ``job_id`` (regex sur le SUFFIXE horodaté — l'id de job amont est ``uuid4().hex[:12]``,
sans underscore, mais la regex tolère n'importe quel id) et lit le job dans le store cron par
``cron.jobs.get_job`` (lecture seule) pour résoudre skills → casquette.

Pourquoi un REGISTRE par session et pas une ``ContextVar`` : ``on_session_start``/``on_session_end``
sont des hooks BORNÉS du cœur (``_HOOK_TIMEOUT_BOUNDED_HOOKS``) — le PluginManager les exécute dans un
thread de travail sur une COPIE du contexte (``contextvars.copy_context().run``) : une ContextVar
posée dans la callback ne serait jamais visible du thread de l'agent. Le middleware, lui, reçoit
``session_id`` et ``turn_id`` du cœur à CHAQUE appel d'outil (``agent/tool_executor.py`` →
``run_tool_execution_middleware``) : il retrouve le contexte par ``session_id``.

Rotation de session à la compression : si le contexte est compressé PENDANT le job, l'amont fait
tourner ``agent.session_id`` vers un enfant ``{YYYYmmdd_HHMMSS}_{hex6}`` (plus de préfixe ``cron_``).
Le middleware pose donc, au premier appel d'outil (toujours avant une rotation : celle-ci n'arrive
qu'après accumulation de résultats d'outils), un ALIAS ``turn_id → session_id`` — le ``turn_id`` est
stable sur tout le ``run_conversation`` et transmis au middleware comme à ``on_session_end``. Après
rotation, drafts et signal « finished » sont retrouvés par ``turn_id``. Limite résiduelle : un job
compressé AVANT tout appel d'outil (cas théorique : prompt initial déjà au-delà du seuil) perdrait
son attribution après rotation et son signal de fin.

Ce qui n'est PLUS signalé (par rapport à l'ancien wrapper ``run_job``) : un job qui échoue AVANT de
créer l'agent (script sans sortie, blocage par le scanner d'injection cron, préflight, garde
d'exfiltration) n'ouvre aucune session → ni « started » ni « finished » ; et l'issue de la
LIVRAISON post-tour n'est pas reflétée (le statut vient de l'issue du tour : ``completed`` /
``failed`` / ``interrupted``).

Résolution best-effort : toute erreur → champs absents, jamais d'exception — l'attribution ne doit
JAMAIS faire échouer un job (les hooks sont de toute façon isolés par le cœur).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Format amont de l'id de session cron (cron/scheduler.py : f"cron_{job_id}_{%Y%m%d_%H%M%S}").
_SESSION_RE = re.compile(r"^cron_(?P<job_id>.+)_\d{8}_\d{6}$")

# Filet anti-fuite : une entrée sans « finished » (callback suspendue par le cœur après un timeout,
# rechargement des plugins…) est évincée après ce délai. Un job cron dure au plus quelques heures.
_TTL_SECONDS = 6 * 3600

_lock = threading.Lock()
# session_id → (ctx, posé_à)
_by_session: Dict[str, Tuple[Dict[str, Any], float]] = {}
# turn_id → session_id (alias posé par le middleware, cf. docstring du module)
_by_turn: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# API lue par le middleware
# ---------------------------------------------------------------------------

def current(session_id: Optional[str] = None, turn_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Contexte d'attribution du job cron courant, ou ``None`` hors job cron (chat libre).

    Recherche par ``session_id`` (transmis par le cœur au middleware), puis par ``turn_id`` (alias,
    survit à la rotation de session). Renvoie une COPIE : le registre n'est jamais muté par un lecteur.
    """
    with _lock:
        entry = _lookup_locked(session_id, turn_id)
    return dict(entry[0]) if entry else None


def bind_turn(session_id: Optional[str], turn_id: Optional[str]) -> None:
    """Alias ``turn_id → session_id`` (appelé par le middleware à chaque appel d'outil). N'échoue jamais."""
    if not session_id or not turn_id:
        return
    try:
        with _lock:
            if session_id in _by_session and turn_id not in _by_turn:
                _by_turn[turn_id] = session_id
    except Exception:
        logger.debug("jb_outbound: bind_turn en échec (ignoré)", exc_info=True)


# ---------------------------------------------------------------------------
# Hooks natifs (enregistrés dans __init__.py : on_session_start / on_session_end)
# ---------------------------------------------------------------------------

def on_session_start(*, session_id: str = "", platform: str = "", **_: Any) -> None:
    """Début d'une session cron : pose le contexte d'attribution et signale le début du job.

    Ignore toute session non-cron (chat, sous-agents ``platform="subagent"``…) et tout id qui ne
    porte pas le format amont. Passif si le plugin n'a ni boucle de proposition
    (``JB_DECISION_PUSH_URL``) ni fil d'activité (``JB_ACTIVITY_EVENTS``).
    """
    try:
        if platform != "cron":
            return
        job_id = job_id_from_session_id(session_id)
        if job_id is None:
            return
        from . import activity, config

        if not (config.enabled() or activity.enabled()):
            return
        ctx = _build_ctx(_load_job(job_id) or {"id": job_id})
        now = time.monotonic()
        with _lock:
            _evict_expired_locked(now)
            _by_session[session_id] = (ctx, now)
        activity.emit("started", "ok", ctx)
    except Exception:
        logger.debug("jb_outbound: on_session_start en échec (ignoré)", exc_info=True)


def on_session_end(
    *,
    session_id: str = "",
    platform: str = "",
    turn_id: str = "",
    completed: Optional[bool] = None,
    failed: Optional[bool] = None,
    interrupted: Optional[bool] = None,
    **_: Any,
) -> None:
    """Fin du tour cron : signale la fin du job et retire le contexte du registre.

    Retrouvé par ``session_id`` ou, après rotation à la compression, par ``turn_id``. Le statut est
    « ok » si le tour s'est achevé (``completed``) sans ``failed`` ni ``interrupted``.
    """
    try:
        if platform != "cron":
            return
        with _lock:
            entry = _lookup_locked(session_id, turn_id)
            if entry is None:
                return
            ctx = entry[0]
            _forget_locked(_session_id_of_locked(ctx, session_id, turn_id))
        from . import activity

        ok = not failed and not interrupted and (completed if completed is not None else True)
        activity.emit("finished", "ok" if ok else "error", ctx)
    except Exception:
        logger.debug("jb_outbound: on_session_end en échec (ignoré)", exc_info=True)


# ---------------------------------------------------------------------------
# Registre (helpers sous verrou)
# ---------------------------------------------------------------------------

def _lookup_locked(session_id: Optional[str], turn_id: Optional[str]) -> Optional[Tuple[Dict[str, Any], float]]:
    if session_id and session_id in _by_session:
        return _by_session[session_id]
    if turn_id:
        sid = _by_turn.get(turn_id)
        if sid and sid in _by_session:
            return _by_session[sid]
    return None


def _session_id_of_locked(ctx: Dict[str, Any], session_id: Optional[str], turn_id: Optional[str]) -> Optional[str]:
    if session_id and session_id in _by_session and _by_session[session_id][0] is ctx:
        return session_id
    if turn_id:
        return _by_turn.get(turn_id)
    return None


def _forget_locked(ctx_session_id: Optional[str]) -> None:
    if not ctx_session_id:
        return
    _by_session.pop(ctx_session_id, None)
    for tid in [t for t, s in _by_turn.items() if s == ctx_session_id]:
        _by_turn.pop(tid, None)


def _evict_expired_locked(now: float) -> None:
    stale = [sid for sid, (_, at) in _by_session.items() if now - at > _TTL_SECONDS]
    for sid in stale:
        _forget_locked(sid)


def _reset_for_tests() -> None:
    with _lock:
        _by_session.clear()
        _by_turn.clear()


# ---------------------------------------------------------------------------
# Résolution du job (session_id → job_id → enregistrement du store cron)
# ---------------------------------------------------------------------------

def job_id_from_session_id(session_id: Optional[str]) -> Optional[str]:
    """``cron_{job_id}_{YYYYmmdd_HHMMSS}`` → ``job_id`` ; ``None`` pour tout autre format."""
    if not session_id:
        return None
    m = _SESSION_RE.match(str(session_id))
    return m.group("job_id") if m else None


def _load_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Enregistrement du job dans le store cron (``cron.jobs.get_job``, LECTURE SEULE). Best-effort."""
    try:
        from cron.jobs import get_job

        return get_job(job_id)
    except Exception:
        logger.debug("jb_outbound: job cron %s introuvable dans le store (ignoré)", job_id, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Construction du contexte (job → {job_id, skill_id, department, label})
# ---------------------------------------------------------------------------

def _build_ctx(job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    job = job or {}
    skills = _skill_names(job)
    department, skill_id = _resolve_department(skills)
    job_id = str(job.get("id") or "").strip() or None
    return {
        "job_id": job_id,
        "skill_id": skill_id,
        "department": department,
        "label": str(job.get("name") or "").strip() or None,
        # D2 : appariement started↔finished du job dans le fil temps réel (homogène avec les délégations).
        "correlation_id": job_id,
    }


def _skill_names(job: Dict[str, Any]) -> List[str]:
    """Skills du job, dans l'ordre (champ canonique ``skills``, repli legacy ``skill``)."""
    raw = job.get("skills")
    if raw is None:
        raw = [job.get("skill")] if job.get("skill") else []
    elif isinstance(raw, str):
        raw = [raw]
    out: List[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _resolve_department(skills: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """(department, skill_id) du job : premier skill qui déclare une casquette.

    ``casquette:`` (gold) est lu avant ``department:`` (custom). Si aucun skill ne déclare de
    département, ``skill_id`` retombe sur le premier skill du job (attribution partielle).
    """
    fallback = skills[0] if skills else None
    for name in skills:
        try:
            fm = _skill_frontmatter(name)
        except Exception:
            continue
        for key in ("casquette", "department"):
            value = fm.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), name
    return None, fallback


def _home() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))


def _find_skill_md(name: str) -> Optional[Path]:
    """Localise le fichier d'un skill sous ``<HERMES_HOME>/skills`` (miroir allégé de skills_tool).

    Stratégies : chemin direct (``name/SKILL.md``, couvre aussi ``catégorie/name``), fichier plat
    ``name.md``, puis recherche récursive par nom de dossier. Refuse toute forme de traversée.
    """
    if not name or ".." in name.replace("\\", "/").split("/") or Path(name).is_absolute() or Path(name).drive:
        return None
    skills_dir = _home() / "skills"
    if not skills_dir.is_dir():
        return None
    direct = skills_dir / name
    if (direct / "SKILL.md").is_file():
        return direct / "SKILL.md"
    if direct.with_suffix(".md").is_file():
        return direct.with_suffix(".md")
    leaf = name.replace("\\", "/").split("/")[-1]
    for cand in skills_dir.rglob("SKILL.md"):
        if cand.parent.name == leaf:
            return cand
    for cand in skills_dir.rglob(f"{leaf}.md"):
        if cand.name != "SKILL.md":
            return cand
    return None


def _skill_frontmatter(name: str) -> Dict[str, str]:
    path = _find_skill_md(name)
    if path is None:
        return {}
    try:
        return _parse_simple_frontmatter(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_simple_frontmatter(text: str) -> Dict[str, str]:
    """Extraction minimale du front-matter YAML : clés scalaires de premier niveau.

    Suffisant pour ``casquette:`` / ``department:`` — pas de dépendance yaml ni du cœur Hermes
    (le plugin reste autonome, même esprit que ``http_client.py``). Les lignes indentées (blocs,
    listes) sont ignorées.
    """
    if not text.startswith("---"):
        return {}
    end = re.search(r"\n---\s*(\n|$)", text[3:])
    if not end:
        return {}
    out: Dict[str, str] = {}
    for line in text[3 : end.start() + 3].splitlines():
        if not line.strip() or line[:1] in (" ", "\t") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip().strip("'\"")
    return out
