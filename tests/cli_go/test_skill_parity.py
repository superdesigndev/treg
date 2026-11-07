"""`treg skill ls|install|bootstrap` from the Go CLI beside the Python CLI: the same output, exit
status and files in the agent skill directories, against a real registry."""
from __future__ import annotations

import json

import pytest

from .agent_sandbox import both, pair, run_in, same_files, same_run, scrub
from .cli_go_harness import Canned, PYTHON_CLI

RECIPE = "---\nname: seo-writer\ndescription: Writes SEO posts.\n---\nCall `treg call seo-writer v1/x`.\n"


@pytest.fixture(scope="session")
def skills_team(live, echo):
    t = live.signup("skills@example.com", "Skill Parity")
    h = {"X-Treg-Token": t.token}
    r = live.http.post("/skills", headers=h, json={
        "name": "seo-writer", "recipe": RECIPE,
        "files": {"scripts/run.py": "print('hi')\n", "notes.md": "# notes\n", "deep/a/b.txt": "b"},
        "secrets": [{"local_name": "key", "value": "sekret", "kind": "env"}],
        "tools": [{"name": "seo-writer", "base_url": echo.url,
                   "bindings": [{"secret": "key", "injector": "env", "location": "header",
                                 "name": "Authorization", "format": "Bearer {secret}"}]}]})
    r.raise_for_status()
    r = live.http.post("/skills", headers=h, json={"name": "notes", "recipe": "---\nname: notes\n---\nnotes\n"})
    r.raise_for_status()
    return t


def _machines(tmp_path, live, team, **config):
    return pair(tmp_path, registry=live.url, token=team.token, active_org=team.slug, identity=True, **config)


@pytest.mark.parametrize("argv", [["skill", "ls"], ["--json", "skill", "ls"]])
def test_skill_ls(tmp_path, live, skills_team, go_bin, argv):
    py_m, go_m = _machines(tmp_path, live, skills_team)
    py, go = both(py_m, go_m, go_bin, argv)
    assert py.code == go.code == 0
    assert json.loads(py.out) == json.loads(go.out)
    assert py.err == go.err == ""


def test_skill_ls_signed_out(tmp_path, live, go_bin):
    py_m, go_m = pair(tmp_path, registry=live.url)
    py, go = both(py_m, go_m, go_bin, ["skill", "ls"])
    assert py.code == go.code == 1
    assert json.loads(py.out) == json.loads(go.out)
    assert scrub(py.err, py_m) == scrub(go.err, go_m)


@pytest.mark.parametrize("argv,agents", [
    (["skill", "install", "seo-writer"], []),
    (["skill", "install", "--all"], []),
    (["skill", "install", "notes", "--agent", "cursor"], []),
    (["skill", "install", "seo-writer", "--global"], [".cursor", ".codex"]),
    (["skill", "install", "seo-writer", "--global"], []),
    (["skill", "install", "--all", "--all-agents"], []),
    (["skill", "install", "--all", "--all-agents", "--global"], []),
    (["skill", "install", "seo-writer", "--dir", "pinned"], []),
    (["skill", "install", "nope"], []),
    (["skill", "install"], []),
])
def test_skill_install(tmp_path, live, skills_team, go_bin, argv, agents):
    py_m, go_m = _machines(tmp_path, live, skills_team)
    for m in (py_m, go_m):
        m.mkdirs(*agents)
    py, go = both(py_m, go_m, go_bin, argv)
    same_run(py, go, py_m, go_m)
    same_files(py_m, go_m)


def test_skill_install_keeps_then_forces(tmp_path, live, skills_team, go_bin):
    py_m, go_m = _machines(tmp_path, live, skills_team)
    for m in (py_m, go_m):
        m.write(".claude/skills/seo-writer/SKILL.md", "my own edits\n")
    for argv in (["skill", "install", "seo-writer"], ["skill", "install", "--all"],
                 ["skill", "install", "seo-writer", "--force"]):
        py, go = both(py_m, go_m, go_bin, argv)
        same_run(py, go, py_m, go_m)
        same_files(py_m, go_m)


def test_skill_install_unsafe_bundles(tmp_path, fake, go_bin):
    """A registry cannot steer a write outside the skill folder."""
    fake.route("/bundles", Canned(200, {}, json.dumps([
        {"id": 1, "name": "../escape"}, {"id": 2, "name": "ok"}, {"id": 2, "name": "ok"},
        {"id": 3, "name": "gone"}, {"id": 4, "name": "empty"}, {"id": 5, "name": ""}]).encode()))
    fake.route("/bundles/2", Canned(200, {}, json.dumps({
        "recipe": "---\nname: ok\n---\n", "tools": [{"name": "t1"}, {"name": "t2"}],
        "files": {"../out.txt": "x", "/abs.txt": "x", "SKILL.md": "x", "a\\b.txt": "back", "link/in.txt": "x",
                  "n/../m.txt": "x", "keep/me.md": "kept", "num": 5}}).encode()))
    fake.route("/bundles/3", Canned(404, {}, b'{"detail":"bundle not found"}'))
    fake.route("/bundles/4", Canned(200, {}, b'{"recipe":"  "}'))
    py_m, go_m = pair(tmp_path, registry=fake.url, token="tok", active_org="acme")
    for m in (py_m, go_m):
        m.mkdirs(".claude/skills/ok", "outside")
        (m.home / ".claude/skills/ok/link").symlink_to(m.home / "outside")
    py, go = both(py_m, go_m, go_bin, ["skill", "install", "--all"])
    same_run(py, go, py_m, go_m)
    same_files(py_m, go_m)
    assert not (go_m.home / "outside" / "in.txt").exists()


def test_skill_install_registry_error(tmp_path, fake, go_bin):
    fake.route("/bundles", Canned(403, {"X-Treg-Error": "1"}, b'{"detail":"finish team setup first"}'))
    py_m, go_m = pair(tmp_path, registry=fake.url, token="tok")
    py, go = both(py_m, go_m, go_bin, ["skill", "install", "x"])
    same_run(py, go, py_m, go_m)


def test_skill_install_public_skill_go(tmp_path, live, skills_team, go_bin):
    """Intentional difference: a name that is one of the registry's public skills installs from its
    skill index instead of failing with "no skill named ... in this org"."""
    py_m, go_m = _machines(tmp_path, live, skills_team)
    py = run_in(py_m, PYTHON_CLI, ["skill", "install", "lead-signals"])
    go = run_in(go_m, [str(go_bin)], ["skill", "install", "lead-signals"])
    assert py.code == 1 and "no skill named 'lead-signals' in this org" in py.err
    assert go.code == 0, go
    assert "✓ lead-signals" in go.text and "a public skill of this registry" in go.text
    for base in (".agents/skills", ".claude/skills"):
        text = (go_m.home / base / "lead-signals" / "SKILL.md").read_text()
        assert text.startswith("---") and "name: lead-signals" in text
    ledger = json.loads((go_m.home / ".treg" / "skills.json").read_text())
    assert len(ledger) == 2
    again = run_in(go_m, [str(go_bin)], ["skill", "install", "lead-signals"])
    assert again.code == 0 and "already on disk - kept your copy" in again.text


def test_skill_install_unknown_agent_go(tmp_path, live, skills_team, go_bin):
    """Intentional difference: names the known agents instead of pointing at `treg agents ls`."""
    _, go_m = _machines(tmp_path, live, skills_team)
    go = run_in(go_m, [str(go_bin)], ["skill", "install", "x", "--agent", "bogus"])
    assert go.code == 1
    assert go.err.startswith('unknown agent "bogus" - one of: claude-code, cursor, codex')


@pytest.mark.parametrize("argv,agents", [
    (["skill", "bootstrap"], []),
    (["skill", "bootstrap"], [".claude", ".cursor", ".config/opencode"]),
    (["skill", "bootstrap", "--project"], []),
    (["skill", "bootstrap", "--all-agents"], []),
    (["skill", "bootstrap", "--skills", "lead-signals, NOPE"], [".codex"]),
    (["skill", "bootstrap", "--skills", "all"], [".claude"]),
    (["--json", "skill", "bootstrap", "--skills", "recruit,zzz"], [".claude"]),
])
def test_skill_bootstrap(tmp_path, live, skills_team, go_bin, argv, agents):
    py_m, go_m = _machines(tmp_path, live, skills_team)
    for m in (py_m, go_m):
        m.mkdirs(*agents)
    py, go = both(py_m, go_m, go_bin, argv)
    same_run(py, go, py_m, go_m)
    same_files(py_m, go_m)


def test_skill_bootstrap_ownership(tmp_path, live, go_bin):
    """Only a SKILL.md treg wrote is refreshed: an edited copy, a foreign one and a pre-ledger copy
    each take their own path, and a lone legacy tools-registry folder is retired."""
    py_m, go_m = pair(tmp_path, registry=live.url)
    for m in (py_m, go_m):
        m.mkdirs(".claude")
    for argv in (["skill", "bootstrap", "--skills", "lead-signals,recruit"],):
        py, go = both(py_m, go_m, go_bin, argv)
        same_run(py, go, py_m, go_m)
    for m in (py_m, go_m):
        m.write(".claude/skills/treg/SKILL.md", "edited by hand\n")
        m.write(".claude/skills/web-access/SKILL.md", f"---\nname: web-access\n---\nsee {live.url}/skill.md\n")
        m.write(".agents/skills/make-ugc/SKILL.md", "---\nname: make-ugc\n---\nmine\n")
        m.write(".claude/skills/tools-registry/SKILL.md", "old\n")
    for argv in (["skill", "bootstrap"], ["skill", "bootstrap", "--skills", "make-ugc"],
                 ["--json", "skill", "bootstrap", "--skills", "all"]):
        py, go = both(py_m, go_m, go_bin, argv)
        same_run(py, go, py_m, go_m)
        same_files(py_m, go_m)


def test_skill_bootstrap_unreachable(tmp_path, go_bin):
    py_m, go_m = pair(tmp_path, registry="http://127.0.0.1:9")
    py, go = both(py_m, go_m, go_bin, ["skill", "bootstrap"])
    assert py.code == go.code == 1
    assert py.out == go.out == b""
    assert py.err.startswith("could not fetch the treg skill from http://127.0.0.1:9: ")
    assert go.err.startswith("could not fetch the treg skill from http://127.0.0.1:9: ")


def test_skill_bootstrap_honours_treg_url_go(tmp_path, live, go_bin):
    """Intentional difference: TREG_URL names the registry for bootstrap too; the Python CLI read
    only the configured base URL."""
    _, go_m = pair(tmp_path, registry="http://127.0.0.1:9")
    go = run_in(go_m, [str(go_bin)], ["skill", "bootstrap"], env={"TREG_URL": live.url})
    assert go.code == 0, go
    assert (go_m.home / ".claude" / "skills" / "treg" / "SKILL.md").read_text().startswith("---")
