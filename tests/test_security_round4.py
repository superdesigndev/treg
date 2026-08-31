"""Round-4 fixes — the bug hunt that closed the agents / projects / deny / local-mode arc.

Four of the five findings are ONE mistake wearing different clothes: **a write path read an ABSENT
value as an UNRESTRICTED one.** An emptied project scope became "every project"; a rotate that sent
no `tool_access` became "every tool". Both are privilege escalations reachable from the ordinary
dashboard, which is why they were release blockers. `test_the_principle_absent_is_never_unrestricted`
pins the rule itself, so the next field added to one of these bodies inherits the guard.

The other two are smaller: a caller with no access to a tool was told the tool did not EXIST (404
instead of 403), and rows outlived the identity they named — a deny rule pointing at a revoked agent,
and a local bootstrap that adopted a team it never created.

Covers: `delete_project` · `create_agent` (rotate) · `_resolve_call` · `_drop_member_deny_rules` ·
`_bootstrap_single_user`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream

from treg import api, crypto
from treg.api import LOCAL_ORG_NAME, LOCAL_USER_EMAIL, app
from treg.config import Settings
from treg.infra.db import reset_db, session_maker
from treg.models import DenyRule, Membership, Org, Tool, User


def _h(t: str) -> dict:
    return {"X-Treg-Token": t}


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


@pytest.fixture
async def env():
    """One org, an owner and a member, and:

    - THREE projects, so scoping to two of them is a real partial scope — `_normalize_project_access`
      collapses an all-projects selection back to NULL, which would hide the very bug under test.
    - three tools on `upstream` (one per project + one org-wide) and one on a SECOND host, so a
      passthrough URL can be made to match only a tool the caller may not use.
    """
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        async with session_maker() as s:
            org = Org(name="Team", slug="team"); s.add(org); await s.commit(); await s.refresh(org)
            org_id = org.id
        owner, owner_uid = await _mint("owner@x.dev", org_id, "owner")
        member, member_uid = await _mint("m@x.dev", org_id, "member")
        sid = (await c.post("/secrets", headers=_h(owner), json={"name": "k", "value": "v"})).json()["id"]
        apollo, gemini, orion = [
            (await c.post(f"/orgs/{org_id}/projects", headers=_h(owner), json={"name": n})).json()
            for n in ("Apollo", "Gemini", "Orion")
        ]
        for name, url, proj in (("a-tool", "http://upstream", apollo["slug"]),
                                ("g-tool", "http://upstream", gemini["slug"]),
                                ("shared", "http://upstream", None),
                                ("hidden", "http://solo-host", None)):
            r = await c.post("/tools", headers=_h(owner), json={
                "name": name, "base_url": url, "secret_id": sid, "project": proj})
            assert r.status_code == 200, r.text
        yield SimpleNamespace(c=c, org_id=org_id, owner=owner, owner_uid=owner_uid,
                              member=member, member_uid=member_uid,
                              apollo=apollo, gemini=gemini, orion=orion)
    await app.state.http.aclose()


async def _agent(env, name="deploy", **kw) -> dict:
    r = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={"name": name, **kw})
    assert r.status_code == 200, r.text
    return r.json()


async def _access(env, user_id: int, **fields) -> dict:
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{user_id}/access", headers=_h(env.owner),
                          json=fields)
    assert r.status_code == 200, r.text
    return r.json()


# ---- #1 deleting a project must not widen a scope ---------------------------------------------
async def test_deleting_a_project_does_not_widen_an_agents_scope(env):
    """The same bug as the member case in test_projects, but through an AGENT — the identity most
    likely to be tightly scoped, and the one whose credential is handed to a machine."""
    agent = await _agent(env)
    await _access(env, agent["user_id"], project_access=[env.apollo["slug"]])
    assert (await env.c.get("/call/g-tool/ok", headers=_h(agent["token"]))).status_code == 403

    r = await env.c.delete(f"/orgs/{env.org_id}/projects/{env.apollo['id']}", headers=_h(env.owner))
    assert r.status_code == 200

    blocked = await env.c.get("/call/g-tool/ok", headers=_h(agent["token"]))
    assert blocked.status_code == 403, \
        "deleting one project must never grant an agent the tools of another"
    names = {t["name"] for t in (await env.c.get("/tools", headers=_h(agent["token"]))).json()}
    assert names == {"a-tool", "shared", "hidden"}, \
        "it keeps what it had (a-tool freed to org-wide) and gains no other project's tools"


async def test_deleting_a_project_leaves_other_scoped_entries_alone(env):
    """A member scoped to two of the three projects loses only the deleted id, not the whole list."""
    await _access(env, env.member_uid, project_access=[env.apollo["slug"], env.gemini["slug"]])
    await env.c.delete(f"/orgs/{env.org_id}/projects/{env.apollo['id']}", headers=_h(env.owner))
    members = (await env.c.get(f"/orgs/{env.org_id}/members", headers=_h(env.owner))).json()
    remaining = next(m for m in members if m["user_id"] == env.member_uid)["project_access"]
    assert remaining == [env.gemini["id"]]
    assert (await env.c.get("/call/g-tool/ok", headers=_h(env.member))).status_code == 200


# ---- #2 rotating an agent must not widen it ----------------------------------------------------
async def test_rotating_an_agent_keeps_its_tool_acl(env):
    """The dashboard's Rotate button sends {name, role, daily_call_cap} and nothing else. With
    `tool_access` defaulting to None ("all tools"), a scoped agent became unrestricted just by
    getting a new token — no admin ever asked for that."""
    agent = await _agent(env, tool_access=["a-tool"])
    assert agent["tool_access"] == ["a-tool"]
    assert (await env.c.get("/call/g-tool/ok", headers=_h(agent["token"]))).status_code == 403

    rotated = await _agent(env, role="member", daily_call_cap=-1)  # exactly what the dashboard sends
    assert rotated["token"] != agent["token"], "a rotate must still replace the token"
    assert rotated["tool_access"] == ["a-tool"], "an absent field must leave the ACL untouched"
    assert (await env.c.get("/call/g-tool/ok", headers=_h(rotated["token"]))).status_code == 403


async def test_rotating_an_agent_keeps_its_cap_project_scope_and_local_run(env):
    """Every other 'absent = unrestricted' field on the same body, including the project scope,
    which `AgentIn` cannot even express and so could only ever be lost."""
    agent = await _agent(env, daily_call_cap=5, local_run_enabled=False)
    await _access(env, agent["user_id"], project_access=[env.apollo["slug"]],
                  local_run_enabled=False)

    rotated = await _agent(env)  # name only — the most minimal rotate there is
    assert rotated["daily_call_cap"] == 5, "an absent cap must not become unlimited"
    assert rotated["local_run_enabled"] is False, "an absent flag must not re-enable local runs"
    assert rotated["project_access"] == [env.apollo["id"]], "a rotate must not clear the project scope"


async def test_rotating_can_still_change_the_limits_when_asked(env):
    """The guard must not freeze the fields: SENT is still authoritative, in both directions."""
    agent = await _agent(env, tool_access=["a-tool"], daily_call_cap=5)
    widened = await _agent(env, tool_access=["a-tool", "g-tool", "shared", "hidden"],
                           daily_call_cap=-1)
    assert widened["tool_access"] is None, "everything selected collapses to NULL, as elsewhere"
    assert widened["daily_call_cap"] == -1
    narrowed = await _agent(env, tool_access=["shared"])
    assert narrowed["tool_access"] == ["shared"]


async def test_a_new_agent_still_takes_the_documented_defaults(env):
    """The 'keep what it had' rule applies to a ROTATE only — a first mint has nothing to keep."""
    fresh = await _agent(env, name="brand-new")
    assert fresh["tool_access"] is None and fresh["daily_call_cap"] == -1
    assert fresh["local_run_enabled"] is True and fresh["role"] == "member"


# ---- the principle behind #1 and #2 ------------------------------------------------------------
async def test_the_principle_absent_is_never_unrestricted(env):
    """The shared rule, stated once: on a path that UPDATES an existing identity, a field the caller
    did not send must leave that field alone. It must never be read as its permissive default.

    Both blockers this round were this rule broken in a different place, so pin it directly: any new
    field added to one of these bodies has to keep passing here."""
    agent = await _agent(env, tool_access=["a-tool"], daily_call_cap=3, local_run_enabled=False)
    await _access(env, agent["user_id"], tool_access=["a-tool"],
                  project_access=[env.apollo["slug"]], local_run_enabled=False)

    async def _limits() -> dict:
        agents = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
        m = next(a for a in agents if a["name"] == "deploy")
        return {k: m[k] for k in ("tool_access", "project_access", "daily_call_cap",
                                  "local_run_enabled")}

    before = await _limits()
    # every write path that can touch this identity without naming its limits
    await _agent(env)                                               # rotate: name only
    await _access(env, agent["user_id"], tool_access=["a-tool"], local_run_enabled=False)
    await env.c.delete(f"/orgs/{env.org_id}/projects/{env.gemini['id']}", headers=_h(env.owner))

    after = await _limits()
    assert after["tool_access"] == before["tool_access"]
    assert after["daily_call_cap"] == before["daily_call_cap"]
    assert after["local_run_enabled"] == before["local_run_enabled"]
    assert after["project_access"] == before["project_access"], \
        "deleting an unrelated project must not touch a scope that never named it"


# ---- #3 no access is a 403, not a 404 ----------------------------------------------------------
async def test_passthrough_without_access_says_403_not_404(env):
    """The ACL filter in `_resolve_call` emptied the candidate set, so the caller was told the tool
    did not EXIST. That sends an admin hunting for a registration that is already there. The named
    shape has always answered 403; the passthrough shape must agree."""
    await _access(env, env.member_uid, tool_access=["shared"])  # everything on solo-host is off-limits
    r = await env.c.get("/call/http://solo-host/ok", headers=_h(env.member))
    assert r.status_code == 403, r.text
    assert "access" in r.text.lower()
    assert "hidden" not in r.text, "the 403 must name only the host the caller typed, not the tool"


async def test_an_unregistered_host_is_still_a_404(env):
    """The other half: 403 must not swallow the genuine 'nothing is registered here' case."""
    r = await env.c.get("/call/http://nowhere.example/ok", headers=_h(env.member))
    assert r.status_code == 404 and "no registered tool" in r.text


async def test_an_out_of_scope_tool_still_cannot_cause_a_409(env):
    """The reason the ACL filter runs BEFORE the tiebreak in the first place — guard it while
    changing the code around it. Three tools share `upstream` with the same base_url, so they would
    all tie; with only ONE of them usable the caller must still get a clean resolve, not a 409."""
    await _access(env, env.member_uid, project_access=[env.apollo["slug"]], tool_access=["a-tool"])
    r = await env.c.get("/call/http://upstream/ok", headers=_h(env.member))
    assert r.status_code == 200, \
        f"exactly one usable tool on the host must resolve, not 409 on the ones it can't see: {r.text}"


# ---- #4 a deny rule must not outlive the identity it named -------------------------------------
async def _rules(env) -> list[dict]:
    return (await env.c.get(f"/orgs/{env.org_id}/deny", headers=_h(env.owner))).json()


async def test_revoking_an_agent_takes_its_deny_rules_with_it(env):
    agent = await _agent(env)
    r = await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner),
                         json={"host": "upstream", "user_id": agent["user_id"]})
    assert r.status_code == 200, r.text
    assert len(await _rules(env)) == 1

    await env.c.delete(f"/orgs/{env.org_id}/agents/{agent['user_id']}", headers=_h(env.owner))
    assert await _rules(env) == [], \
        "a rule naming a caller that no longer exists can never fire — it is only clutter"


async def test_removing_a_member_takes_their_deny_rules_with_them(env):
    await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner),
                     json={"host": "upstream", "user_id": env.member_uid})
    await env.c.delete(f"/orgs/{env.org_id}/members/{env.member_uid}", headers=_h(env.owner))
    assert await _rules(env) == []


async def test_the_sweep_leaves_org_wide_rules_and_other_members_alone(env):
    """It removes rules ABOUT one caller, never the team's own policy."""
    agent = await _agent(env)
    await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner), json={"method": "DELETE"})
    await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner),
                     json={"host": "upstream", "user_id": env.member_uid})
    await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner),
                     json={"host": "upstream", "user_id": agent["user_id"]})

    await env.c.delete(f"/orgs/{env.org_id}/agents/{agent['user_id']}", headers=_h(env.owner))
    left = await _rules(env)
    assert len(left) == 2
    assert {r["scope"] for r in left} == {"org", "member"}
    assert all(r["user_id"] != agent["user_id"] for r in left)
    # and the org-wide rule still bites
    assert (await env.c.delete("/call/shared/ok", headers=_h(env.owner))).status_code == 403


async def test_deleting_a_user_clears_their_rules_in_every_org(env, monkeypatch):
    """`DenyRule.user_id` is a FOREIGN KEY. A super-admin delete that left one behind would dangle —
    Postgres refuses that outright, and SQLite only hides it because it does not enforce FKs."""
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "sa-token")
    from treg.config import get_settings
    get_settings.cache_clear()
    try:
        await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner),
                         json={"host": "upstream", "user_id": env.member_uid})
        r = await env.c.delete(f"/admin/users/{env.member_uid}", headers=_h("sa-token"))
        assert r.status_code == 200, r.text
        async with session_maker() as s:
            left = (await s.execute(select(DenyRule).where(
                DenyRule.user_id == env.member_uid))).scalars().all()
        assert left == [], "no rule may survive the user row it points at"
    finally:
        get_settings.cache_clear()


# ---- #5 local bootstrap must not adopt someone else's team -------------------------------------
def _settings(**kw) -> Settings:
    base = dict(single_user=True, database_url="sqlite+aiosqlite:///./x.db",
                public_url="http://localhost:18790")
    base.update(kw)
    return Settings(**base)


async def test_local_bootstrap_never_claims_an_existing_personal_team(tmp_path, monkeypatch):
    """It used to look the org up by SLUG and join it as owner. On a database that is not fresh —
    a hosted registry someone ran locally, a restored dump — that hands the password-less local
    identity ownership of a real team, and an owner is exempt from every ACL."""
    await reset_db()
    async with session_maker() as s:
        theirs = Org(name="Personal", slug=LOCAL_ORG_NAME); s.add(theirs)
        real = User(email="someone@x.dev"); s.add(real)
        await s.flush()
        s.add(Membership(user_id=real.id, org_id=theirs.id, role="owner",
                         token_hash=crypto.hash_token(crypto.new_token())))
        await s.commit()
        theirs_id = theirs.id

    monkeypatch.setattr(api, "get_settings",
                        lambda: _settings(single_user_token_file=str(tmp_path / "tok")))
    await api._bootstrap_single_user()

    async with session_maker() as s:
        local_user = (await s.execute(
            select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one()
        mems = (await s.execute(select(Membership).where(
            Membership.user_id == local_user.id))).scalars().all()
        orgs = {o.id: o.slug for o in (await s.execute(select(Org))).scalars().all()}
    assert len(mems) == 1
    assert mems[0].org_id != theirs_id, "the local identity must never join a team it did not create"
    assert orgs[mems[0].org_id] != LOCAL_ORG_NAME, "it takes a free slug instead of colliding"
    assert orgs[mems[0].org_id].startswith(LOCAL_ORG_NAME)


async def test_local_bootstrap_is_still_idempotent_on_its_own_team(tmp_path, monkeypatch):
    """The fix must not cost the property the feature is built on: re-running makes no second team
    and does not rotate the token the installer already wrote into the CLI config."""
    await reset_db()
    token_file = tmp_path / "tok"
    monkeypatch.setattr(api, "get_settings",
                        lambda: _settings(single_user_token_file=str(token_file)))
    await api._bootstrap_single_user()
    first = token_file.read_text()
    await api._bootstrap_single_user()
    await api._bootstrap_single_user()

    assert token_file.read_text() == first
    async with session_maker() as s:
        local_user = (await s.execute(
            select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one()
        mems = (await s.execute(select(Membership).where(
            Membership.user_id == local_user.id))).scalars().all()
        orgs = (await s.execute(select(Org))).scalars().all()
    assert len(mems) == 1 and len(orgs) == 1
    assert orgs[0].slug == LOCAL_ORG_NAME, "on a FRESH box it still gets the clean name"
