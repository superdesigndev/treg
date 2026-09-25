"""The official treg skill: served {BASE}-templated at GET /skill.md and indexed at
/.well-known/skills/."""

from __future__ import annotations


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
    assert [s["name"] for s in skills] == ["treg", "make-ugc"]
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


