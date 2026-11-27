"""The official treg skill: served {BASE}-templated at GET /skill.md and indexed at
/.well-known/skills/."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml


async def test_skill_md_served_and_templated(clients):
    r = await clients.get("/skill.md")
    assert r.status_code == 200
    body = r.text
    assert body.startswith("---") and "name: treg" in body  # loadable skill frontmatter
    assert "{BASE}" not in body                                        # fully templated
    # A marketplace build swaps the parts marked `<!--store:NAME-->`; the served page keeps them.
    assert "<!--store:" not in body and "<!--/store:" not in body
    assert "Call at the listed price without asking" in body


async def test_well_known_skills_index_advertises_the_skill(clients):
    """The agentskills.io convention: a host that serves this is itself a skill source, so treg can
    be installed from treg.to with no directory and no review queue in between."""
    r = await clients.get("/.well-known/skills/index.json")
    assert r.status_code == 200
    skills = r.json()["skills"]
    entry = skills[0]
    assert entry["name"] == "treg"
    assert entry["files"] == ["SKILL.md"]
    assert entry["description"], "the description is what registries index on"
    # It is read from the skill's own frontmatter at request time; a copy hard-coded in api.py
    # would be caught the moment the two disagree.
    served = await clients.get("/skill.md")
    assert f"description: {entry['description']}" in served.text
    # The installer offers the others to the human in one line each; a trigger description is no pitch.
    assert len(skills) > 1 and all(s.get("summary") for s in skills[1:])


async def test_well_known_skill_md_matches_the_canonical_one(clients):
    """The index promises this exact path. It must serve the SAME skill as /skill.md: a second copy
    that drifts is the whole failure mode the generated plugins exist to avoid."""
    r = await clients.get("/.well-known/skills/treg/SKILL.md")
    assert r.status_code == 200
    assert "{BASE}" not in r.text                    # templated to the serving host, like /skill.md
    canonical = await clients.get("/skill.md")
    assert r.text == canonical.text


_PUBLIC_SKILL_PATH = {"make-ugc": "/skills/ugc/SKILL.md"}


async def test_every_indexed_skill_is_served_and_advertised(clients):
    """Each skill the index promises is served at its well-known path and at its public URL, the
    same file, templated to the serving host, with the description the index advertises."""
    skills = (await clients.get("/.well-known/skills/index.json")).json()["skills"][1:]
    for idx in skills:
        name = idx["name"]
        r = await clients.get(_PUBLIC_SKILL_PATH.get(name, f"/skills/{name}/SKILL.md"))
        assert r.status_code == 200 and r.text.startswith(f"---\nname: {name}"), name
        assert "{BASE}" not in r.text, name
        wk = await clients.get(f"/.well-known/skills/{name}/SKILL.md")
        assert wk.text == r.text, name
        assert f"description: {idx['description']}" in r.text, name


async def test_doomscroll_skill_carries_a_working_merge_script(clients, tmp_path):
    """Every install channel ships SKILL.md alone, so the script travels inside it, and it runs."""
    import subprocess
    import sys
    r = await clients.get("/skills/doomscroll/SKILL.md")
    assert r.status_code == 200
    script = tmp_path / "doomscroll.py"
    script.write_text(r.text.split("```python\n", 1)[1].split("\n```", 1)[0])
    run = subprocess.run([sys.executable, "-I", str(script), "--selftest"], capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.strip() == "ok", run.stderr


async def test_web_access_landing_page_installs_the_skill(clients):
    """The /web-access launch page is served with its placeholders filled and points at the skill it sells."""
    r = await clients.get("/web-access")
    assert r.status_code == 200 and "{BASE}" not in r.text
    assert "npx skills add superdesigndev/treg --skill web-access" in r.text
    assert "/web-arena/api/leaderboard" in r.text


async def test_doomscroll_landing_page_installs_the_skill(clients):
    """The /doomscroll page is served with its placeholders filled and points at the skill it sells."""
    r = await clients.get("/doomscroll")
    assert r.status_code == 200 and "{BASE}" not in r.text and "{ENDPOINTS}" not in r.text
    assert "npx skills add superdesigndev/treg --skill doomscroll" in r.text
    assert "/app?ref=doomscroll" in r.text and 'rel="canonical" href="https://treg.to/doomscroll"' in r.text


async def test_opt_in_skills_are_served_but_not_indexed(clients):
    """Served by URL with their placeholders filled (a page's prompt points agents at them), but kept
    out of the index so install.sh never installs them unasked: several spend real money per run."""
    indexed = [s["name"] for s in (await clients.get("/.well-known/skills/index.json")).json()["skills"]]
    for name in ("rea-growth", "rea-marketing", "ai-ugc-video", "jev-memory"):
        r = await clients.get(f"/skills/{name}/SKILL.md")
        assert r.status_code == 200, name
        assert yaml.safe_load(r.text.split("---\n", 2)[1])["name"] == name
        assert "{BASE}" not in r.text, name
        assert name not in indexed


async def test_jev_memory_skill_points_at_the_mod_in_the_repo(clients):
    """The skill points at the Claude Code memory mod in the repo, so that folder has to exist where
    the skill says it is."""
    r = await clients.get("/skills/jev-memory/SKILL.md")
    repo = Path(__file__).resolve().parent.parent
    mod = repo / "examples" / "claude-code-mods"
    assert "examples/claude-code-mods/jev-memory" in r.text
    assert (mod / "jev-memory" / ".claude-plugin" / "plugin.json").is_file()
    market = json.loads((mod / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert f"jev-memory@{market['name']}" in r.text
    assert [p["source"] for p in market["plugins"]] == ["./jev-memory"]


def test_served_skills_name_only_endpoints_that_exist():
    """Several skills name endpoint ids step by step, so an agent calls them as written. A renamed or
    retired row would send it to a 404 mid-run, after money is spent on earlier steps; this fails first."""
    from treg.domain.catalog import store as catalog_store

    ids = {e["id"] for e in catalog_store.load().endpoints}
    skills = sorted((Path(__file__).resolve().parent.parent / "src/treg/web/skills").glob("*/SKILL.md"))
    named = {p.parent.name: set(re.findall(r"`([a-z0-9]+(?:\.[a-z0-9_-]+){2,})`", p.read_text(encoding="utf-8")))
             for p in skills}
    assert len(named["rea-growth"]) > 20 and len(named["rea-marketing"]) > 20 and len(named["ai-ugc-video"]) >= 8
    assert {name: sorted(n - ids) for name, n in named.items() if n - ids} == {}


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
