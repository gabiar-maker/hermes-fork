"""Tests de la veille amont (scripts/jb_upstream_watch.py, F2 lot 5).

Règle des 21 jours, mot-clé sécurité, lecture du marqueur JB_UPSTREAM_BASE, idempotence de
l'issue (création vs mise à jour) et silence garanti sur erreur réseau (exit 0).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "jb_upstream_watch.py"
REAL_ALLOWLIST = REPO_ROOT / ".github" / "jb-allowed-paths.txt"


def _load():
    spec = importlib.util.spec_from_file_location("jb_upstream_watch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # le @dataclass du script résout son module via sys.modules
    spec.loader.exec_module(mod)
    return mod


watch = _load()
NOW = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)


# ── règle des 21 jours ───────────────────────────────────────────────────────

def test_meme_tag_pas_d_alerte():
    d = watch.decide("v2026.8.27", "v2026.8.27", NOW - timedelta(days=60), now=NOW)
    assert d.alert is False and "dernier tag" in d.reason


@pytest.mark.parametrize("age_days, expected", [(0, False), (20, False), (21, True), (45, True)])
def test_regle_des_21_jours(age_days, expected):
    d = watch.decide("v2026.8.27", "v2026.9.1", NOW - timedelta(days=age_days), now=NOW)
    assert d.alert is expected
    assert d.age_days == age_days and d.security is False


def test_date_inconnue_on_attend():
    d = watch.decide("v2026.8.27", "v2026.9.1", None, now=NOW)
    assert d.alert is False and "inconnue" in d.reason


# ── mot-clé sécurité → immédiat ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "name, body",
    [
        ("v2026.9.1", "Fixes a **security** issue in the gateway"),
        ("Security release", ""),
        ("v2026.9.1", "Addresses CVE-2026-12345 in tirith"),
        ("v2026.9.1", "Correctif de sécurité"),
    ],
)
def test_release_de_securite_alerte_immediatement(name, body):
    d = watch.decide("v2026.8.27", "v2026.9.1", NOW - timedelta(days=1), name, body, now=NOW)
    assert d.alert is True and d.security is True and "IMMÉDIATE" in d.reason


def test_mot_securite_dans_le_tag_courant_ne_declenche_rien():
    d = watch.decide("v2026.8.27", "v2026.8.27", NOW - timedelta(days=1), "Security release", "", now=NOW)
    assert d.alert is False


@pytest.mark.parametrize("text", ["insecure defaults", "securely stored", "Rollup patch release"])
def test_faux_positifs_securite_evites(text):
    assert watch.is_security_release(text, text) is False


# ── marqueur JB_UPSTREAM_BASE ────────────────────────────────────────────────

def test_lecture_du_marqueur_dans_l_allowlist_reelle():
    base = watch.read_base(REAL_ALLOWLIST)
    assert base.startswith("v20")
    # Cohérent avec la base déclarée en prose dans l'en-tête.
    assert f"Base actuelle : {base}" in REAL_ALLOWLIST.read_text(encoding="utf-8")


def test_marqueur_absent_est_une_erreur(tmp_path):
    p = tmp_path / "allow.txt"
    p.write_text("# rien ici\nplugins/**\n", encoding="utf-8")
    with pytest.raises(ValueError):
        watch.read_base(p)


def test_parse_published_at():
    assert watch.parse_published_at("2026-08-27T12:00:00Z") == datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    assert watch.parse_published_at(None) is None and watch.parse_published_at("n/a") is None


# ── issue : jamais de doublon, titre/corps attendus ──────────────────────────

def test_issue_titre_et_corps():
    d = watch.decide("v2026.8.27", "v2026.9.1", NOW - timedelta(days=30), now=NOW)
    assert watch.issue_title(d) == "Veille amont : v2026.9.1 disponible (base v2026.8.27, 30 jours)"
    body = watch.issue_body(d, "https://x/rel")
    assert body.startswith(watch.ISSUE_MARKER) and "v2026.8.27" in body and "https://x/rel" in body


def test_upsert_met_a_jour_l_issue_existante_sans_doublon(monkeypatch):
    calls: list = []

    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("issue", "list"):
            return '[{"number": 7, "title": "old", "body": "%s\\nold"}]' % watch.ISSUE_MARKER
        return ""

    monkeypatch.setattr(watch, "_gh", fake_gh)
    d = watch.decide("v2026.8.27", "v2026.9.1", NOW - timedelta(days=30), now=NOW)
    assert watch.upsert_issue("gabiar-maker/hermes-fork", d, "https://x") == "updated"
    assert [c[:2] for c in calls] == [("issue", "list"), ("issue", "edit")]
    assert "7" in calls[1]


def test_upsert_cree_l_issue_quand_aucune_ouverte(monkeypatch):
    calls: list = []

    def fake_gh(*args):
        calls.append(args)
        return "[]" if args[:2] == ("issue", "list") else ""

    monkeypatch.setattr(watch, "_gh", fake_gh)
    d = watch.decide("v2026.8.27", "v2026.9.1", NOW - timedelta(days=30), now=NOW)
    assert watch.upsert_issue("gabiar-maker/hermes-fork", d, "https://x") == "created"
    assert [c[:2] for c in calls] == [("issue", "list"), ("label", "create"), ("issue", "create")]


# ── jamais bruyant ───────────────────────────────────────────────────────────

def test_run_erreur_reseau_sort_en_0(monkeypatch, capsys, tmp_path):
    p = tmp_path / "allow.txt"
    p.write_text("# JB_UPSTREAM_BASE=v2026.8.27\n", encoding="utf-8")

    def _down(*a, **k):
        raise urllib.error.URLError("api.github.com injoignable")

    monkeypatch.setattr(watch, "fetch_latest_release", _down)
    assert watch.run(["--allowlist", str(p), "--repo", "x/y"]) == 0
    assert "passage sans effet" in capsys.readouterr().err


def test_run_gh_absent_sort_en_0(monkeypatch, capsys, tmp_path):
    p = tmp_path / "allow.txt"
    p.write_text("# JB_UPSTREAM_BASE=v2026.8.27\n", encoding="utf-8")
    monkeypatch.setattr(watch, "fetch_latest_release", lambda *a, **k: {
        "tag_name": "v2026.9.1", "published_at": "2026-08-01T00:00:00Z", "body": "", "html_url": "u"})

    def _no_gh(*args):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="gh: not found")

    monkeypatch.setattr(watch, "_gh", _no_gh)
    assert watch.run(["--allowlist", str(p), "--repo", "x/y"]) == 0
    assert "passage sans effet" in capsys.readouterr().err


def test_run_dry_run_n_appelle_jamais_gh(monkeypatch, capsys, tmp_path):
    p = tmp_path / "allow.txt"
    p.write_text("# JB_UPSTREAM_BASE=v2026.8.27\n", encoding="utf-8")
    monkeypatch.setattr(watch, "fetch_latest_release", lambda *a, **k: {
        "tag_name": "v2026.9.1", "published_at": "2026-08-01T00:00:00Z", "body": "", "html_url": "u"})
    monkeypatch.setattr(watch, "_gh", lambda *a: pytest.fail("gh appelé en --dry-run"))
    assert watch.run(["--allowlist", str(p), "--repo", "x/y", "--dry-run"]) == 0
    assert "alerte=True" in capsys.readouterr().out


def test_workflow_planifie_le_lundi_et_sans_secret():
    text = (REPO_ROOT / ".github" / "workflows" / "jb-upstream-watch.yml").read_text(encoding="utf-8")
    assert "cron: '0 7 * * 1'" in text and "workflow_dispatch" in text
    assert "issues: write" in text and "secrets." not in text
    assert "scripts/jb_upstream_watch.py" in text
