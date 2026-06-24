"""Bus de délégation → fil d'activité Jean-Billie (D2) + contributeurs (D3).

Branché sur les hooks natifs `subagent_start`/`subagent_stop` (invoqués sur le thread PARENT, une fois
chacun par sous-agent → throttling structurel : JAMAIS par appel d'outil). Pour un sous-agent TAGUÉ
d'une casquette (`department`, posé par `delegate_task`), on :
  - D2 : émet un événement d'activité `started`/`finished`, apparié par `correlation_id` (= subagent_id) ;
  - D3 : enregistre le département comme contributeur du tour (en plus du lead = job_context du parent).

White-label : on ne manipule QUE l'id de département (stable, sanitisé) — JAMAIS le goal/objectif LLM,
ni un slug technique. Une délégation NON taguée (l'agent a délégué seul, sans casquette prévue) n'émet
rien et n'enregistre rien : on n'invente pas de casquette. L'émission d'activité est gatée
`JB_ACTIVITY_EVENTS=1` (cf. `activity.enabled()`) ; l'enregistrement des contributeurs, lui, est de la
simple attribution (comme le `department` du draft) et n'est pas gaté.
"""

from __future__ import annotations

import re
from typing import Optional

_DEPT_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _safe_department(value) -> Optional[str]:
    """Normalise un id de département (minuscules, slug-like) ; None si vide/exotique (jamais émis brut)."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if _DEPT_RE.match(v) else None


def on_subagent_start(*, child_subagent_id=None, child_department=None, **_) -> None:
    """Une casquette commence une sous-mission : contributeur du tour (D3) + signal « au travail » (D2)."""
    from . import activity, contributions

    dept = _safe_department(child_department)
    if dept is None:
        return  # délégation non taguée → ni attribution ni activité (pas de casquette inventée)
    contributions.record(dept, role="support")
    activity.emit("started", "ok", {"department": dept, "correlation_id": child_subagent_id})


def on_subagent_stop(*, child_subagent_id=None, child_department=None, child_status=None, **_) -> None:
    """Une casquette a fini sa sous-mission : signal de fin (D2), apparié au `started` par correlation_id."""
    from . import activity

    dept = _safe_department(child_department)
    if dept is None:
        return
    status = "ok" if child_status in ("completed", None) else "error"
    activity.emit("finished", status, {"department": dept, "correlation_id": child_subagent_id})
