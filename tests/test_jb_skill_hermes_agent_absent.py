"""White-label (F2 lot 4) : le skill « hermes-agent » n'existe jamais sur une box Jean-Billie.

En 0.20.6, ``skills.disabled: [hermes-agent]`` et ``.no-bundled-skills`` sont des placebos :
``agent/skill_utils.ESSENTIAL_SKILLS`` soustrait ce nom de toute liste de désactivation et
``tools/skills_sync.py`` le seed même sur un profil opt-out. La seule voie sûre est l'absence de
la SOURCE dans l'image (``Dockerfile`` : ``RUN rm -rf …/skills/autonomous-ai-agents/hermes-agent``
après la copie des sources). Ces tests prouvent que ``sync_skills`` tolère cette absence : pas
d'exception, rien seedé, aucun re-téléchargement — sur un profil opt-out comme sur un profil normal —
et que le Dockerfile porte bien la suppression APRÈS ``COPY . .``.

Fuite résiduelle documentée (identique 0.18.2, hors périmètre du fork — PR amont proposée) :
``agent/prompt_builder.HERMES_AGENT_HELP_GUIDANCE`` (« You run on Hermes Agent… load the
`hermes-agent` skill ») est injectée dès que ``skill_view`` est dans le toolset
(``agent/system_prompt.py``), sans vérifier que le skill existe. Le test de fin PINCE cette
fuite : le jour où l'amont conditionne la phrase, il casse et on retire la note.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bundled(tmp_path: Path, names: list[tuple[str, str]]) -> Path:
    bundled = tmp_path / "bundled"
    for cat, name in names:
        d = bundled / cat / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\nbody\n", encoding="utf-8")
    return bundled


@pytest.fixture
def ss_env(monkeypatch, tmp_path):
    import tools.skills_sync as ss

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(ss, "_hermes_home", lambda: home)
    monkeypatch.setattr(ss, "_build_external_skill_index", lambda: set())
    return ss, home


def test_sync_opt_out_sans_source_hermes_agent_ne_seed_rien_et_ne_leve_pas(ss_env, monkeypatch):
    """Profil ``.no-bundled-skills`` (essential_only) : la source manque → aucun skill, aucune erreur."""
    ss, home = ss_env
    (home / ss.NO_BUNDLED_SKILLS_MARKER).write_text("", encoding="utf-8")
    bundled = _bundled(home.parent, [("media", "gif-search")])  # PAS de hermes-agent
    monkeypatch.setattr(ss, "_get_bundled_dir", lambda: bundled)

    result = ss.sync_skills(quiet=True)

    assert result["skipped_opt_out"] is True
    assert result["copied"] == [] and result["updated"] == []
    assert not (home / "skills" / "autonomous-ai-agents").exists()
    assert not (home / "skills" / "media").exists()


def test_sync_normal_sans_source_hermes_agent_seed_le_reste_seulement(ss_env, monkeypatch):
    ss, home = ss_env
    bundled = _bundled(home.parent, [("media", "gif-search"), ("devops", "docker")])
    monkeypatch.setattr(ss, "_get_bundled_dir", lambda: bundled)

    result = ss.sync_skills(quiet=True)

    assert sorted(result["copied"]) == ["docker", "gif-search"]
    assert not any(p.name == "hermes-agent" for p in (home / "skills").rglob("*"))
    # Idempotent : un second passage ne cherche pas à « réparer » l'essentiel manquant.
    again = ss.sync_skills(quiet=True)
    assert again["copied"] == [] and again["updated"] == []


def test_sync_box_deja_seedee_nettoie_le_manifeste_sans_erreur(ss_env, monkeypatch):
    """Box provisionnée avec l'ancienne image (skill présent) puis mise à jour : le manifeste est
    nettoyé (entrée « cleaned »), rien ne casse."""
    ss, home = ss_env
    bundled_v1 = _bundled(home.parent / "v1", [("autonomous-ai-agents", "hermes-agent"), ("media", "gif-search")])
    monkeypatch.setattr(ss, "_get_bundled_dir", lambda: bundled_v1)
    assert "hermes-agent" in ss.sync_skills(quiet=True)["copied"]

    bundled_v2 = _bundled(home.parent / "v2", [("media", "gif-search")])
    monkeypatch.setattr(ss, "_get_bundled_dir", lambda: bundled_v2)
    result = ss.sync_skills(quiet=True)
    assert result["cleaned"] == ["hermes-agent"]


def test_essential_names_ne_leve_pas_quand_la_source_manque(ss_env, monkeypatch):
    """Le nom reste « essentiel » côté config (placebo amont), mais aucun code ne l'exige sur disque."""
    ss, _ = ss_env
    assert "hermes-agent" in ss._essential_names()
    from agent.skill_utils import ESSENTIAL_SKILLS, get_disabled_skill_names

    assert "hermes-agent" in ESSENTIAL_SKILLS
    assert "hermes-agent" not in get_disabled_skill_names()  # lecture sans fichier : pas d'exception


def test_dockerfile_supprime_le_skill_apres_la_copie_des_sources():
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    lines = text.splitlines()
    copy_idx = [i for i, ln in enumerate(lines) if re.match(r"^\s*COPY\s+.*\s\.\s+\.\s*$", ln)]
    rm_idx = [
        i for i, ln in enumerate(lines)
        if re.match(r"^\s*RUN\s+rm\s+-rf\s+/opt/hermes/skills/autonomous-ai-agents/hermes-agent\s*$", ln)
    ]
    assert len(copy_idx) == 1, copy_idx
    assert len(rm_idx) == 1, rm_idx
    assert copy_idx[0] < rm_idx[0], "la suppression doit suivre `COPY . .` (sinon elle est écrasée)"
    assert re.search(r"^\s*WORKDIR\s+/opt/hermes\s*$", text, re.M), "chemin de l'image = /opt/hermes"
    # Le répertoire existe bien dans le dépôt amont : la ligne supprime quelque chose de réel.
    assert (REPO_ROOT / "skills" / "autonomous-ai-agents" / "hermes-agent" / "SKILL.md").is_file()


def test_fuite_residuelle_du_prompt_systeme_est_inconditionnelle():
    """PINCE la fuite amont : la guidance pointe vers le skill sans vérifier qu'il existe.

    Casse le jour où l'amont conditionne ``HERMES_AGENT_HELP_GUIDANCE`` à la présence du skill
    (PR amont proposée en F2) → retirer alors la note du registre de dette.
    """
    import inspect

    import agent.system_prompt as sp
    from agent.prompt_builder import HERMES_AGENT_HELP_GUIDANCE

    assert "skill_view(name='hermes-agent')" in HERMES_AGENT_HELP_GUIDANCE
    src = inspect.getsource(sp)
    assert "HERMES_AGENT_HELP_GUIDANCE if _has_skill_view" in src
