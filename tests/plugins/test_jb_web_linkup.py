"""Fournisseur web « linkup » du plugin jb_outbound — relais du plan de contrôle.

Ce que ces tests verrouillent, et pourquoi chacun :

* DISPONIBILITÉ — `is_available()` suit `JB_DRAFT_ADDR` (« ai-je un daemon à qui parler »)
  et NON la présence d'une clé : il n'y en a aucune côté box, c'est la raison d'être du relais.
  Un test veille aussi à ce qu'elle ne se mette pas à dépendre de `JB_DECISION_PUSH_URL`, qui
  commande une capacité sans rapport (la boucle de proposition).
* AUCUN SECRET, AUCUN HÔTE EXTERNE — le module ne doit contenir ni clé, ni domaine de
  fournisseur : si l'un apparaît un jour, l'architecture a été contournée.
* FORMES DU CONTRAT — `{success, data: {web: [...]}}` pour la recherche, la liste de documents
  pour l'extraction, y compris la traduction `snippet` → `description` que seul ce module connaît.
* FAIL-CLOSED — daemon injoignable ou refus métier ⇒ `success: False` avec un message franc,
  jamais un repli vers un autre fournisseur ni un résultat fabriqué.
* PAGE VIDE = SUCCÈS — c'est le cas que l'arbitrage « pas de rendu JavaScript » veut mesurer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from plugins.jb_outbound import config, web_linkup


@pytest.fixture
def provider() -> web_linkup.JbRelayWebSearchProvider:
    return web_linkup.JbRelayWebSearchProvider()


@pytest.fixture
def poste(monkeypatch):
    """Remplace le POST loopback et capture ce qui part."""
    vus: List[Dict[str, Any]] = []

    def _install(reponse: Any):
        def _fake(url: str, payload: dict, timeout: float = 0.0):
            vus.append({"url": url, "payload": payload})
            if isinstance(reponse, Exception):
                raise reponse
            return 200, reponse

        monkeypatch.setattr(web_linkup, "_post", _fake)
        return vus

    return _install


# -- disponibilité ---------------------------------------------------------


def test_disponible_suit_l_adresse_du_daemon(provider, monkeypatch):
    monkeypatch.delenv("JB_DRAFT_ADDR", raising=False)
    assert provider.is_available() is False, "hors box, Hermes doit retrouver son comportement"

    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    assert provider.is_available() is True


def test_disponible_ne_depend_pas_de_la_boucle_de_proposition(provider, monkeypatch):
    # `JB_DECISION_PUSH_URL` commande le listener de décisions — une capacité SANS RAPPORT.
    # Les confondre couperait la recherche le jour où l'une des deux évoluerait seule.
    monkeypatch.delenv("JB_DECISION_PUSH_URL", raising=False)
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    assert provider.is_available() is True


def test_nom_du_backend_est_celui_que_la_config_designe(provider):
    # `web.search_backend` / `web.extract_backend` sont posés à cette valeur par la plateforme :
    # la changer d'un côté sans l'autre rendrait le fournisseur introuvable, en silence.
    assert provider.name == "linkup"
    assert provider.supports_search() is True
    assert provider.supports_extract() is True


# -- aucun secret ne descend sur la box ------------------------------------


def test_le_module_ne_porte_ni_cle_ni_hote_externe():
    source = Path(web_linkup.__file__).read_text(encoding="utf-8")
    # Aucune URL externe : tout part en loopback, via config.search_url()/fetch_url().
    externes = re.findall(r"https?://(?!127\.0\.0\.1|localhost)[a-z0-9.-]+", source, re.IGNORECASE)
    assert externes == [], f"aucun hôte externe ne doit figurer ici : {externes}"
    # Aucun nom de variable de clé : si l'une apparaît, le relais a été contourné.
    for interdit in ("API_KEY", "api_key", "Authorization", "Bearer"):
        assert interdit not in source, f"« {interdit} » n'a rien à faire sur la box"


def test_le_schema_de_reglage_ne_demande_aucune_variable(provider):
    # Dire « rien à configurer » explicitement, plutôt que de laisser croire à un réglage manquant.
    assert provider.get_setup_schema()["env_vars"] == []


# -- recherche -------------------------------------------------------------


def test_recherche_rend_la_forme_du_moteur_et_traduit_snippet(provider, poste, monkeypatch):
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    vus = poste(
        {
            "status": "ok",
            "results": [
                {"title": "Devis", "url": "https://ex.fr/1", "snippet": "extrait 1"},
                {"title": "Tarifs", "url": "https://ex.fr/2", "snippet": "extrait 2"},
            ],
        }
    )

    out = provider.search("devis plomberie", limit=5)

    assert vus[0]["url"] == config.search_url()
    assert vus[0]["payload"] == {"query": "devis plomberie", "maxResults": 5}
    assert out["success"] is True
    assert out["data"]["web"] == [
        {"title": "Devis", "url": "https://ex.fr/1", "description": "extrait 1", "position": 1},
        {"title": "Tarifs", "url": "https://ex.fr/2", "description": "extrait 2", "position": 2},
    ]


def test_recherche_borne_le_nombre_de_resultats_et_ignore_une_limite_absurde(provider, poste, monkeypatch):
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    vus = poste({"status": "ok", "results": []})

    provider.search("x", limit=5000)
    assert vus[-1]["payload"]["maxResults"] == 20, "on borne au lieu de laisser passer"

    provider.search("x", limit=-3)
    assert "maxResults" not in vus[-1]["payload"], "une option absurde est oubliée, pas relayée"


def test_recherche_vide_ou_demesuree_refusee_sans_rien_envoyer(provider, poste, monkeypatch):
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    vus = poste({"status": "ok", "results": []})

    for mauvaise in ("", "   ", "x" * 501):
        out = provider.search(mauvaise)
        assert out["success"] is False
    assert vus == [], "rien ne doit quitter la box pour une requête qu'on sait rejetée"


def test_daemon_injoignable_echoue_franchement_sans_repli(provider, poste, monkeypatch):
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    poste(OSError("connexion refusée"))

    out = provider.search("x")
    assert out["success"] is False
    assert "réessayer" in out["error"]
    # FAIL-CLOSED : aucune donnée fabriquée, aucun autre fournisseur.
    assert "data" not in out


def test_refus_metier_relaie_la_phrase_du_plan_de_controle(provider, poste, monkeypatch):
    # Le plafond n'est pas une panne : sa phrase est déjà white-label, on ne la reformule pas.
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    poste({"status": "error", "message": "Ernestio a beaucoup cherché ces dernières minutes."})

    out = provider.search("x")
    assert out["success"] is False
    assert out["error"] == "Ernestio a beaucoup cherché ces dernières minutes."


def test_reponse_malformee_ne_plante_pas(provider, poste, monkeypatch):
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    poste({"status": "ok", "results": "pas une liste"})
    assert provider.search("x")["success"] is False

    poste({"status": "ok", "results": [None, 42, {"title": "T", "url": "u", "snippet": "s"}]})
    out = provider.search("x")
    assert out["success"] is True
    assert len(out["data"]["web"]) == 1, "les entrées douteuses sont écartées, le reste passe"


# -- lecture de page -------------------------------------------------------


def test_lecture_rend_les_documents_attendus(provider, poste, monkeypatch):
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    vus = poste({"status": "ok", "markdown": "# Tarifs\n\nDevis sous 48 h."})

    docs = provider.extract(["https://ex.fr/tarifs"])

    assert vus[0]["url"] == config.fetch_url()
    assert vus[0]["payload"] == {"url": "https://ex.fr/tarifs"}
    assert len(docs) == 1
    assert docs[0]["url"] == "https://ex.fr/tarifs"
    assert docs[0]["content"] == "# Tarifs\n\nDevis sous 48 h."
    assert docs[0]["raw_content"] == docs[0]["content"]
    assert docs[0]["metadata"]["sourceURL"] == "https://ex.fr/tarifs"
    assert "error" not in docs[0]


def test_page_vide_reste_un_succes(provider, poste, monkeypatch):
    # C'est le cas que l'arbitrage « pas de rendu JavaScript » veut pouvoir MESURER.
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    poste({"status": "ok", "markdown": ""})

    docs = provider.extract(["https://ex.fr/vide"])
    assert docs[0]["content"] == ""
    assert "error" not in docs[0]


def test_adresse_non_lisible_refusee_sans_rien_envoyer(provider, poste, monkeypatch):
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    vus = poste({"status": "ok", "markdown": "x"})

    docs = provider.extract(["file:///etc/passwd", "data:text/html,x", "  ", "x" * 2049])
    assert len(docs) == 4
    assert all("error" in d for d in docs)
    assert vus == [], "aucune de ces adresses ne doit quitter la box"


def test_une_page_ratee_n_emporte_pas_les_autres(provider, monkeypatch):
    # Le contrat du moteur veut une liste de la MÊME longueur que l'entrée.
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    appels = {"n": 0}

    def _fake(url: str, payload: dict, timeout: float = 0.0):
        appels["n"] += 1
        if payload["url"].endswith("/casse"):
            raise OSError("connexion refusée")
        return 200, {"status": "ok", "markdown": "ok"}

    monkeypatch.setattr(web_linkup, "_post", _fake)

    docs = provider.extract(["https://ex.fr/a", "https://ex.fr/casse", "https://ex.fr/b"])
    assert len(docs) == 3
    assert docs[0]["content"] == "ok"
    assert "error" in docs[1]
    assert docs[2]["content"] == "ok", "la page qui SUIT l'échec doit être lue quand même"
    assert appels["n"] == 3


def test_les_messages_rendus_ne_nomment_aucun_fournisseur(provider, poste, monkeypatch):
    # Ces phrases peuvent finir sous les yeux du client (règle d'or : jamais la technique).
    monkeypatch.setenv("JB_DRAFT_ADDR", "127.0.0.1:8442")
    poste(OSError("Linkup 500 upstream firecrawl tavily"))
    interdits = re.compile(r"linkup|tavily|firecrawl|api|http|token|bearer", re.IGNORECASE)

    assert not interdits.search(provider.search("x")["error"])
    assert not interdits.search(provider.extract(["https://ex.fr/a"])[0]["error"])


# -- enregistrement --------------------------------------------------------


def test_le_plugin_enregistre_bien_le_fournisseur():
    """`register()` doit poser le fournisseur : sans ça, tout le reste est mort-né."""
    from plugins import jb_outbound

    poses: List[Any] = []

    class _Ctx:
        def register_middleware(self, *a, **k):
            pass

        def register_hook(self, *a, **k):
            pass

        def register_tool(self, *a, **k):
            pass

        def register_auxiliary_task(self, *a, **k):
            pass

        def register_web_search_provider(self, p):
            poses.append(p)

    jb_outbound.register(_Ctx())
    assert len(poses) == 1
    assert isinstance(poses[0], web_linkup.JbRelayWebSearchProvider)
    assert poses[0].name == "linkup"
