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


def _bootstrap(monkeypatch, tmp_path, skills=None, json_out=False):
    from types import SimpleNamespace

    from treg import cli

    pages = {
        "/skill.md": "---\nname: treg\n---\n",
        "/.well-known/skills/index.json": {"skills": [
            {"name": "treg"},
            {"name": "lead-signals", "description": "Find in-market buyers."},
            {"name": "recruit", "description": "A ranked shortlist."},
            {"name": "../evil"},
        ]},
        "/.well-known/skills/lead-signals/SKILL.md": "---\nname: lead-signals\n---\n",
        "/.well-known/skills/recruit/SKILL.md": "---\nname: recruit\n---\n",
    }

    def fake_get(url, **_):
        body = pages.get(url.removeprefix("https://reg.example"), "")
        return SimpleNamespace(status_code=200 if body else 404, text=body if isinstance(body, str) else "",
                               json=lambda: body, raise_for_status=lambda: None)

    monkeypatch.setattr(cli.httpx, "get", fake_get)
    monkeypatch.setattr(cli, "_JSON_OVERRIDE", json_out)
    monkeypatch.setattr(ag, "resolve_targets", lambda **_: [tmp_path / "skills"])
    monkeypatch.delenv("TREG_TOKEN", raising=False)
    cli.cmd_skill_bootstrap(SimpleNamespace(project=False, all_agents=False, skills=skills),
                            {"base_url": "https://reg.example"})
    return sorted(p.parent.name for p in (tmp_path / "skills").glob("*/SKILL.md"))


def test_skill_bootstrap_installs_only_treg_and_lists_the_rest(monkeypatch, tmp_path, capsys):
    """`treg skill bootstrap` (what install.sh runs) installs the treg skill alone, and prints the
    other advertised skills with the command that adds them, never a hostile name."""
    assert _bootstrap(monkeypatch, tmp_path) == ["treg"]
    out = capsys.readouterr().out
    assert "lead-signals  Find in-market buyers." in out
    assert "treg skill bootstrap --skills all" in out
    assert "evil" not in out


def test_skill_bootstrap_installs_named_and_all(monkeypatch, tmp_path, capsys):
    assert _bootstrap(monkeypatch, tmp_path, skills="recruit, nope") == ["recruit", "treg"]
    out = capsys.readouterr().out
    assert "Not a skill on this registry: nope" in out
    assert "recruit  " not in out.split("Optional skills")[1]
    assert _bootstrap(monkeypatch, tmp_path, skills="all") == ["lead-signals", "recruit", "treg"]
    assert "Optional skills" not in capsys.readouterr().out
    assert not (tmp_path / "evil").exists()


def test_skill_bootstrap_refreshes_an_installed_optional_skill(monkeypatch, tmp_path, capsys):
    stale = tmp_path / "skills" / "recruit" / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    assert _bootstrap(monkeypatch, tmp_path) == ["recruit", "treg"]
    assert stale.read_text() == "---\nname: recruit\n---\n"


def test_skill_bootstrap_json(monkeypatch, tmp_path, capsys):
    import json

    _bootstrap(monkeypatch, tmp_path, skills="lead-signals", json_out=True)
    body = json.loads(capsys.readouterr().out)
    assert body["installed"] == ["treg", "lead-signals"]
    assert body["available"] == [{"name": "recruit", "description": "A ranked shortlist."}]
