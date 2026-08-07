"""Fournisseur web « linkup » — recherche et lecture de page PAR LE RELAIS du plan de contrôle.

Ce que ce fournisseur ne fait PAS, et c'est tout l'intérêt : il ne connaît aucune clé, aucune URL
externe, aucun fournisseur. Il POSTe sur le loopback du control daemon (127.0.0.1, sans mTLS,
moindre privilège : le workload ne détient aucun cert de flotte), qui relaie au plan de contrôle
avec SON identité mTLS. La clé du moteur de recherche vit au coffre de la plateforme et n'en sort
jamais — EXACTEMENT le même chemin que les drafts, `creer_support` et `request_tool_connection`.

Trois raisons à ce montage, dans l'ordre où elles comptent :
  1. une clé plateforme copiée sur N box fuirait avec la PREMIÈRE box compromise ;
  2. un appel direct échapperait au comptage, donc au plafond de forfait du client ;
  3. la box ne gagne AUCUN hôte sortant : elle parle déjà au plan de contrôle.

Enregistré PAR LE PLUGIN jb_outbound (`ctx.register_web_search_provider`, cf. `__init__.py`) :
zéro patch du cœur, et la divergence du fork reste dans le seul répertoire qui lui est réservé.
La découverte de plugins étant générique (`_ensure_web_plugins_loaded` appelle
`_ensure_plugins_discovered`), ce fournisseur atterrit dans le registre comme les fournisseurs
groupés de `plugins/web/*`.

Hors box (`JB_DRAFT_ADDR` absent), `is_available()` est faux : en CLI local, Hermes retrouve son
comportement d'origine. C'est la seule condition — et NON la présence d'une clé, puisqu'il n'y en a
aucune de ce côté-ci.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Dict, List, Tuple

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Une recherche est SYNCHRONE de bout en bout : un tour de conversation attend derrière. On borne
# LÉGÈREMENT au-dessus du relais daemon (30 s) pour que l'erreur amont arrive avant la nôtre — sinon
# on rendrait « délai dépassé » là où le daemon avait un motif précis à donner.
_TIMEOUT_S = 35.0

# Bornes MIROIR de celles du daemon (web.go) et du plan de contrôle (ingest.ts). Vérifier ici aussi
# n'est pas de la redondance : c'est le seul endroit qui peut refuser AVANT que quoi que ce soit ne
# quitte la box, et un appel refusé plus loin aurait déjà traversé deux étages pour rien.
_MAX_QUERY_LEN = 500
_MAX_URL_LEN = 2048
# Plafond de résultats demandés — le plan de contrôle ramène de toute façon au sien.
_MAX_LIMIT = 20

# Messages rendus à l'assistant. Ni nom de fournisseur, ni jargon : ils peuvent finir sous les yeux
# du client. Le fournisseur, lui, n'est nommé nulle part dans ce fichier hors du nom du backend.
_MSG_UNAVAILABLE = (
    "Je n'ai pas réussi à chercher sur le web à l'instant. On peut réessayer dans un moment."
)
_MSG_UNAVAILABLE_READ = (
    "Je n'ai pas réussi à lire cette page à l'instant. On peut réessayer dans un moment."
)


def _post(url: str, payload: dict, timeout: float = _TIMEOUT_S) -> Tuple[int, dict]:
    """POST JSON qui LIT la réponse. Loopback uniquement (le daemon).

    Lève en cas d'erreur réseau/HTTP — l'appelant traduit en message franc. Même forme que
    `produce._post` : on ne factorise pas entre les deux pour garder chaque module lisible seul.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback only)
        body = resp.read()
    return int(getattr(resp, "status", 200)), (json.loads(body.decode("utf-8")) if body else {})


class JbRelayWebSearchProvider(WebSearchProvider):
    """Recherche + lecture de page, relayées par le control daemon de la box."""

    @property
    def name(self) -> str:
        # C'est CE nom que `web.search_backend` / `web.extract_backend` du config.yaml désignent
        # (posés par le config-generator de la plateforme). Le changer casse les deux bouts.
        return "linkup"

    @property
    def display_name(self) -> str:
        return "Recherche web (relais Ernestio)"

    def is_available(self) -> bool:
        """Vrai sur une box (le daemon écoute), faux ailleurs.

        PAS « une clé est présente » : il n'y en a aucune de ce côté-ci, c'est la raison d'être du
        relais. La disponibilité, ici, c'est « ai-je quelqu'un à qui demander ».
        """
        from . import config

        return bool(config.draft_addr_present())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    # -- recherche ---------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Relaie la recherche et rend la forme attendue par le moteur.

        Contrat (`agent/web_search_provider`) : `{success, data: {web: [...]}}` en succès,
        `{success: False, error}` en échec — l'erreur est un message franc, jamais un détail
        technique ni le nom d'un fournisseur.
        """
        from . import config

        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}
        except Exception:  # noqa: BLE001 — module optionnel selon le contexte d'exécution
            pass

        q = (query or "").strip()
        if not q:
            return {"success": False, "error": "Dites-moi ce que je dois chercher."}
        if len(q) > _MAX_QUERY_LEN:
            # Refusé ICI : inutile de faire traverser deux étages à une requête qu'on sait rejetée.
            return {"success": False, "error": "Cette recherche est trop longue pour moi."}

        payload: Dict[str, Any] = {"query": q}
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            payload["maxResults"] = min(n, _MAX_LIMIT)

        try:
            _, out = _post(config.search_url(), payload)
        except Exception as exc:  # noqa: BLE001 — y compris les erreurs urllib
            # FAIL-CLOSED : jamais de repli vers un autre fournisseur, jamais un résultat fabriqué.
            logger.warning("jb_outbound: recherche web indisponible: %s", exc)
            return {"success": False, "error": _MSG_UNAVAILABLE}

        if out.get("status") != "ok":
            # Refus MÉTIER du plan de contrôle (plafond atteint…) : sa phrase est déjà white-label,
            # on la relaie telle quelle plutôt que de la reformuler et d'en perdre le sens.
            return {"success": False, "error": str(out.get("message") or _MSG_UNAVAILABLE)}

        results = out.get("results")
        if not isinstance(results, list):
            return {"success": False, "error": _MSG_UNAVAILABLE}

        web: List[Dict[str, Any]] = []
        for i, r in enumerate(results):
            if not isinstance(r, dict):
                continue
            web.append(
                {
                    "title": str(r.get("title") or ""),
                    "url": str(r.get("url") or ""),
                    # Le relais parle « snippet », le moteur attend « description » : la traduction
                    # se fait ICI, au seul endroit qui connaît les deux vocabulaires.
                    "description": str(r.get("snippet") or ""),
                    "position": i + 1,
                }
            )
        return {"success": True, "data": {"web": web}}

    # -- lecture de page ---------------------------------------------------

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Relaie la lecture, une URL à la fois, et rend la liste de documents attendue.

        Le relais lit UNE page par appel (contrat du plan de contrôle) : on boucle. Un échec sur une
        URL devient une entrée porteuse d'`error`, jamais une exception — le contrat du moteur veut
        une liste de la même longueur que l'entrée, et une page ratée ne doit pas emporter les autres.
        """
        from . import config

        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [{"url": u, "title": "", "content": "", "error": "Interrupted"} for u in urls]
        except Exception:  # noqa: BLE001
            pass

        documents: List[Dict[str, Any]] = []
        for raw_url in urls or []:
            u = str(raw_url or "").strip()
            if not u or len(u) > _MAX_URL_LEN or not u.lower().startswith(("http://", "https://")):
                documents.append(
                    {
                        "url": u,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": "Cette adresse n'est pas lisible.",
                        "metadata": {"sourceURL": u},
                    }
                )
                continue

            try:
                _, out = _post(config.fetch_url(), {"url": u})
            except Exception as exc:  # noqa: BLE001
                logger.warning("jb_outbound: lecture de page indisponible (%s): %s", u, exc)
                documents.append(
                    {
                        "url": u,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": _MSG_UNAVAILABLE_READ,
                        "metadata": {"sourceURL": u},
                    }
                )
                continue

            if out.get("status") != "ok":
                documents.append(
                    {
                        "url": u,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": str(out.get("message") or _MSG_UNAVAILABLE_READ),
                        "metadata": {"sourceURL": u},
                    }
                )
                continue

            # `markdown` VIDE est un SUCCÈS : la page n'avait pas de texte lisible. C'est le cas que
            # l'arbitrage « pas de rendu JavaScript » veut pouvoir mesurer, pas une panne à maquiller.
            content = str(out.get("markdown") or "")
            documents.append(
                {
                    "url": u,
                    "title": "",
                    "content": content,
                    "raw_content": content,
                    "metadata": {"sourceURL": u},
                }
            )
        return documents

    def get_setup_schema(self) -> Dict[str, Any]:
        """Aucune variable à saisir : c'est le propre de ce fournisseur.

        La clé vit au coffre de la plateforme, la box n'en a pas. Rendre une liste vide dit
        explicitement « rien à configurer ici » — au lieu de laisser croire qu'il manque un réglage.
        """
        return {
            "name": "Recherche web (relais Ernestio)",
            "badge": "managed",
            "tag": "Recherche et lecture de page, servies par la plateforme. Aucune clé sur la machine.",
            "env_vars": [],
        }
