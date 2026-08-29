"""Commande plugin ``hermes jb-mcp-probe`` : sonde MCP read-only du fleet daemon (F2, lot 3).

Reproduit EXACTEMENT ``hermes mcp probe`` (hunk cœur ``hermes_cli/mcp_config.py::cmd_mcp_probe`` +
``hermes_cli/subcommands/mcp.py``) par le seam natif ``ctx.register_cli_command`` — zéro patch du
cœur. Contrat figé « C1 » consommé par le daemon Go (``tests/test_mcp_probe.py``) :

* ``--url`` obligatoire, ``--header "Name: Value"`` répétable ;
* découverte ``tools/list`` seulement (jamais ``tools/call``), AUCUNE écriture (pas de config.yaml) ;
* stdout = UN objet JSON ``{"tools": [{"name", "description"}]}`` et rien d'autre ;
* diagnostics sur stderr, secrets (url, en-têtes) jamais renvoyés — seul le TYPE de l'exception ;
* codes de sortie : 2 (usage : url absente / en-tête invalide), 1 (sonde en échec), 0 sinon.

Transport : ``hermes_cli.mcp_config._probe_single_server`` — fonction PRIVÉE du cœur (nom instable,
rapport S5 §B.6), résolue PAR ATTRIBUT au moment de l'appel (monkeypatch-able, et un renommage amont
tombe dans le chemin « probe failed » plutôt qu'en ImportError au chargement du plugin). Le hunk
cœur reste en place tant que le daemon appelle ``hermes mcp probe`` ; il est retiré avec la bascule
de chaîne côté daemon (lane D).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Tuple

COMMAND_NAME = "jb-mcp-probe"
COMMAND_HELP = "Probe an MCP URL and print its tools as JSON (read-only, no save)"
COMMAND_DESCRIPTION = (
    "Non-interactive, read-only MCP discovery for the Jean-Billie fleet daemon: connect to an "
    "ad-hoc URL, list its tools, print JSON on stdout, save nothing. Same output as "
    "`hermes mcp probe`."
)

# Nom de serveur jetable passé au transport partagé (jamais persisté) — identique au cœur.
_PROBE_SERVER_NAME = "__probe__"


def setup_parser(parser: Any) -> None:
    """Ajoute les options de la commande (même forme que ``hermes mcp probe``)."""
    parser.add_argument("--url", required=True, help="MCP server URL to probe")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME: VALUE",
        help="HTTP header (repeatable), e.g. 'Authorization: Bearer <key>'",
    )


def _probe(name: str, config: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Résout ``_probe_single_server`` à l'appel (attribut du module, pas import figé)."""
    import hermes_cli.mcp_config as mcp_config

    return mcp_config._probe_single_server(name, config)


def run(args: Any) -> None:
    """Handler de la commande : JSON sur stdout, erreurs sur stderr, ``sys.exit`` sur échec.

    Corps volontairement identique à ``cmd_mcp_probe`` (parité prouvée par
    ``test_mcp_probe.py``) : c'est la sortie que le daemon parse.
    """
    url = getattr(args, "url", None)
    if not url:
        print("error: --url is required", file=sys.stderr)
        sys.exit(2)

    # Parse repeated --header "Name: Value" into a headers dict.
    headers: Dict[str, str] = {}
    for raw in getattr(args, "header", None) or []:
        if ":" not in raw:
            print("error: invalid --header (expected 'Name: Value')", file=sys.stderr)
            sys.exit(2)
        k, v = raw.split(":", 1)
        headers[k.strip()] = v.strip()

    # Ad-hoc server config — NOT persisted anywhere.
    server_config: Dict[str, Any] = {"url": url}
    if headers:
        server_config["headers"] = headers

    try:
        tools = _probe(_PROBE_SERVER_NAME, server_config)
    except Exception as exc:  # scrub: never echo url/header
        print(f"error: probe failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)

    payload = {"tools": [{"name": n, "description": d or ""} for (n, d) in tools]}
    print(json.dumps(payload))
