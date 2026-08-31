"""Agent identity — a member token for a machine caller.

NOTE: "agent" here is an IDENTITY, not the skill-directory registry in `treg/agents.py`
(covered by test_agents.py). Same word, different concept.

Proves the invariants that make an agent safe to hand out:
  - it can CALL, but can never act as a USER (no org creation → no self-promotion to owner),
  - no login door can resolve an agent address,
  - it can never be an owner (owners bypass the tool ACL),
  - two orgs can own the same agent name without sharing one User row,
  - rotate/revoke kill the old token immediately,
  - it inherits the per-member cap, tool ACL and audit trail for free.
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
from treg.models import Membership, Org, User


def _h(t: str) -> dict:
    return {"X-Treg-Token": t}


async def _mint(email: str, org_id: int, role: str) -> str:
    token = crypto.new_token()
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u is None:
            u = User(email=email); s.add(u); await s.flush()
        s.add(Membership(user_id=u.id, org_id=org_id, role=role, token_hash=crypto.hash_token(token)))
        await s.commit()
    return token


@pytest.fixture
async def env():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        async with session_maker() as s:
            org = Org(name="Team", slug="team"); s.add(org)
            other = Org(name="Other", slug="other"); s.add(other)
            await s.commit(); await s.refresh(org); await s.refresh(other)
            org_id, other_id = org.id, other.id
        owner = await _mint("owner@x.dev", org_id, "owner")
        admin = await _mint("adm@x.dev", org_id, "admin")
        other_owner = await _mint("owner2@x.dev", other_id, "owner")
        # two callable tools, so tool_access has something to discriminate between
        sid = (await c.post("/secrets", headers=_h(owner), json={"name": "k", "value": "v"})).json()["id"]
        for name in ("alpha", "beta"):
            await c.post("/tools", headers=_h(owner),
                         json={"name": name, "base_url": "http://upstream", "secret_id": sid})
        yield SimpleNamespace(c=c, org_id=org_id, other_id=other_id,
                              owner=owner, admin=admin, other_owner=other_owner)
    await app.state.http.aclose()


async def _agent(env, name="deploy", **kw) -> dict:
    r = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={"name": name, **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ---- the security invariants ---------------------------------------------------------------
async def test_agent_can_call_but_cannot_act_as_a_user(env):
    """The load-bearing one. `create_org` runs on require_identity — if an agent could reach it, it
    would make itself OWNER of a fresh org, and owners bypass the tool ACL and local-run gate."""
    tok = (await _agent(env))["token"]
    assert (await env.c.get("/tools", headers=_h(tok))).status_code == 200  # it CAN work in its org

    blocked = await env.c.post("/orgs", headers=_h(tok), json={"name": "escape"})
    assert blocked.status_code == 403, "an agent must never create an org (it would own it)"
    assert "machine identity" in blocked.text
    # and it must not be able to mint a user-scoped identity token, nor accept invites as a user
    assert (await env.c.get("/auth/cli-token", headers=_h(tok))).status_code == 403
    assert (await env.c.get("/invites/mine", headers=_h(tok))).status_code == 403


async def test_no_login_door_can_resolve_an_agent_address(env):
    """The address is unroutable, but the block is explicit so no door can hand a human the identity."""
    email = (await _agent(env))["email"]
    # open registration must not squat/reuse the address
    r = await env.c.post("/users", json={"email": email})
    assert r.status_code == 403 and "cannot be used to sign in" in r.text
    # nor the email one-time-code door: refused up front, so no code is ever minted for it
    r = await env.c.post("/auth/email/start", json={"email": email})
    assert r.status_code == 403 and "cannot be used to sign in" in r.text


async def test_the_verify_door_is_blocked_even_if_a_code_were_obtained(env):
    """Belt and braces: `_find_or_create_user` is the choke point EVERY door shares, so even handed a
    valid code (dev mode returns one for a real address) an agent address cannot be signed in."""
    email = (await _agent(env, name="sneak"))["email"]
    # mint a real code for a REAL address, then try to redeem it against the agent address
    started = await env.c.post("/auth/email/start", json={"email": "human@x.dev"})
    code = started.json().get("dev_code")
    assert code, "dev mode should return the code in the test env"
    r = await env.c.post("/auth/email/verify", json={"email": email, "code": code})
    assert r.status_code != 200, r.text
    async with session_maker() as s:
        rows = (await s.execute(select(User).where(User.email == email))).scalars().all()
    assert len(rows) == 1, "no second identity may be created for an agent address"


async def test_an_agent_can_never_be_an_owner(env):
    """Owners short-circuit _tool_allowed and _require_local_run — an owner agent bypasses everything."""
    r = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                         json={"name": "root", "role": "owner"})
    assert r.status_code == 422 and "cannot be an owner" in r.text

    a = await _agent(env, name="promote-me")
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{a['user_id']}", headers=_h(env.owner),
                          json={"role": "owner"})
    assert r.status_code == 422, "promoting an agent to owner must be refused too"
    assert "machine identity" in r.text


async def test_only_an_owner_can_mint_an_admin_agent(env):
    r = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.admin),
                         json={"name": "power", "role": "admin"})
    assert r.status_code == 403
    r = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                         json={"name": "power", "role": "admin"})
    assert r.status_code == 200, r.text


async def test_two_orgs_own_the_same_agent_name_without_sharing_a_user(env):
    """Without the org in the address both orgs would collide on User.email (unique) and share one
    row — a superadmin suspending one tenant's agent would kill the other tenant's too."""
    mine = await _agent(env, name="deploy")
    theirs = await env.c.post(f"/orgs/{env.other_id}/agents", headers=_h(env.other_owner),
                              json={"name": "deploy"})
    assert theirs.status_code == 200, theirs.text
    assert mine["email"] != theirs.json()["email"]
    assert mine["user_id"] != theirs.json()["user_id"]
    # and each token only reaches its own org
    assert (await env.c.get("/tools", headers=_h(mine["token"]))).status_code == 200
    assert (await env.c.get("/tools", headers=_h(theirs.json()["token"]))).json() == []


# ---- lifecycle -----------------------------------------------------------------------------
async def test_rotate_kills_the_previous_token(env):
    first = await _agent(env, name="ci")
    second = await _agent(env, name="ci")  # same name → rotate
    assert first["token"] != second["token"]
    assert first["user_id"] == second["user_id"], "rotate must reuse the identity, not duplicate it"
    assert (await env.c.get("/tools", headers=_h(first["token"]))).status_code == 401
    assert (await env.c.get("/tools", headers=_h(second["token"]))).status_code == 200


async def test_revoke_kills_the_token(env):
    a = await _agent(env, name="temp")
    r = await env.c.delete(f"/orgs/{env.org_id}/agents/{a['user_id']}", headers=_h(env.owner))
    assert r.status_code == 200, r.text
    assert (await env.c.get("/tools", headers=_h(a["token"]))).status_code == 401
    assert (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json() == []


async def test_agents_are_listed_and_flagged_apart_from_people(env):
    await _agent(env, name="worker")
    agents = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
    assert [a["name"] for a in agents] == ["worker"]
    assert "token" not in agents[0], "the token is shown once at mint time, never listed"

    members = (await env.c.get(f"/orgs/{env.org_id}/members", headers=_h(env.owner))).json()
    by_email = {m["email"]: m for m in members}
    assert by_email[agents[0]["email"]]["is_agent"] is True
    assert by_email["owner@x.dev"]["is_agent"] is False


# ---- it inherits the per-member controls for free -------------------------------------------
async def test_agent_inherits_the_tool_acl(env):
    """tool_access is enforced by _require_tool_access — the agent gets it with no new code."""
    tok = (await _agent(env, name="scoped", tool_access=["alpha"]))["token"]
    assert (await env.c.get("/call/alpha/ok", headers=_h(tok))).status_code == 200
    assert (await env.c.get("/call/beta/ok", headers=_h(tok))).status_code == 403
    # and the restricted agent only SEES what it may use
    assert [t["name"] for t in (await env.c.get("/tools", headers=_h(tok))).json()] == ["alpha"]


async def test_agent_inherits_its_own_daily_cap(env):
    """A cap of 0 proves the cap is read from the agent's own membership, not the creator's."""
    tok = (await _agent(env, name="capped", daily_call_cap=0))["token"]
    assert (await env.c.get("/call/alpha/ok", headers=_h(tok))).status_code == 429
    assert (await env.c.get("/call/alpha/ok", headers=_h(env.owner))).status_code == 200


async def test_agent_traffic_is_attributed_to_the_agent(env):
    """Per-agent logging falls out of identity: CallRecord already stamps user_email."""
    a = await _agent(env, name="logger")
    assert (await env.c.get("/call/alpha/ok", headers=_h(a["token"]))).status_code == 200
    calls = (await env.c.get("/calls", headers=_h(env.owner))).json()
    assert any(c["user_email"] == a["email"] and c["tool_name"] == "alpha" for c in calls), calls

    usage = (await env.c.get(f"/orgs/{env.org_id}/usage", headers=_h(env.owner))).json()
    assert any(row["user_email"] == a["email"] for row in usage["by_user"]), usage["by_user"]


async def test_bad_input_is_refused_cleanly(env):
    for payload, code in (({"name": ""}, 422), ({"name": "  "}, 422),
                          ({"name": "x", "role": "wizard"}, 422),
                          ({"name": "x", "daily_call_cap": -5}, 422),
                          ({"name": "x", "tool_access": ["nope"]}, 422)):
        r = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json=payload)
        assert r.status_code == code, f"{payload} → {r.status_code} {r.text}"


async def test_a_plain_member_cannot_mint_or_list_agents(env):
    member = await _mint("plain@x.dev", env.org_id, "member")
    assert (await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(member),
                             json={"name": "sneaky"})).status_code == 403
    assert (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(member))).status_code == 403


async def test_the_agent_listing_carries_everything_the_dashboard_renders(env):
    """The Agents screen renders role, cap, usage and BOTH access axes — a missing field would show
    as a blank column, so pin the shape here."""
    await _agent(env, name="ui-bot")
    row = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()[0]
    for field in ("user_id", "name", "email", "role", "daily_call_cap", "used_today",
                  "tool_access", "project_access", "local_run_enabled"):
        assert field in row, f"{field} missing from the agent listing"
