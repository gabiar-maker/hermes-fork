"""Le `tool_execution` middleware : le cœur de « rien ne part sans accord ».

Contrat Hermes (hermes_cli/middleware.py) : la callback reçoit `tool_name`, `args`, `next_call`.
  - Appeler `next_call(args)` et retourner son résultat = exécution normale (pass-through).
  - NE PAS appeler `next_call` et retourner une valeur = court-circuit (l'outil ne s'exécute pas).

Pour un envoi sortant, on COURT-CIRCUITE : on enregistre l'envoi localement, on dépose une
proposition (DraftRequest) sur le daemon, et on rend au modèle un résultat synthétique. L'envoi
réel n'aura lieu qu'au RETOUR (replay.py), après approbation. Le replay passe par
`registry.dispatch` qui NE repasse PAS par ce middleware → pas de ré-interception (pas besoin de
flag). On n'intercepte jamais un appel interne (lecture, terminal, etc.).

FAIL-CLOSED (garde essentielle) : le runner du cœur est FAIL-OPEN — si une callback lève AVANT
d'avoir appelé `next_call`, il EXÉCUTE l'outil lui-même (`_run_execution_chain` : « if next_called:
raise ; return call_at(index + 1, payload) »). Une exception dans notre logique de décision
(classification, mapping, store, contexte d'attribution…) ferait donc PARTIR l'envoi. Toute la
décision vit dans `_decide()`, encadrée d'un `try/except` global : en cas d'exception, on rend un
résultat BLOQUANT sans jamais appeler `next_call`. Seul l'appel `next_call` du pass-through reste
hors de cette garde : une exception de l'outil réel doit remonter au cœur comme avant.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def _result(payload: dict) -> str:
    # Les outils Hermes renvoient une chaîne JSON ; on respecte ce format.
    return json.dumps(payload, ensure_ascii=False)


def _blocked_on_internal_failure() -> str:
    """Résultat rendu au modèle quand NOTRE vérification a échoué (jamais l'outil)."""
    return _result(
        {
            "status": "blocked",
            "message": (
                "Envoi bloqué : une vérification interne a échoué, rien n'est parti. "
                "Réessayez plus tard ou signalez-le."
            ),
        }
    )


def _decide(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    """Décide du sort d'un appel d'outil.

    Retourne `None` pour laisser l'outil s'exécuter (pass-through : plugin passif, lecture, outil
    hors périmètre), sinon la CHAÎNE JSON à rendre au modèle à la place de l'exécution (proposition
    déposée, envoi bloqué, dépôt impossible). N'appelle JAMAIS l'outil : c'est l'appelant qui
    décide de faire suivre à `next_call`, et seulement sur `None`.
    """
    from . import classify, config, contributions, http_client, job_context, mapping, store

    # Plugin passif hors box Jean-Billie (JB_DECISION_PUSH_URL non posé) : ne rien changer.
    if not config.enabled():
        return None

    decision = classify.classify(tool_name)

    if decision == classify.PASS:
        return None

    if decision == classify.BLOCK:
        logger.warning("jb_outbound: outil d'envoi non répertorié BLOQUÉ : %s", tool_name)
        return _result(
            {
                "status": "blocked",
                "message": (
                    "Cette action n'est pas encore autorisée. Rien n'a été envoyé — "
                    "signalez-le à l'équipe pour l'activer."
                ),
            }
        )

    # PROPOSE : court-circuit → proposition.
    jb_id = uuid.uuid4().hex
    draft = mapping.to_draft(tool_name, args)
    store.save(jb_id, tool_name, args, draft["kind"], draft.get("to", ""))

    # On glisse notre identifiant local dans le payload : il nous reviendra dans la DecisionItem
    # (le control-plane round-trip le payload) → corrélation décision ↔ envoi en attente.
    body = dict(draft)
    body["payload"] = {**draft.get("payload", {}), "jb_id": jb_id}

    # Attribution : si l'interception a lieu pendant un job cron (skill → casquette), le draft
    # porte le département. Champs ADDITIFS, omis hors contexte job (chat libre) — le daemon Go
    # actuel ignore les champs inconnus (contrat répliqué côté Go en vague 2).
    ctx = job_context.current() or {}
    for key in ("department", "skill_id", "job_id"):
        value = ctx.get(key)
        if value:
            body[key] = value

    # Attribution MULTI-RÔLES (D3) : le LEAD (département du job_context parent) + les casquettes
    # DÉLÉGUÉES accumulées sur le tour (hook subagent_start) → `contributors`. Émis SEULEMENT s'il y
    # a au moins un support distinct (≥ 2 contributeurs) ; sinon omis → le portail retombe sur
    # `department` (legacy). En chat libre sans lead, on promeut le 1er contributeur en lead (Q5).
    lead = ctx.get("department")
    supports = [c for c in contributions.snapshot() if c.get("department")]
    contributors = []
    if lead:
        contributors.append({"department": lead, "role": "lead"})
        contributors.extend(c for c in supports if c["department"] != lead)
    elif supports:
        first = supports[0]
        promoted = {"department": first["department"], "role": "lead"}
        if first.get("skill_id"):
            promoted["skill_id"] = first["skill_id"]
        contributors.append(promoted)
        contributors.extend(c for c in supports[1:] if c["department"] != first["department"])
    if len(contributors) >= 2:
        body["contributors"] = contributors
    contributions.reset()  # un draft = une livraison → on repart propre (Q4)

    try:
        http_client.post_json(config.draft_url(), body)
    except Exception as exc:  # dépôt impossible → on n'a rien envoyé, on le dit franchement.
        store.mark(jb_id, "failed", str(exc))
        logger.warning("jb_outbound: dépôt de la proposition échoué (%s) : %s", tool_name, exc)
        return _result(
            {
                "status": "error",
                "message": "Je n'ai pas pu préparer la proposition pour l'instant. Rien n'est parti.",
            }
        )

    return _result(
        {
            "status": "queued_for_approval",
            "id": jb_id,
            "message": "C'est prêt : j'ai préparé la proposition. Rien ne part tant que vous n'avez pas validé.",
        }
    )


def make_middleware() -> Callable[..., Any]:
    def jb_outbound_tool_execution(
        *,
        tool_name: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        next_call: Callable[[Any], Any],
        **_: Any,
    ) -> Any:
        try:
            outcome = _decide(tool_name or "", args or {})
        except Exception as exc:
            # FAIL-CLOSED : sans ce filet, le cœur exécuterait l'outil (fail-open) et l'envoi
            # partirait. On journalise le type seulement — jamais les arguments (contenu du client).
            logger.error(
                "jb_outbound: vérification interne échouée sur %s (%s) — envoi BLOQUÉ, rien n'est parti.",
                tool_name,
                type(exc).__name__,
            )
            return _blocked_on_internal_failure()

        if outcome is None:
            # Pass-through : hors de la garde, une exception de l'outil réel remonte au cœur telle quelle.
            return next_call(args)
        return outcome

    return jb_outbound_tool_execution
