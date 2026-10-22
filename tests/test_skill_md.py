"""The official treg skill: served {BASE}-templated at GET /skill.md and indexed at
/.well-known/skills/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


async def test_skill_md_served_and_templated(clients):
    r = await clients.get("/skill.md")
    assert r.status_code == 200
    body = r.text
    assert body.startswith("---") and "name: treg" in body  # loadable skill frontmatter
    assert "{BASE}" not in body                                        # fully templated


async def test_well_known_skills_index_advertises_the_skill(clients):
    """The agentskills.io convention: a host that serves this is itself a skill source, so treg can
    be installed from treg.to with no directory and no review queue in between."""
    r = await clients.get("/.well-known/skills/index.json")
    assert r.status_code == 200
    skills = r.json()["skills"]
    assert [s["name"] for s in skills] == ["treg", "make-ugc", "lead-signals", "web-access", "recruit"]
    entry = skills[0]
    assert entry["name"] == "treg"
    assert entry["files"] == ["SKILL.md"]
    assert entry["description"], "the description is what registries index on"
    # It is read from the skill's own frontmatter at request time; a copy hard-coded in api.py
    # would be caught the moment the two disagree.
    served = await clients.get("/skill.md")
    assert f"description: {entry['description']}" in served.text
    # The installer offers the others to the human in one line each; a trigger description is no pitch.
    assert all(s.get("summary") for s in skills[1:])


async def test_well_known_skill_md_matches_the_canonical_one(clients):
    """The index promises this exact path. It must serve the SAME skill as /skill.md — a second copy
    that drifts is the whole failure mode the generated plugins exist to avoid."""
    r = await clients.get("/.well-known/skills/treg/SKILL.md")
    assert r.status_code == 200
    assert "{BASE}" not in r.text                    # templated to the serving host, like /skill.md
    canonical = await clients.get("/skill.md")
    assert r.text == canonical.text


async def test_make_ugc_skill_is_served_and_advertised(clients):
    """The /ugc workflow as a skill: one public URL an agent can be pointed at, the same file the
    well-known index promises, templated to the serving host like the core skill."""
    r = await clients.get("/skills/ugc/SKILL.md")
    assert r.status_code == 200 and r.text.startswith("---\nname: make-ugc")
    assert "{BASE}" not in r.text
    wk = await clients.get("/.well-known/skills/make-ugc/SKILL.md")
    assert wk.text == r.text
    idx = (await clients.get("/.well-known/skills/index.json")).json()["skills"][1]
    assert f"description: {idx['description']}" in r.text


async def test_lead_signals_skill_is_served_and_advertised(clients):
    """The /leads-signals workflow as a skill, served and indexed exactly like make-ugc."""
    r = await clients.get("/skills/lead-signals/SKILL.md")
    assert r.status_code == 200 and r.text.startswith("---\nname: lead-signals")
    assert "{BASE}" not in r.text
    wk = await clients.get("/.well-known/skills/lead-signals/SKILL.md")
    assert wk.text == r.text
    idx = (await clients.get("/.well-known/skills/index.json")).json()["skills"][2]
    assert f"description: {idx['description']}" in r.text


async def test_web_access_skill_is_served_and_advertised(clients):
    """The web-access guide as a skill, served and indexed exactly like lead-signals."""
    r = await clients.get("/skills/web-access/SKILL.md")
    assert r.status_code == 200 and r.text.startswith("---\nname: web-access")
    assert "{BASE}" not in r.text
    wk = await clients.get("/.well-known/skills/web-access/SKILL.md")
    assert wk.text == r.text
    idx = (await clients.get("/.well-known/skills/index.json")).json()["skills"][3]
    assert f"description: {idx['description']}" in r.text


async def test_recruit_skill_is_served_and_advertised(clients):
    """The recruit workflow as a skill, served and indexed exactly like lead-signals."""
    r = await clients.get("/skills/recruit/SKILL.md")
    assert r.status_code == 200 and r.text.startswith("---\nname: recruit")
    assert "{BASE}" not in r.text
    wk = await clients.get("/.well-known/skills/recruit/SKILL.md")
    assert wk.text == r.text
    idx = (await clients.get("/.well-known/skills/index.json")).json()["skills"][4]
    assert f"description: {idx['description']}" in r.text


async def test_web_access_landing_page_installs_the_skill(clients):
    """The /web-access launch page is served with its placeholders filled and points at the skill it sells."""
    r = await clients.get("/web-access")
    assert r.status_code == 200 and "{BASE}" not in r.text
    assert "npx skills add superdesigndev/treg --skill web-access" in r.text
    assert "/web-arena/api/leaderboard" in r.text


async def test_jev_memory_skill_is_served_but_opt_in(clients):
    """The Claude Code memory mod as a skill, served by URL but kept out of the index, so install.sh
    never installs it unasked. It points at the mod in the repo, so that folder has to exist where
    the skill says it is."""
    r = await clients.get("/skills/jev-memory/SKILL.md")
    assert r.status_code == 200 and r.text.startswith("---\nname: jev-memory")
    assert "{BASE}" not in r.text
    idx = (await clients.get("/.well-known/skills/index.json")).json()["skills"]
    assert "jev-memory" not in [s["name"] for s in idx]
    repo = Path(__file__).resolve().parent.parent
    mod = repo / "examples" / "claude-code-mods"
    assert "examples/claude-code-mods/jev-memory" in r.text
    assert (mod / "jev-memory" / ".claude-plugin" / "plugin.json").is_file()
    market = json.loads((mod / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert f"jev-memory@{market['name']}" in r.text
    assert [p["source"] for p in market["plugins"]] == ["./jev-memory"]


async def test_recruit_landing_page_installs_the_skill(clients):
    """The /recruit launch page is served with its placeholders filled and points at the skill it sells."""
    r = await clients.get("/recruit")
    assert r.status_code == 200 and "{BASE}" not in r.text
    assert "npx skills add superdesigndev/treg --skill recruit" in r.text
    assert "/skills/recruit/SKILL.md" in r.text


REPO = Path(__file__).resolve().parents[1]
PUBLISHED_SKILLS = sorted(
    p
    for pattern in ("src/treg/web/skill.md", "src/treg/web/skills/*/SKILL.md", "skills/*/SKILL.md",
                    "plugin*/**/SKILL.md", "dsh/skills/*/SKILL.md")
    for p in REPO.glob(pattern)
)


@pytest.mark.parametrize("path", PUBLISHED_SKILLS, ids=lambda p: str(p.relative_to(REPO)))
def test_published_skill_frontmatter_is_valid_yaml(path):
    """skills.sh and Claude Code parse the frontmatter as YAML and silently skip a skill that fails."""
    fm = yaml.safe_load(path.read_text().split("---\n", 2)[1])
    assert isinstance(fm, dict) and isinstance(fm.get("name"), str) and isinstance(fm.get("description"), str)
