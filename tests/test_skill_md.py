"""The official treg skill: served {BASE}-templated at GET /skill.md and indexed at
/.well-known/skills/."""

from __future__ import annotations

import json
from pathlib import Path


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
    assert [s["name"] for s in skills] == ["treg", "make-ugc", "lead-signals", "jev-memory"]
    entry = skills[0]
    assert entry["name"] == "treg"
    assert entry["files"] == ["SKILL.md"]
    assert entry["description"], "the description is what registries index on"
    # It is read from the skill's own frontmatter at request time; a copy hard-coded in api.py
    # would be caught the moment the two disagree.
    served = await clients.get("/skill.md")
    assert f"description: {entry['description']}" in served.text


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


async def test_jev_memory_skill_is_served_and_advertised(clients):
    """The Claude Code memory mod as a skill, served and indexed exactly like make-ugc. It points at
    the mod in the repo, so that folder has to exist where the skill says it is."""
    r = await clients.get("/skills/jev-memory/SKILL.md")
    assert r.status_code == 200 and r.text.startswith("---\nname: jev-memory")
    assert "{BASE}" not in r.text
    wk = await clients.get("/.well-known/skills/jev-memory/SKILL.md")
    assert wk.text == r.text
    idx = (await clients.get("/.well-known/skills/index.json")).json()["skills"][3]
    assert f"description: {idx['description']}" in r.text
    repo = Path(__file__).resolve().parent.parent
    mod = repo / "examples" / "claude-code-mods"
    assert "examples/claude-code-mods/jev-memory" in r.text
    assert (mod / "jev-memory" / ".claude-plugin" / "plugin.json").is_file()
    market = json.loads((mod / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert f"jev-memory@{market['name']}" in r.text
    assert [p["source"] for p in market["plugins"]] == ["./jev-memory"]
