#!/usr/bin/env python3
"""Veille amont hebdomadaire du fork Jean-Billie (F2, lot 5).

Politique de refusion (founder, QCM 2026-08-28) : **refusion à chaque TAG amont vieux d'au
moins 3 semaines, ou IMMÉDIATE sur une release de sécurité ; jamais sur upstream/main.**

Ce script (stdlib seule, ``gh`` pour l'issue) :

1. lit la base amont du fork dans l'en-tête de ``.github/jb-allowed-paths.txt``
   (marqueur machine-lisible ``# JB_UPSTREAM_BASE=vX``) ;
2. interroge ``GET /repos/NousResearch/hermes-agent/releases/latest`` (token facultatif) ;
3. décide : alerte si le dernier tag ≠ base ET (publié depuis ≥ 21 jours OU release de sécurité —
   ``security`` / ``CVE-…`` dans le nom ou le corps) ;
4. ouvre — ou met à jour, jamais de doublon — une issue « Veille amont : <tag> disponible
   (base <base>, N jours) », reconnue par un marqueur caché dans son corps.

Idempotent et JAMAIS bruyant : toute erreur (réseau, ``gh`` absent…) est journalisée sur stderr et
le script sort en 0 — une veille qui casse la CI serait désactivée, une veille silencieuse est relue
la semaine suivante. ``--dry-run`` affiche la décision sans toucher aux issues.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

UPSTREAM_REPO = "NousResearch/hermes-agent"
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent.parent / ".github" / "jb-allowed-paths.txt"
MIN_AGE_DAYS = 21
ISSUE_MARKER = "<!-- jb-upstream-watch -->"
ISSUE_LABEL = "veille-amont"

_BASE_RE = re.compile(r"^\s*#\s*JB_UPSTREAM_BASE\s*=\s*(\S+)\s*$", re.M)
_SECURITY_RE = re.compile(r"\b(security|sécurité|CVE-\d{4}-\d{4,})\b", re.I)


@dataclass(frozen=True)
class Decision:
    alert: bool
    tag: str
    base: str
    age_days: int
    security: bool
    reason: str


def read_base(allowlist_path: Path | str) -> str:
    """Base amont déclarée (``# JB_UPSTREAM_BASE=vX``) ; ``ValueError`` si le marqueur manque."""
    text = Path(allowlist_path).read_text(encoding="utf-8")
    m = _BASE_RE.search(text)
    if not m:
        raise ValueError(f"marqueur JB_UPSTREAM_BASE absent de {allowlist_path}")
    return m.group(1)


def is_security_release(name: Optional[str], body: Optional[str]) -> bool:
    """Vrai si le nom ou le corps de la release mentionne la sécurité ou une CVE."""
    return bool(_SECURITY_RE.search(f"{name or ''}\n{body or ''}"))


def parse_published_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def decide(
    base: str,
    tag: str,
    published_at: Optional[datetime],
    name: Optional[str] = None,
    body: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
    min_age_days: int = MIN_AGE_DAYS,
) -> Decision:
    """Applique la politique : même tag → rien ; sinon alerte si ≥ ``min_age_days`` ou sécurité."""
    now = now or datetime.now(timezone.utc)
    age_days = int((now - published_at).days) if published_at else 0
    security = is_security_release(name, body)
    if tag == base:
        return Decision(False, tag, base, age_days, security, "le fork est sur le dernier tag amont")
    if security:
        return Decision(True, tag, base, age_days, True, "release de sécurité : refusion IMMÉDIATE")
    if published_at is None:
        return Decision(False, tag, base, age_days, False, "date de publication inconnue : on attend")
    if age_days >= min_age_days:
        return Decision(True, tag, base, age_days, False, f"tag publié depuis {age_days} j (≥ {min_age_days})")
    return Decision(
        False, tag, base, age_days, False,
        f"tag publié depuis {age_days} j (< {min_age_days}) : on laisse mûrir",
    )


def fetch_latest_release(repo: str = UPSTREAM_REPO, token: Optional[str] = None, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "jb-upstream-watch",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL fixe api.github.com
        return json.loads(resp.read().decode("utf-8"))


def issue_title(decision: Decision) -> str:
    return f"Veille amont : {decision.tag} disponible (base {decision.base}, {decision.age_days} jours)"


def issue_body(decision: Decision, release_url: str) -> str:
    urgency = "**RELEASE DE SÉCURITÉ — refusion immédiate.**" if decision.security else (
        f"Tag mûr depuis ≥ {MIN_AGE_DAYS} jours : à fusionner à la prochaine vague."
    )
    return "\n".join(
        [
            ISSUE_MARKER,
            f"Le dernier tag amont **{decision.tag}** ({release_url}) diffère de la base du fork "
            f"**{decision.base}** (`JB_UPSTREAM_BASE` dans `.github/jb-allowed-paths.txt`).",
            "",
            f"- Âge de la release : {decision.age_days} jours",
            f"- Raison : {decision.reason}",
            "",
            urgency,
            "",
            "Politique (founder, 2026-08-28) : refusion à chaque tag ≥ 3 semaines, ou immédiate sur "
            "release de sécurité ; jamais sur upstream/main. Rituel : base de contexte → fusion à blanc → "
            "lanes → registre de dette (`JB_UPSTREAM_BASE` mis à jour ferme cette issue au prochain passage).",
            "",
            f"_Mise à jour automatique : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}._",
        ]
    )


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def find_open_issue(repo: str) -> Optional[dict]:
    """Issue ouverte portant le marqueur (recherche par label, puis par corps)."""
    raw = _gh(
        "issue", "list", "--repo", repo, "--state", "open", "--label", ISSUE_LABEL,
        "--json", "number,title,body", "--limit", "20",
    )
    for item in json.loads(raw or "[]"):
        if ISSUE_MARKER in (item.get("body") or ""):
            return item
    return None


def ensure_label(repo: str) -> None:
    try:
        _gh("label", "create", ISSUE_LABEL, "--repo", repo, "--color", "B60205",
            "--description", "Veille amont Hermes (tag disponible)", "--force")
    except subprocess.CalledProcessError as exc:
        print(f"jb-upstream-watch: label non créé ({exc.stderr.strip()})", file=sys.stderr)


def upsert_issue(repo: str, decision: Decision, release_url: str) -> str:
    """Crée l'issue ou met à jour titre/corps de l'existante. Renvoie « created » / « updated »."""
    title, body = issue_title(decision), issue_body(decision, release_url)
    existing = find_open_issue(repo)
    if existing:
        _gh("issue", "edit", str(existing["number"]), "--repo", repo, "--title", title, "--body", body)
        return "updated"
    ensure_label(repo)
    _gh("issue", "create", "--repo", repo, "--title", title, "--body", body, "--label", ISSUE_LABEL)
    return "created"


def close_stale_issue(repo: str, decision: Decision) -> bool:
    """Le fork a rattrapé le tag (ou il n'y a rien à signaler) : ferme l'issue de veille si ouverte."""
    existing = find_open_issue(repo)
    if not existing:
        return False
    _gh(
        "issue", "close", str(existing["number"]), "--repo", repo, "--comment",
        f"Refermée par la veille : {decision.reason} (base {decision.base}, dernier tag {decision.tag}).",
    )
    return True


def run(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Veille amont hebdomadaire (fork Jean-Billie).")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                        help="Dépôt du FORK où ouvrir l'issue (défaut : $GITHUB_REPOSITORY).")
    parser.add_argument("--upstream", default=UPSTREAM_REPO)
    parser.add_argument("--dry-run", action="store_true", help="Décide et affiche, sans toucher aux issues.")
    args = parser.parse_args(argv)

    try:
        base = read_base(args.allowlist)
        release = fetch_latest_release(args.upstream, token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
        tag = str(release.get("tag_name") or "")
        decision = decide(
            base, tag, parse_published_at(release.get("published_at")),
            release.get("name"), release.get("body"),
        )
        release_url = str(release.get("html_url") or f"https://github.com/{args.upstream}/releases/tag/{tag}")
        print(f"jb-upstream-watch: base={base} dernier={tag} âge={decision.age_days}j "
              f"sécurité={decision.security} alerte={decision.alert} — {decision.reason}")
        if args.dry_run:
            return 0
        if not args.repo:
            print("jb-upstream-watch: --repo / GITHUB_REPOSITORY absent, pas d'issue.", file=sys.stderr)
            return 0
        if decision.alert:
            print(f"jb-upstream-watch: issue {upsert_issue(args.repo, decision, release_url)}.")
        elif close_stale_issue(args.repo, decision):
            print("jb-upstream-watch: issue de veille refermée (fork à jour).")
    except (urllib.error.URLError, subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        # Jamais bruyant : une veille qui casse la CI finirait désactivée.
        print(f"jb-upstream-watch: passage sans effet ({type(exc).__name__}: {exc})", file=sys.stderr)
    return 0


def main() -> int:  # pragma: no cover - point d'entrée
    try:
        return run()
    except Exception as exc:  # filet ultime : exit 0 + log
        print(f"jb-upstream-watch: erreur inattendue ignorée ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
