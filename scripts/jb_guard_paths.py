#!/usr/bin/env python3
"""Garde-fou de confinement du fork Jean-Billie (lane F1).

Vérifie qu'une liste de fichiers modifiés (arguments, ou stdin à défaut)
reste dans le périmètre autorisé décrit par ``.github/jb-allowed-paths.txt``.
Sort avec le code 1 en nommant chaque fichier hors périmètre.

Utilisation :
    python3 scripts/jb_guard_paths.py fichier1 fichier2 ...
    git diff --name-only origin/main...HEAD | python3 scripts/jb_guard_paths.py

Stdlib uniquement (argparse, pathlib, re, sys) — aucune dépendance.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_ALLOWLIST = (
    Path(__file__).resolve().parent.parent / ".github" / "jb-allowed-paths.txt"
)


def load_allowlist(path: Path | str) -> list[str]:
    """Lit l'allowlist : un glob par ligne, ignore vides et commentaires ``#``."""
    patterns: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Traduit un glob en regex ancrée.

    Sémantique : ``**`` traverse les séparateurs ``/`` (zéro ou plusieurs
    segments) ; ``*`` et ``?`` restent dans un segment (ne matchent pas ``/``).
    C'est plus strict que ``fnmatch`` (où ``*`` traverse les ``/``), et
    conforme aux globs « gitignore-like » attendus dans l'allowlist.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def normalize(path: str) -> str:
    """Normalise un chemin git : séparateurs ``/``, sans ``./`` de tête."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def find_offenders(files: list[str], patterns: list[str]) -> list[str]:
    """Renvoie les fichiers qui ne matchent AUCUN glob de l'allowlist."""
    regexes = [glob_to_regex(p) for p in patterns]
    return [f for f in files if not any(r.match(f) for r in regexes)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Échoue (code 1) si un fichier modifié sort du périmètre autorisé "
            "du fork Jean-Billie."
        )
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Fichiers modifiés à vérifier (à défaut : lus sur stdin, un par ligne).",
    )
    parser.add_argument(
        "--allowlist",
        default=str(DEFAULT_ALLOWLIST),
        help="Chemin de l'allowlist de globs (défaut : .github/jb-allowed-paths.txt).",
    )
    args = parser.parse_args(argv)

    raw_files = args.files if args.files else sys.stdin.read().splitlines()
    files = [normalize(f) for f in raw_files if f.strip()]

    allowlist_path = Path(args.allowlist)
    if not allowlist_path.is_file():
        print(f"jb-guard: allowlist introuvable : {allowlist_path}", file=sys.stderr)
        return 2

    patterns = load_allowlist(allowlist_path)
    if not files:
        print("jb-guard: aucun fichier modifié — rien à vérifier.")
        return 0

    offenders = find_offenders(files, patterns)
    if offenders:
        print(
            "jb-guard: ÉCHEC — fichiers hors du périmètre autorisé du fork "
            f"({allowlist_path.name}) :",
            file=sys.stderr,
        )
        for f in offenders:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nRègle : la divergence vit dans plugins/jb_outbound/ + les fichiers "
            "additifs déclarés. Zéro nouveau patch du cœur Hermes.\n"
            "Si ce patch cœur est volontaire (ex. résorption F2/F3), posez le "
            "label « jb-core-approved » sur la PR.",
            file=sys.stderr,
        )
        return 1

    print(f"jb-guard: OK — {len(files)} fichier(s) dans le périmètre autorisé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
