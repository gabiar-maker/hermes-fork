"""Registre des contributeurs du tour courant (D3, attribution multi-rôles).

Quand l'assistant délègue à plusieurs casquettes AVANT de produire un envoi (draft), on accumule ici
les départements ayant contribué — pour les poser dans `contributors` du DraftRequest. Un ContextVar :
alimenté par le hook `subagent_start` (sur le thread PARENT), lu puis réinitialisé par le middleware au
moment du dépôt du draft. Purement informatif, best-effort : ne bloque ni n'échoue jamais un envoi.

Pourquoi un registre du TOUR (et pas un snapshot des sous-agents actifs) : un sous-agent peut avoir
TERMINÉ avant que le parent produise le draft (il délègue, récupère le résultat, PUIS envoie).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, List, Optional

_CONTRIB: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar("jb_contrib", default=None)


def record(department: Optional[str], role: str = "support", skill_id: Optional[str] = None) -> None:
    """Mémorise une casquette contributrice pour le tour courant (dédupliquée par département)."""
    if not department:
        return
    lst = _CONTRIB.get()
    if lst is None:
        lst = []
        _CONTRIB.set(lst)
    if any(c.get("department") == department for c in lst):
        return
    entry: Dict[str, Any] = {"department": department, "role": role}
    if skill_id:
        entry["skill_id"] = skill_id
    lst.append(entry)


def snapshot() -> List[Dict[str, Any]]:
    """Copie de la liste des contributeurs accumulés sur le tour (vide si aucun)."""
    return list(_CONTRIB.get() or [])


def reset() -> None:
    """Repart d'une liste vide (appelé après le dépôt d'un draft : un draft = une livraison)."""
    _CONTRIB.set(None)
