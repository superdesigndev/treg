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
from sqlmodel import select

from conftest import make_upstream

from treg import crypto
from treg.api import app
from treg.infra.db import reset_db, session_maker
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
            u = User(email=email); s.add(u); await s.flush()
        s.add(Membership(user_id=u.id, org_id=org_id, role=role, token_hash=crypto.hash_token(token)))
        await s.commit()
        uid = u.id
    return token, uid


async def _drain_audit() -> None:
    """audit writes are fire-and-forget tasks — let them land before asserting."""
    import asyncio
    from treg import audit
    while audit._pending:
        await asyncio.sleep(0)


@pytest.fixture
async def env():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        async with session_maker() as s:
            org = Org(name="Team", slug="team"); s.add(org); await s.commit(); await s.refresh(org)
            org_id = org.id
        owner, _ = await _mint("owner@x.dev", org_id, "owner")
        member, member_uid = await _mint("m@x.dev", org_id, "member")
        sid = (await c.post("/secrets", headers=_h(owner), json={"name": "k", "value": "v"})).json()["id"]
        await c.post("/tools", headers=_h(owner),
                     json={"name": "alpha", "base_url": "http://upstream", "secret_id": sid})
        yield SimpleNamespace(c=c, org_id=org_id, owner=owner, member=member, member_uid=member_uid)
    await app.state.http.aclose()


# ---- the stamp ---------------------------------------------------------------------------------
async def test_client_header_lands_on_the_audit_row(env):
    r = await env.c.get("/call/alpha/ok", headers=_h(env.member, "claude-code"))
    assert r.status_code == 200
    await _drain_audit()
    async with session_maker() as s:
        rec = (await s.execute(select(CallRecord))).scalars().one()
        assert rec.client == "claude-code" and rec.user_email == "m@x.dev"


async def test_client_is_normalized_and_junk_is_discarded(env):
    for sent, stored in (("Claude-Code/1.2.3", "claude-code"), ("weird agent!!", "weirdagent"),
                         (None, "")):
        await env.c.get("/call/alpha/ok", headers=_h(env.member, sent))
    await _drain_audit()
    async with session_maker() as s:
        got = {r.client for r in (await s.execute(select(CallRecord))).scalars().all()}
    assert got == {"claude-code", "weirdagent", ""}


# ---- the roster --------------------------------------------------------------------------------
async def test_observed_groups_by_member_and_runtime(env):
    for client in ("claude-code", "claude-code", "codex"):
        await env.c.get("/call/alpha/ok", headers=_h(env.member, client))
    await env.c.get("/call/alpha/ok", headers=_h(env.owner, "claude-code"))
    await _drain_audit()
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
    await _drain_audit()
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    assert rows == []


async def test_minted_agents_stay_out_of_the_observed_roster(env):
    """A machine identity's calls are already attributed to itself — listing it as 'detected'
    would suggest promoting an agent that already has a token."""
    made = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                            json={"name": "ci-bot"})
    await env.c.get("/call/alpha/ok", headers=_h(made.json()["token"], "claude-code"))
    await _drain_audit()
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
    monkeypatch.delenv("TREG_TOKEN"); monkeypatch.delenv("TREG_ORG"); monkeypatch.delenv("TREG_URL")
    c = _client({"base_url": "http://x", "token": "human-token", "active_org": "other"})
    assert c.headers["X-Treg-Token"] == "human-token"
    assert c.headers["X-Treg-Org"] == "other"


# ---- promotion: a detected pair becomes a real agent --------------------------------------------
async def test_promoted_pair_leaves_the_observed_roster_and_returns_on_revoke(env):
    await env.c.get("/call/alpha/ok", headers=_h(env.member, "claude-code"))
    await _drain_audit()
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
    listed = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
    me = next(a for a in listed if a["name"] == "m-codex")
    assert me["promoted_from"] == "m@x.dev|codex", "a rotate must not unlink the promotion"


# ---- product analytics mirror ------------------------------------------------------------------
async def test_call_emits_one_tool_called_event(env, monkeypatch):
    """The audit funnel also mirrors each /call to PostHog (when a key is set): one `tool_called`
    per call, carrying the runtime attribution and — for own tools — the upstream host as vendor."""
    from treg import analytics
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test_suite", raising=False)

    async def _no_post(batch):  # the flusher must not reach the real PostHog host
        pass
    monkeypatch.setattr(analytics, "_post", _no_post)
    analytics._queue.clear()

    r = await env.c.get("/call/alpha/ok", headers=_h(env.member, "claude-code"))
    assert r.status_code == 200
    events = [e for e in analytics._queue if e["event"] == "tool_called"]
    assert len(events) == 1
    e = events[0]
    assert e["distinct_id"] == "m@x.dev"
    p = e["properties"]
    assert p["client"] == "claude-code" and p["status_code"] == 200
    assert p["own_tool"] is True and p["tool_name"] == "alpha"
    assert p["provider"] == "upstream"  # own tool → vendor falls back to the upstream host
    assert p["$groups"] == {"team": "team"}
    await analytics.drain()
    analytics._queue.clear()


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
