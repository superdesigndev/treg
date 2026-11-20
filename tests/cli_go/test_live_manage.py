"""Team, tool, secret, connection, audit, feedback, hosting and billing commands against a real
registry, the Go CLI beside the Python CLI. Each CLI works in its own freshly signed-up team, so
the two runs see the same state; ids, slugs, emails, tokens and timestamps are scrubbed before the
outputs are compared."""
from __future__ import annotations

import json
import re
import secrets
import time

import pytest

from .cli_go_harness import PYTHON_CLI, normalize, run, write_config

CLIS = (("py", PYTHON_CLI), ("go", None))


def _fresh_team(live, label: str):
    tag = secrets.token_hex(4)
    team = live.signup(f"{label}-{tag}@example.com", f"Parity {label} {tag}")
    team.name = f"Parity {label} {tag}"
    return team


class Side:
    """One CLI in its own home and team."""

    def __init__(self, tmp_path, live, label: str, cli: list[str], team, *, identity: bool = True):
        self.label, self.cli, self.team = label, cli, team
        self.home = tmp_path / label
        write_config(self.home, base_url=live.url, token=team.token, active_org=team.slug,
                     email=team.email, identity=identity)
        self.values: dict[str, str] = {team.slug: "SLUG", team.email: "EMAIL", team.token: "TOKEN",
                                       getattr(team, "name", team.slug): "NAME"}

    def run(self, argv, **kw):
        return run(self.cli + argv, home=self.home, **kw)

    def config(self) -> dict:
        return json.loads((self.home / ".treg" / "config.json").read_text())

    def scrub(self, text: str) -> str:
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"
        except ValueError:
            pass
        for value, name in sorted(self.values.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(value, name)
        text = re.sub(r'"(\w*_id|id|token|url|expires_at|created_at|updated_at|last_used_at|owner|email)": ?'
                      r'("[^"]*"|\d+)', r'"\1": "-"', text)
        text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?Z?", "TIME", text)
        return normalize(text)


def _sides(tmp_path, live, go_bin, **kw):
    out = []
    for label, cli in CLIS:
        out.append(Side(tmp_path, live, label, cli or [str(go_bin)], _fresh_team(live, label), **kw))
    return out


def _same(py_side, go_side, argv, **kw):
    py, go = py_side.run(argv, **kw), go_side.run(argv, **kw)
    assert py.code == go.code, (argv, py, go)
    assert _json_or_text(py_side.scrub(py.text)) == _json_or_text(go_side.scrub(go.text)), (argv, py.out, go.out)
    assert py_side.scrub(py.err) == go_side.scrub(go.err), (argv, py.err, go.err)
    return py, go


def _json_or_text(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def test_org_lifecycle(tmp_path, live, go_bin, echo):
    py, go = _sides(tmp_path, live, go_bin)
    for side in (py, go):
        live.own_tool(side.team, "echo", echo.url)
    for argv in (["org", "ls"], ["--json", "org", "ls"], ["org", "members"], ["org", "agents"], ["org", "use", "nope"]):
        _same(py, go, argv)
    created = []
    for side in (py, go):
        name = f"Second {side.label} {secrets.token_hex(4)}"
        created.append(side.run(["org", "create", name]))
        side.values[name] = "NAME2"
    assert created[0].code == created[1].code == 0, created
    for side, result in zip((py, go), created):
        made = json.loads(result.out)
        side.values[made["org"]] = "SLUG2"
        side.values[made["token"]] = "TOKEN2"
    assert py.scrub(created[0].text) == go.scrub(created[1].text)
    assert py.scrub(created[0].err) == go.scrub(created[1].err)
    for side, result in zip((py, go), created):
        made = json.loads(result.out)
        cfg = side.config()
        assert (cfg["active_org"], cfg["token"], cfg["identity"]) == (made["org"], made["token"], True)
    _same(py, go, ["org", "ls"])
    for side in (py, go):
        r = side.run(["org", "use", side.team.slug])
        assert (r.code, r.text) == (0, f"active org: {side.team.slug}\n"), r
        cfg = side.config()
        assert cfg["active_org"] == side.team.slug and cfg["token"] != side.values.get("TOKEN2")
    _same(py, go, ["org", "ls"])
    minted = _same(py, go, ["org", "agent-new", "ci-bot", "--tools", "echo", "--cap", "5", "--pin", "Customer=c1"])
    ids = []
    for side, result in zip((py, go), minted):
        assert result.code == 0, result
        agent = json.loads(result.out)
        side.values[agent["token"]] = "AGENT"
        ids.append(str(agent.get("user_id") or agent.get("id")))
    _same(py, go, ["org", "agents"])
    for side, user_id in zip((py, go), ids):
        r = side.run(["org", "agent-rm", user_id])
        assert r.code == 0, r
    _same(py, go, ["org", "agent-rm", "99999999"])


def test_org_use_with_another_teams_key(tmp_path, live, go_bin):
    """An agent-style key cannot switch teams; the stored team and key stay."""
    py, go = _sides(tmp_path, live, go_bin, identity=False)
    _same(py, go, ["org", "use", "someone-else"])
    for side in (py, go):
        assert side.config()["token"] == side.team.token


def test_secrets_and_tools(tmp_path, live, go_bin, echo):
    py, go = _sides(tmp_path, live, go_bin)
    for side in (py, go):
        (side.home / ".env").write_text('export SERVICE_KEY="from env"\n')
        skill = side.home / "skill" / ".secret"
        skill.mkdir(parents=True)
        (skill / "token.json").write_text('{"access_token": "a", "refresh_token": "r"}')
        (side.home / "skill" / "treg.json").write_text(json.dumps({"name": "skill", "base_url": echo.url}))
    added = _same(py, go, ["secret", "add", "PLAIN", "--value", "v1 & <v>"])
    _same(py, go, ["secret", "add", "FROM_ENV", "--env-var", "SERVICE_KEY"])
    _same(py, go, ["secret", "add", "OAUTH", "--dir", "skill", "--kind", "oauth"])
    assert (py.home / "skill" / "treg.json").read_bytes() == (go.home / "skill" / "treg.json").read_bytes()
    _same(py, go, ["secret", "ls"])
    secret_ids = [str(json.loads(r.out)["id"]) for r in added]
    for side, sid in zip((py, go), secret_ids):
        assert side.run(["secret", "update", sid, "--name", "RENAMED"]).code == 0
    _same(py, go, ["secret", "ls"])
    tools = []
    for side, sid in zip((py, go), secret_ids):
        r = side.run(["tool", "add", "echo2", "--base-url", echo.url, "--secret", sid, "--auth-format", "Token {secret}"])
        assert r.code == 0, r
        tools.append(r)
    assert py.scrub(tools[0].text) == go.scrub(tools[1].text)
    tool_ids = [str(json.loads(r.out)["id"]) for r in tools]
    _same(py, go, ["tool", "ls"])
    for args in (["--base-url", echo.url + "/v2"], ["--local-run", "on"], ["--health", '{"path": "status/200"}']):
        results = [side.run(["tool", "update", tid, *args]) for side, tid in zip((py, go), tool_ids)]
        assert results[0].code == results[1].code == 0, results
        assert py.scrub(results[0].text) == go.scrub(results[1].text), args
    _same(py, go, ["tool", "ls"])
    _same(py, go, ["tool", "update", "99999999", "--local-run", "off"])
    _same(py, go, ["tool", "rm", "99999999"])
    _same(py, go, ["secret", "rm", "99999999"])
    for side, tid, sid in zip((py, go), tool_ids, secret_ids):
        assert side.run(["tool", "rm", tid]).code == 0
        assert side.run(["secret", "rm", sid]).code == 0
    _same(py, go, ["tool", "ls"])
    _same(py, go, ["secret", "ls"])


def _wait_for_call(live, team):
    for _ in range(100):
        rows = live.http.get("/calls", headers={"X-Treg-Token": team.token}).json()
        if rows:
            return
        time.sleep(0.1)
    raise AssertionError("the call record never landed")


@pytest.fixture
def busy_team(live, echo):
    team = _fresh_team(live, "shared")
    live.own_tool(team, "echo", echo.url)
    r = live.http.get("/call/echo/v1/items", headers={"X-Treg-Token": team.token})
    assert r.status_code == 200, r.text
    _wait_for_call(live, team)
    return team


@pytest.mark.parametrize("argv", [
    ["health"], ["connections"], ["connections", "ls"], ["connections", "providers"], ["oauth", "providers"],
    ["connections", "resources", "99999999"], ["connections", "rm", "99999999"], ["resources", "list"],
    ["resources", "list", "--provider", "fishaudio", "--include-deleted"], ["calls"], ["calls", "--limit", "1"],
    ["audit"], ["audit", "--calls"], ["audit", "--runs"], ["usage"], ["--json", "usage"], ["usage", "--by", "workspace"],
    ["topup", "10"], ["topup", "--auto", "off"], ["review", "nope", "useful"], ["health", "--run"],
    ["connections", "connect", "--provider", "google-search-console"],
])
def test_shared_reads(tmp_path, live, go_bin, busy_team, argv):
    sides = [Side(tmp_path, live, label, cli or [str(go_bin)], busy_team) for label, cli in CLIS]
    _same(*sides, argv)


def test_feedback_and_review(tmp_path, live, go_bin, busy_team):
    sides = [Side(tmp_path, live, label, cli or [str(go_bin)], busy_team) for label, cli in CLIS]
    calls = live.http.get("/calls", headers={"X-Treg-Token": busy_team.token}).json()
    _same(*sides, ["review", calls[0]["call_id"] if "call_id" in calls[0] else str(calls[0]["id"]), "useful"])
    _same(*sides, ["feedback", "submit", "friction", "The pagination example is unclear.", "--call-id", "c1"])
    _same(*sides, ["feedback", "quality", "outdated result"])
    _same(*sides, ["feedback", "submit", "other", "-"], stdin=b"from stdin\n")


def test_host(tmp_path, live, go_bin, busy_team):
    sides = [Side(tmp_path, live, label, cli or [str(go_bin)], busy_team) for label, cli in CLIS]
    for side in sides:
        (side.home / "face.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    results = [side.run(["host", "face.png"]) for side in sides]
    assert results[0].code == results[1].code, results
    if results[0].code == 0:
        assert all(r.text.startswith(live.url + "/m/") for r in results), results
        assert sides[0].scrub(results[0].err) == sides[1].scrub(results[1].err)
    else:
        assert results[0].text == results[1].text and normalize(results[0].err) == normalize(results[1].err)
    _same(*sides, ["--json", "host", "face.png"])
