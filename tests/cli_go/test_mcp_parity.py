"""`treg mcp install|grants|use-team` from the Go CLI beside the Python CLI: the same output, exit
status and agent configs (Claude Code through its own CLI, Cursor, Codex, opencode), against a
real registry."""
from __future__ import annotations

import json
import os
import stat

import pytest

from .agent_sandbox import both, machine, pair, run_in, same_files, same_run, scrub
from .cli_go_harness import Canned

CURSOR = json.dumps({"mcpServers": {"other": {"command": "npx", "args": ["x"]}, "treg": {"url": "old"}},
                     "theme": "dark", "ratio": 1.50, "name": "café"}, indent=4)
CODEX = ('model = "o3"\n\n[mcp_servers.treg]\nurl = "https://old.example/mcp/"\nbearer_token_env_var = "TREG_TOKEN"\n\n'
         '[mcp_servers.twin]\nurl = "{url}/mcp/"\n\n[profiles.fast]\nmodel = "o4-mini"\n')
OPENCODE = json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {"keep": {"type": "local"}}})


def _install_agents(m, url: str, *, claude: bool = True) -> None:
    m.mkdirs(".cursor", ".codex", ".config/opencode", ".hermes", ".dsh", ".openclaw")
    if claude:
        m.fake_bin("claude")
    m.write(".cursor/mcp.json", CURSOR)
    m.write(".codex/config.toml", CODEX.replace("{url}", url))
    m.write(".config/opencode/opencode.json", OPENCODE)


def _machines(tmp_path, live, team, **config):
    return pair(tmp_path, registry=live.url, token=team.token, active_org=team.slug, identity=True, **config)


def _private(m, rel):
    return stat.S_IMODE((m.home / rel).stat().st_mode)


@pytest.mark.parametrize("argv", [
    ["mcp", "install"],
    ["mcp", "install", "--oauth"],
    ["mcp", "install", "--name", "treg-dev"],
    ["--json", "mcp", "install"],
])
def test_mcp_install_every_agent(tmp_path, live, team, go_bin, argv):
    py_m, go_m = _machines(tmp_path, live, team)
    for m in (py_m, go_m):
        _install_agents(m, live.url)
    py, go = both(py_m, go_m, go_bin, argv)
    same_run(py, go, py_m, go_m)
    same_files(py_m, go_m)
    assert py_m.calls("claude") == go_m.calls("claude")
    assert go_m.calls("claude")[-1][:8] == ["mcp", "add", "--scope", "user", "--transport", "http",
                                            argv[-1] if argv[-1] == "treg-dev" else "treg", live.url + "/mcp/"]
    for rel in (".cursor/mcp.json", ".codex/config.toml", ".config/opencode/opencode.json"):
        assert _private(go_m, rel) == 0o600, rel


def test_mcp_install_fresh_configs(tmp_path, live, team, go_bin):
    """Configs that do not exist yet are created, each 0600 in a directory the writer creates 0700."""
    py_m, go_m = _machines(tmp_path, live, team)
    for m in (py_m, go_m):
        m.mkdirs(".cursor", ".config/opencode")
        m.fake_bin("codex")
    py, go = both(py_m, go_m, go_bin, ["mcp", "install"])
    same_run(py, go, py_m, go_m)
    same_files(py_m, go_m)
    assert _private(go_m, ".codex") == 0o700
    assert _private(go_m, ".codex/config.toml") == 0o600


def test_mcp_install_claude_fails_and_bad_configs(tmp_path, live, team, go_bin):
    py_m, go_m = _machines(tmp_path, live, team)
    for m in (py_m, go_m):
        m.mkdirs(".claude", ".cursor", ".codex", ".config/opencode")
        m.fake_bin("claude")
        m.write(".cursor/mcp.json", "[1, 2]")
        m.write(".codex/config.toml", '[mcp_servers.treg]\nurl = "a"\n\n[mcp_servers.treg.env]\nK = "v"\n')
        m.write(".config/opencode/opencode.json", '{"mcp": 5}')
    py, go = both(py_m, go_m, go_bin, ["mcp", "install"], env={"FAKE_CLAUDE_EXIT": "3"})
    same_run(py, go, py_m, go_m)
    same_files(py_m, go_m)


def test_mcp_install_no_agents(tmp_path, live, team, go_bin):
    py_m, go_m = _machines(tmp_path, live, team)
    for argv in (["mcp", "install"], ["mcp", "install", "--oauth"]):
        py, go = both(py_m, go_m, go_bin, argv)
        same_run(py, go, py_m, go_m)
        same_files(py_m, go_m)


def test_mcp_install_claude_marker_without_cli(tmp_path, live, team, go_bin):
    py_m, go_m = _machines(tmp_path, live, team)
    for m in (py_m, go_m):
        m.mkdirs(".claude")
    py, go = both(py_m, go_m, go_bin, ["mcp", "install"])
    same_run(py, go, py_m, go_m)


def test_mcp_install_refusals(tmp_path, live, team, go_bin):
    bootstrap = live.http.post("/auth/email/verify", json={"email": "mcp-solo@example.com",
                                                           "code": live.email_code("mcp-solo@example.com")}).json()["token"]
    live.http.cookies.clear()
    for i, (config, env) in enumerate((({}, {}), ({"token": bootstrap, "identity": True}, {}),
                                       ({"token": "not-a-token"}, {}), ({}, {"TREG_TOKEN": "also-bad"}))):
        py_m = machine(tmp_path / str(i), "py", registry=live.url, **config)
        go_m = machine(tmp_path / str(i), "go", registry=live.url, **config)
        for m in (py_m, go_m):
            _install_agents(m, live.url)
        py, go = both(py_m, go_m, go_bin, ["mcp", "install"], env=env)
        assert py.code == go.code == 1, (config, py, go)
        same_run(py, go, py_m, go_m)
        same_files(py_m, go_m)
        assert go_m.calls("claude") == []


def test_mcp_install_unreachable(tmp_path, go_bin):
    py_m, go_m = pair(tmp_path, registry="http://127.0.0.1:9", token="tok")
    py, go = both(py_m, go_m, go_bin, ["mcp", "install"])
    assert py.code == go.code == 1
    assert py.out == go.out == b""
    for r in (py, go):
        first, *rest = r.err.splitlines()
        assert first.startswith("Could not reach http://127.0.0.1:9 to verify the token: ")
        assert rest == ["  If this is an SSL or connection error, retry in a moment.",
                        "  If it persists, check your network or try: curl -I http://127.0.0.1:9"]


def test_mcp_install_writes_the_registry_it_talks_to_go(tmp_path, live, team, go_bin):
    """Intentional difference: with TREG_URL set, the server URL written into every agent is the
    registry the CLI talks to; the Python CLI wrote the configured base URL instead."""
    go_m = machine(tmp_path, "go", registry="http://127.0.0.1:9", token=team.token, active_org=team.slug)
    _install_agents(go_m, live.url)
    go = run_in(go_m, [str(go_bin)], ["mcp", "install"], env={"TREG_URL": live.url})
    assert go.code == 0, go
    want = live.url + "/mcp/"
    assert want in go.text
    assert json.loads((go_m.home / ".cursor/mcp.json").read_text())["mcpServers"]["treg"]["url"] == want
    assert f'url = "{want}"' in (go_m.home / ".codex/config.toml").read_text()
    assert go_m.calls("claude")[-1][7] == want
    assert str(go_bin) not in "".join(p.read_text() for p in go_m.home.rglob("*") if p.is_file())


def test_mcp_grants_live(tmp_path, live, team, go_bin):
    py_m, go_m = _machines(tmp_path, live, team)
    for argv in (["mcp", "grants"], ["--json", "mcp", "grants"]):
        py, go = both(py_m, go_m, go_bin, argv)
        assert py.code == go.code == 0
        assert scrub(py.err, py_m) == scrub(go.err, go_m)
        if argv[0] == "--json":
            assert json.loads(py.out) == json.loads(go.out)
        else:
            assert py.text == go.text


GRANTS = [{"grant": "g_0123456789abcdefghijklmnop", "client": "Claude Desktop with a very long client name",
           "team": "a-team-slug-that-is-long", "granted": "2026-11-01T10:20:30.123456"},
          {"grant": "g2", "client": None, "team": None, "granted": None}]


@pytest.mark.parametrize("argv,route,answer", [
    (["mcp", "grants"], "/oauth/grants", Canned(200, {}, json.dumps(GRANTS).encode())),
    (["mcp", "grants"], "/oauth/grants", Canned(401, {"X-Treg-Error": "1"}, b'{"detail":"invalid token"}')),
    (["mcp", "use-team", "g1", "acme"], "/oauth/grants/g1/team",
     Canned(200, {}, b'{"grant":"g1","team":"acme","team_name":"Acme Inc","note":"takes effect on the next call"}')),
    (["mcp", "use-team", "g1", "acme"], "/oauth/grants/g1/team", Canned(200, {}, b'{"grant":"g1","team":"acme"}')),
    (["--json", "mcp", "use-team", "g1", "acme"], "/oauth/grants/g1/team", Canned(200, {}, b'{"grant":"g1","team":"acme"}')),
    (["mcp", "use-team", "g1", "acme"], "/oauth/grants/g1/team", Canned(404, {"X-Treg-Error": "1"}, b'{"detail":"grant not found"}')),
    (["mcp", "use-team", "g1", "acme"], "/oauth/grants/g1/team", Canned(202, {}, b'{"queued":true}')),
])
def test_mcp_grants_and_use_team(tmp_path, fake, go_bin, argv, route, answer):
    fake.route(route, answer)
    py_m, go_m = pair(tmp_path, registry=fake.url, token="tok", active_org="acme")
    start = len(fake.seen)
    py, go = both(py_m, go_m, go_bin, argv)
    assert py.code == go.code, (py, go)
    try:
        assert json.loads(py.out) == json.loads(go.out)
    except ValueError:
        if "use-team" in argv and answer.status == 200 and b"team_name" not in answer.body:
            # Where the registry names no team or note, the Python CLI printed "()" and a blank
            # note line; the Go CLI prints neither.
            assert go.text == "g1 now spends from acme\n", go.text
        else:
            assert scrub(py.text, py_m) == scrub(go.text, go_m), (py.text, go.text)
    assert scrub(py.err, py_m) == scrub(go.err, go_m)
    sent = fake.seen[start:]
    assert [(s.method, s.path, s.body and json.loads(s.body)) for s in sent[:1]] == \
           [(s.method, s.path, s.body and json.loads(s.body)) for s in sent[1:]]


def test_mcp_use_team_live_unknown_grant(tmp_path, live, team, go_bin):
    py_m, go_m = _machines(tmp_path, live, team)
    py, go = both(py_m, go_m, go_bin, ["mcp", "use-team", "nope", team.slug])
    assert py.code == go.code == 1
    assert json.loads(py.out) == json.loads(go.out)
    assert scrub(py.err, py_m) == scrub(go.err, go_m)


def test_mcp_bare_group_is_a_usage_error(tmp_path, live, go_bin):
    py_m, go_m = pair(tmp_path, registry=live.url)
    for argv in (["mcp"], ["skill"], ["mcp", "use-team", "only-one"]):
        py, go = both(py_m, go_m, go_bin, argv)
        assert py.code == go.code == 2, (argv, py, go)
        assert py.out == go.out == b""


def test_mcp_written_tokens_stay_private_on_umask_000(tmp_path, live, team, go_bin):
    go_m = machine(tmp_path, "go", registry=live.url, token=team.token, active_org=team.slug)
    _install_agents(go_m, live.url, claude=False)
    old = os.umask(0)
    try:
        go = run_in(go_m, [str(go_bin)], ["mcp", "install"])
    finally:
        os.umask(old)
    assert go.code == 0, go
    for rel in (".cursor/mcp.json", ".codex/config.toml", ".config/opencode/opencode.json"):
        assert _private(go_m, rel) == 0o600, rel
