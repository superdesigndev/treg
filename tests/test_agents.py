"""The per-agent skills-path registry + target resolution (`treg agents`, skill fan-out).

Pure logic over the registry - no DB, no network. Covers: the two-dir default fan-out, all-agents
targeting, global-scope resolution, install detection, and that env overrides (CLAUDE_CONFIG_DIR,
CODEX_HOME, XDG_CONFIG_HOME) are honored at call time.
"""
from __future__ import annotations

from pathlib import Path

from treg import agents as ag


def test_default_fanout_is_agents_plus_claude():
    bases = ag.resolve_targets()
    assert bases == [Path(".agents/skills"), Path(".claude/skills")]


def test_all_agents_dedupes_shared_bucket():
    dirs = ag.resolve_targets(all_agents=True)
    # every .agents/skills agent collapses to a single entry
    assert dirs.count(Path(".agents/skills")) == 1
    assert Path(".claude/skills") in dirs
    assert Path(".windsurf/skills") in dirs
    # far fewer distinct dirs than agents
    assert len(dirs) < len(ag.AGENTS)


def test_single_agent_global_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert ag.resolve_targets(agent="codex", scope_global=True) == [tmp_path / "cx" / "skills"]


def test_global_scope_uses_detected_or_falls_back(monkeypatch, tmp_path):
    # point every marker into an empty home so nothing is "installed"
    monkeypatch.setattr(ag, "HOME", tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert ag.detect_installed() == []
    bases = ag.resolve_targets(scope_global=True)
    # fallback: claude global + the standard ~/.agents/skills
    assert (tmp_path / ".claude" / "skills") in bases
    assert (tmp_path / ag.STANDARD_PROJECT_DIR) in bases


def test_detect_installed_reads_markers(monkeypatch, tmp_path):
    monkeypatch.setattr(ag, "HOME", tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".claude").mkdir()
    detected = ag.detect_installed()
    assert "cursor" in detected and "claude-code" in detected
    assert "codex" not in detected  # ~/.codex absent


def test_claude_config_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert ag.global_dir("claude-code") == tmp_path / "cc" / "skills"


def _bootstrap(monkeypatch, tmp_path, skills=None, json_out=False, bases=("skills",), overrides=None):
    from types import SimpleNamespace

    from treg import cli

    pages = {
        "/skill.md": "---\nname: treg\n---\nhttps://reg.example/llms.txt\n",
        "/.well-known/skills/index.json": {"skills": [
            {"name": "treg"},
            {"name": "lead-signals", "description": "Use when the user wants buyers.",
             "summary": "Find people ready to buy."},
            {"name": "recruit", "description": "A ranked shortlist."},
            {"name": "../evil"},
        ]},
        "/.well-known/skills/lead-signals/SKILL.md": "---\nname: lead-signals\n---\nhttps://reg.example/llms.txt\n",
        "/.well-known/skills/recruit/SKILL.md": "---\nname: recruit\n---\nhttps://reg.example/llms.txt\n",
        **(overrides or {}),
    }

    def fake_get(url, **_):
        body = pages.get(url.removeprefix("https://reg.example"), "")
        if isinstance(body, Exception):
            raise body
        return SimpleNamespace(status_code=200 if body else 404, text=body if isinstance(body, str) else "",
                               json=lambda: body, raise_for_status=lambda: None)

    monkeypatch.setattr(cli.httpx, "get", fake_get)
    monkeypatch.setattr(cli, "_JSON_OVERRIDE", json_out)
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / ".treg" / "config.json")
    monkeypatch.setattr(ag, "resolve_targets", lambda **_: [tmp_path / b for b in bases])
    monkeypatch.delenv("TREG_TOKEN", raising=False)
    cli.cmd_skill_bootstrap(SimpleNamespace(project=False, all_agents=False, skills=skills),
                            {"base_url": "https://reg.example"})
    return sorted(p.parent.name for p in (tmp_path / bases[0]).glob("*/SKILL.md"))


def _skill(tmp_path, base, name, text):
    path = tmp_path / base / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_skill_bootstrap_installs_only_treg_and_lists_the_rest(monkeypatch, tmp_path, capsys):
    """`treg skill bootstrap` (what install.sh runs) installs the treg skill alone, and prints the
    other advertised skills with the command that adds them, never a hostile name."""
    assert _bootstrap(monkeypatch, tmp_path) == ["treg"]
    out = capsys.readouterr().out
    assert "lead-signals  Find people ready to buy." in out
    assert "treg skill bootstrap --skills all" in out
    assert "evil" not in out


def test_skill_bootstrap_installs_named_and_all(monkeypatch, tmp_path, capsys):
    assert _bootstrap(monkeypatch, tmp_path, skills="Recruit, nope") == ["recruit", "treg"]
    out = capsys.readouterr().out
    assert "Not a skill on this registry: nope" in out
    assert "recruit  " not in out.split("Optional skills")[1]
    assert _bootstrap(monkeypatch, tmp_path, skills="all,recruit") == ["lead-signals", "recruit", "treg"]
    out = capsys.readouterr().out
    assert "Optional skills" not in out and "Not a skill" not in out
    assert not (tmp_path / "evil").exists()


def test_skill_bootstrap_refreshes_its_own_copies_in_place(monkeypatch, tmp_path, capsys):
    """A copy an older bootstrap wrote (a lone SKILL.md naming the skill and linking this registry)
    is refreshed where it is, and not spread to agents that never had it."""
    stale = _skill(tmp_path, "a", "recruit", "---\nname: recruit\n---\nold https://reg.example/x\n")
    _bootstrap(monkeypatch, tmp_path, bases=("a", "b"))
    assert stale.read_text().endswith("https://reg.example/llms.txt\n")
    assert not (tmp_path / "b" / "recruit").exists()


def test_skill_bootstrap_never_overwrites_a_skill_it_did_not_write(monkeypatch, tmp_path, capsys):
    mine = _skill(tmp_path, "a", "recruit", "---\nname: recruit\n---\nmy own recruiting flow\n")
    _bootstrap(monkeypatch, tmp_path, skills="recruit", bases=("a", "b"))
    assert mine.read_text() == "---\nname: recruit\n---\nmy own recruiting flow\n"
    assert (tmp_path / "b" / "recruit" / "SKILL.md").read_text().startswith("---\nname: recruit")
    assert f"Left alone, not written by treg: {mine}" in capsys.readouterr().out


def test_skill_bootstrap_leaves_an_edited_copy_alone(monkeypatch, tmp_path, capsys):
    _bootstrap(monkeypatch, tmp_path, skills="recruit")
    copy = tmp_path / "skills" / "recruit" / "SKILL.md"
    copy.write_text(copy.read_text() + "my note\n")
    _bootstrap(monkeypatch, tmp_path, skills="recruit")
    assert copy.read_text().endswith("my note\n")


def test_skill_bootstrap_reports_an_unreadable_index_and_a_failed_download(monkeypatch, tmp_path, capsys):
    _bootstrap(monkeypatch, tmp_path, skills="recruit",
               overrides={"/.well-known/skills/index.json": OSError("timed out")})
    out = capsys.readouterr().out
    assert "Could not read the skill list" in out and "Not a skill" not in out
    for broken in ({"skills": None}, {"skills": ["recruit"]}, ["recruit"]):
        _bootstrap(monkeypatch, tmp_path, overrides={"/.well-known/skills/index.json": broken})
    assert _bootstrap(monkeypatch, tmp_path, skills="recruit",
                      overrides={"/.well-known/skills/recruit/SKILL.md": ""}) == ["treg"]
    out = capsys.readouterr().out
    assert "Could not download: recruit" in out
    assert "recruit  " not in out.split("Could not download")[1]


def test_skill_bootstrap_json(monkeypatch, tmp_path, capsys):
    import json

    _bootstrap(monkeypatch, tmp_path, skills="lead-signals", json_out=True)
    body = json.loads(capsys.readouterr().out)
    assert body["installed"] == ["treg", "lead-signals"]
    assert body["available"] == [{"name": "recruit", "summary": "A ranked shortlist."}]
    assert body["failed"] == [] and body["skipped"] == []
