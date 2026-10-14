"""Runtime attribution + the observed-agents roster.

The treg CLI reports which coding agent it runs inside (X-Treg-Client). The registry stamps it on
the audit trail and derives "detected agents" from it: one row per (member, runtime) — the
zero-setup half of the agents story. Attribution, never authentication: nothing may GATE on it.

Proves: the header lands on CallRecord.client (normalized, versions stripped, junk discarded);
/agents/observed groups by member × runtime and excludes plain-terminal + machine traffic; and the
endpoint is admin-only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event
from sqlmodel import select

from conftest import make_upstream

from treg import audit, crypto
from treg.api import app
from treg.infra.db import _engine as engine, reset_db, session_maker
from treg.models import CallRecord, Membership, Org, User


def _h(t: str, client: str | None = None) -> dict:
    h = {"X-Treg-Token": t}
    if client is not None:
        h["X-Treg-Client"] = client
    return h


async def _mint(email: str, org_id: int, role: str) -> tuple[str, int]:
    token = crypto.new_token()
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u is None:
            u = User(email=email)
            s.add(u)
            await s.flush()
        s.add(Membership(user_id=u.id, org_id=org_id, role=role, token_hash=crypto.hash_token(token)))
        await s.commit()
        uid = u.id
    return token, uid


@pytest.fixture
async def env():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        async with session_maker() as s:
            org = Org(name="Team", slug="team")
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_id = org.id
        owner, _ = await _mint("owner@x.dev", org_id, "owner")
        member, member_uid = await _mint("m@x.dev", org_id, "member")
        sid = (await c.post("/secrets", headers=_h(owner), json={"name": "k", "value": "v"})).json()["id"]
        await c.post("/tools", headers=_h(owner),
                     json={"name": "alpha", "base_url": "http://upstream", "secret_id": sid})
        yield SimpleNamespace(c=c, org_id=org_id, owner=owner, member=member, member_uid=member_uid)
    await app.state.http.aclose()


# ---- the stamp ---------------------------------------------------------------------------------
async def test_client_is_normalized_and_junk_is_discarded(env):
    for sent, stored in (("Claude-Code/1.2.3", "claude-code"), ("weird agent!!", "weirdagent"),
                         (None, "")):
        await env.c.get("/call/alpha/ok", headers=_h(env.member, sent))
    await audit.drain()
    async with session_maker() as s:
        got = {r.client for r in (await s.execute(select(CallRecord))).scalars().all()}
    assert got == {"claude-code", "weirdagent", ""}


# ---- the roster --------------------------------------------------------------------------------
async def test_observed_groups_by_member_and_runtime(env):
    for client in ("claude-code", "claude-code", "codex"):
        await env.c.get("/call/alpha/ok", headers=_h(env.member, client))
    await env.c.get("/call/alpha/ok", headers=_h(env.owner, "claude-code"))
    await audit.drain()
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    key = {(r["member"], r["client"]): r for r in rows}
    assert set(key) == {("m@x.dev", "claude-code"), ("m@x.dev", "codex"),
                        ("owner@x.dev", "claude-code")}
    assert key[("m@x.dev", "claude-code")]["calls_30d"] == 2
    assert key[("m@x.dev", "claude-code")]["used_today"] == 2


async def test_plain_terminal_and_unreported_stay_out(env):
    """`cli` (a human at a prompt) and '' (an SDK or old CLI) would list every member twice."""
    await env.c.get("/call/alpha/ok", headers=_h(env.member, "cli"))
    await env.c.get("/call/alpha/ok", headers=_h(env.member))
    await audit.drain()
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    assert rows == []


async def test_minted_agents_stay_out_of_the_observed_roster(env):
    """A machine identity's calls are already attributed to itself — listing it as 'detected'
    would suggest promoting an agent that already has a token."""
    made = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                            json={"name": "ci-bot"})
    await env.c.get("/call/alpha/ok", headers=_h(made.json()["token"], "claude-code"))
    await audit.drain()
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    assert rows == []


async def test_observed_is_admin_only(env):
    r = await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.member))
    assert r.status_code == 403


async def test_minted_agent_records_its_creator(env):
    await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={"name": "ci-bot"})
    listed = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
    assert listed[0]["created_by"] == "owner@x.dev"


# ---- the CLI detector --------------------------------------------------------------------------
def test_detect_runtime_ignores_config_location_vars(monkeypatch):
    """CODEX_HOME sits in the shell profile of anyone who installed Codex — a config path, not an
    'executing inside Codex' marker. Treating it as one tagged every plain terminal on that machine
    as codex (found on a real machine)."""
    from treg.cli import _detect_runtime
    for var in ("TREG_CLIENT", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
                "CURSOR_AGENT", "CURSOR_TRACE_ID", "GEMINI_CLI", "PI_CODING_AGENT",
                "GITHUB_COPILOT_AGENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CODEX_HOME", "/Users/someone/.codex")
    assert _detect_runtime() == "cli"
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")  # the real execution-time marker still counts
    assert _detect_runtime() == "codex"
    monkeypatch.setenv("CLAUDECODE", "1")
    assert _detect_runtime() == "claude-code"
    monkeypatch.setenv("TREG_CLIENT", "my-agent")
    assert _detect_runtime() == "my-agent"


def test_treg_token_env_overrides_the_config_file(monkeypatch):
    """Per-PROCESS identity: TREG_TOKEN/TREG_ORG in a runtime's env make the CLI act as that
    agent while ~/.treg/config.json stays the human's — several agents on one machine, each with
    its own scope. Without this, every runtime shared the machine-global config identity."""
    from treg.cli import _client
    monkeypatch.setenv("TREG_TOKEN", "agent-token")
    monkeypatch.setenv("TREG_ORG", "test-team")
    c = _client({"base_url": "http://x", "token": "human-token", "active_org": "other"})
    assert c.headers["X-Treg-Token"] == "agent-token"
    assert c.headers["X-Treg-Org"] == "test-team"
    monkeypatch.setenv("TREG_URL", "http://dev-registry:1")
    c = _client({"base_url": "http://x", "token": "human-token", "active_org": "other"})
    assert str(c.base_url).startswith("http://dev-registry:1"), "TREG_URL must ride with the token"
    for var in ("TREG_TOKEN", "TREG_ORG", "TREG_URL"):
        monkeypatch.delenv(var)
    c = _client({"base_url": "http://x", "token": "human-token", "active_org": "other"})
    assert c.headers["X-Treg-Token"] == "human-token"
    assert c.headers["X-Treg-Org"] == "other"


# ---- promotion: a detected pair becomes a real agent --------------------------------------------
async def test_promoted_pair_leaves_the_observed_roster_and_returns_on_revoke(env):
    await env.c.get("/call/alpha/ok", headers=_h(env.member, "claude-code"))
    await audit.drain()
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    assert [(r["member"], r["client"]) for r in rows] == [("m@x.dev", "claude-code")]

    made = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={
        "name": "m-claude-code", "promoted_member": "m@x.dev", "promoted_client": "claude-code"})
    assert made.status_code == 200, made.text
    listed = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
    assert listed[0]["promoted_from"] == "m@x.dev|claude-code"
    assert (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json() == [], \
        "the detected row became this agent — showing both would suggest promoting it twice"

    # revoking the agent resurfaces the pair: the traffic history is still there
    await env.c.delete(f"/orgs/{env.org_id}/agents/{made.json()['user_id']}", headers=_h(env.owner))
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    assert [(r["member"], r["client"]) for r in rows] == [("m@x.dev", "claude-code")]


async def test_promotion_link_survives_a_rotate(env):
    made = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={
        "name": "m-codex", "promoted_member": "m@x.dev", "promoted_client": "codex"})
    assert made.status_code == 200, made.text
    rot = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                           json={"name": "m-codex"})  # the dashboard Rotate shape
    assert rot.status_code == 200, rot.text
    listed = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
    me = next(a for a in listed if a["name"] == "m-codex")
    assert me["promoted_from"] == "m@x.dev|codex", "a rotate must not unlink the promotion"


# ---- product analytics mirror ------------------------------------------------------------------
async def test_call_emits_one_tool_called_event(env, posthog_events):
    """The audit funnel also mirrors each /call to PostHog (when a key is set): one `tool_called`
    per call, carrying the runtime attribution and — for own tools — the upstream host as vendor."""
    r = await env.c.get("/call/alpha/ok", headers=_h(env.member, "claude-code"))
    assert r.status_code == 200
    (e,) = await posthog_events()
    assert e["distinct_id"] == "m@x.dev"
    p = e["properties"]
    assert p["client"] == "claude-code" and p["status_code"] == 200
    assert p["own_tool"] is True and p["tool_name"] == "alpha"
    assert p["provider"] == "upstream"  # own tool → vendor falls back to the upstream host
    assert p["$groups"] == {"team": "team"}
    assert p["outcome"] == "ok" and p["refused_by"] is None
    assert p["call_ref"] == r.headers["X-Treg-Call-Id"]
    assert p["cached"] is False and p["smoothed"] is None and p["hit"] is None
    assert p["ua_family"] == "python-httpx" and p["user_agent"].startswith("python-httpx/")
    assert "capacity_signal" not in p, "a capacity reading belongs to catalog calls only"


async def test_no_key_no_tool_called_events(env):
    from treg import analytics
    analytics._queue.clear()  # default settings: no key
    assert (await env.c.get("/call/alpha/ok", headers=_h(env.member, "codex"))).status_code == 200
    assert analytics._queue == []


# ---- the check-in handshake ---------------------------------------------------------------------
async def test_checkin_flips_connected(env):
    made = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={"name": "ci-bot"})
    listed = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
    assert listed[0]["connected"] is False, "a fresh agent has never called in"

    r = await env.c.post("/agents/checkin", headers=_h(made.json()["token"], "claude-code"))
    assert r.status_code == 200 and r.json()["connected"] is True
    assert r.json()["you"] == made.json()["email"]

    listed = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
    assert listed[0]["connected"] is True, "the poll must see the check-in synchronously"


async def test_members_roster_carries_agent_name_and_owner(env):
    made = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={"name": "ci-bot"})
    assert made.status_code == 200
    members = (await env.c.get(f"/orgs/{env.org_id}/members", headers=_h(env.owner))).json()
    agent = next(m for m in members if m["is_agent"])
    assert agent["name"] == "ci-bot", "the UI shows the short name, never the machine address"
    assert agent["created_by"] == "owner@x.dev"
    human = next(m for m in members if not m["is_agent"])
    assert human["name"] is None and "created_by" in human


def _connection_path(env, agent, key_id=None):
    key_id = agent["api_key_id"] if key_id is None else key_id
    return f"/orgs/{env.org_id}/agents/{agent['user_id']}/connection?api_key_id={key_id}"


async def _agent(env, name="connection-bot"):
    response = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={"name": name})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("action", ["checkin", "call"])
@pytest.mark.parametrize("archive_enabled", [True, False])
async def test_connection_checks_only_the_issued_key(env, action, archive_enabled):
    settings = f"/orgs/{env.org_id}/settings"
    changed = await env.c.patch(settings, headers=_h(env.owner), json={"archive": archive_enabled})
    assert changed.status_code == 200 and changed.json()["archive"] is archive_enabled
    agent = await _agent(env)
    path = _connection_path(env, agent)
    first = await env.c.get(path, headers=_h(env.owner))
    assert first.status_code == 200 and first.json() == {"connected": False}
    assert first.headers["cache-control"] == "no-store"
    if action == "checkin":
        assert (await env.c.post("/agents/checkin", headers=_h(agent["token"]))).status_code == 200
    else:
        assert (await env.c.get("/call/alpha/ok", headers=_h(agent["token"]))).status_code == 200
        await audit.drain()
    assert (await env.c.get(path, headers=_h(env.owner))).json() == {"connected": True}
    assert (await env.c.get(settings, headers=_h(env.owner))).json()["archive"] is archive_enabled


@pytest.mark.parametrize("route", ["agents", "api-keys"])
@pytest.mark.parametrize("archive_enabled", [True, False])
async def test_rotation_requires_a_new_key_checkin(env, route, archive_enabled):
    settings = f"/orgs/{env.org_id}/settings"
    assert (await env.c.patch(settings, headers=_h(env.owner), json={"archive": archive_enabled})).status_code == 200
    old = await _agent(env)
    await env.c.post("/agents/checkin", headers=_h(old["token"]))
    if route == "agents":
        new = await _agent(env)
    else:
        rotated = await env.c.post(f"/orgs/{env.org_id}/api-keys/{old['api_key_id']}/rotate",
                                   headers=_h(env.owner))
        assert rotated.status_code == 200
        data = rotated.json()
        new = {**old, "api_key_id": data["id"], "token": data["secret"]}
    assert new["api_key_id"] != old["api_key_id"]
    assert (await env.c.get(_connection_path(env, old), headers=_h(env.owner))).status_code == 404
    assert (await env.c.get(_connection_path(env, new), headers=_h(env.owner))).json() == {"connected": False}
    assert (await env.c.post("/agents/checkin", headers=_h(old["token"]))).status_code == 401
    assert (await env.c.post("/agents/checkin", headers=_h(new["token"]))).status_code == 200
    assert (await env.c.get(_connection_path(env, new), headers=_h(env.owner))).json() == {"connected": True}
    assert (await env.c.get(settings, headers=_h(env.owner))).json()["archive"] is archive_enabled


async def test_connection_requires_admin_in_the_selected_org(env):
    agent = await _agent(env)
    path = _connection_path(env, agent)
    assert (await env.c.get(path)).status_code == 401
    assert (await env.c.get(path, headers=_h(env.member))).status_code == 403
    viewer, _ = await _mint("viewer@x.dev", env.org_id, "viewer")
    assert (await env.c.get(path, headers=_h(viewer))).status_code == 403
    admin, _ = await _mint("admin@x.dev", env.org_id, "admin")
    assert (await env.c.get(path, headers=_h(admin))).status_code == 200
    async with session_maker() as db:
        other = Org(name="Other", slug="other")
        db.add(other)
        await db.commit()
        other_id = other.id
    other_owner, _ = await _mint("other@x.dev", other_id, "owner")
    assert (await env.c.get(path, headers=_h(other_owner))).status_code == 403
    foreign_path = path.replace(f"/orgs/{env.org_id}/", f"/orgs/{other_id}/")
    assert (await env.c.get(foreign_path, headers=_h(env.owner))).status_code == 403
    assert (await env.c.get(foreign_path, headers=_h(other_owner))).status_code == 404


async def test_connection_binds_the_key_to_an_active_agent_membership(env):
    agent, other = await _agent(env), await _agent(env, "other-bot")
    assert (await env.c.get(_connection_path(env, agent, other["api_key_id"]), headers=_h(env.owner))).status_code == 404
    human_path = _connection_path(env, {**agent, "user_id": env.member_uid})
    assert (await env.c.get(human_path, headers=_h(env.owner))).status_code == 404
    assert (await env.c.get(_connection_path(env, agent, 999999), headers=_h(env.owner))).status_code == 404
    path = _connection_path(env, agent)
    await env.c.post(f"/orgs/{env.org_id}/api-keys/{agent['api_key_id']}/disable", headers=_h(env.owner))
    assert (await env.c.get(path, headers=_h(env.owner))).status_code == 404
    await env.c.post(f"/orgs/{env.org_id}/api-keys/{agent['api_key_id']}/enable", headers=_h(env.owner))
    assert (await env.c.get(path, headers=_h(env.owner))).status_code == 200
    await env.c.delete(f"/orgs/{env.org_id}/agents/{agent['user_id']}", headers=_h(env.owner))
    assert (await env.c.get(path, headers=_h(env.owner))).status_code == 404


def _assert_connection_index_lookup(plan, identity_columns=("api_key_id", "user_email")):
    # EXISTS also emits SCAN CONSTANT ROW; only access to the history table must be a search.
    accesses = [row[3].lower() for row in plan if "callrecord" in row[3].lower()]
    assert len(accesses) == 1, plan
    access = accesses[0]
    assert access.startswith(("search callrecord using index ",
                              "search callrecord using covering index ")), plan
    assert "org_id=?" in access, plan
    assert any(f"{column}=?" in access for column in identity_columns), plan
    assert not any("temp b-tree" in row[3].lower() for row in plan), plan


async def test_connection_uses_one_indexed_exists_and_no_history_aggregation(env):
    agent = await _agent(env)
    statements = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        if "callrecord" in statement.lower():
            statements.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        result = await env.c.get(_connection_path(env, agent), headers=_h(env.owner))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert result.status_code == 200 and result.json() == {"connected": False}
    assert len(statements) == 1
    sql, parameters = statements[0]
    assert "exists" in sql.lower()
    for heavy in ("group by", "distinct", "count(", "sum(", "response_body", "request_body",
                  "error_request", "error_response"):
        assert heavy not in sql.lower()
    if engine.dialect.name == "sqlite":
        async with engine.connect() as conn:
            plan = (await conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)).all()
        # Empty-table estimates may choose either composite index, depending on SQLite's
        # planner/statistics. Require an org + identity search, not a particular index name.
        _assert_connection_index_lookup(plan)

        # A fresh credential for an agent with a long history must not walk that history.
        # Replay the endpoint's actual SQL against the model's indexes in a separate DB so
        # ANALYZE statistics cannot leak into other API tests that share the fixture engine.
        history_engine = create_engine("sqlite://")
        try:
            with history_engine.begin() as conn:
                table = CallRecord.__table__
                table.create(conn)
                record = dict(org_id=env.org_id, user_email=agent["email"], tool_name="test",
                              method="GET", path="/", status_code=200)
                conn.execute(table.insert(), [
                    {**record, "api_key_id": agent["api_key_id"] + offset + 1}
                    for offset in range(64) for _ in range(32)
                ])
                conn.exec_driver_sql("ANALYZE callrecord")
                plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters).all()
                _assert_connection_index_lookup(plan, identity_columns=("api_key_id",))
                assert not conn.exec_driver_sql(sql, parameters).scalar_one()
                conn.execute(table.insert(), {**record, "api_key_id": agent["api_key_id"]})
                assert conn.exec_driver_sql(sql, parameters).scalar_one()
        finally:
            history_engine.dispose()
