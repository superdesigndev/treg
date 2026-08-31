"""Projects — an optional sub-scope inside an org.

The org stays the hard isolation boundary; a project is a softer grouping on top of it, and a
deliberate LABEL + ACL scope rather than a namespace (tool names stay unique per org, so no unique
constraint had to be rebuilt).

Proves: NULL everywhere reproduces the old behavior exactly; project scope AND tool_access compose;
`project_access=[X]` + `tool_access=NULL` auto-inherits tools added later; a tool in an out-of-scope
project can't cause a 409 for a caller who can't even use it (the reason `_resolve_call` filters
before the tiebreak); and deleting a project frees its tools instead of hiding them.
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
from treg.models import Membership, Org, Tool, User


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
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        async with session_maker() as s:
            org = Org(name="Team", slug="team"); s.add(org); await s.commit(); await s.refresh(org)
            org_id = org.id
        owner, _ = await _mint("owner@x.dev", org_id, "owner")
        member, member_uid = await _mint("m@x.dev", org_id, "member")
        sid = (await c.post("/secrets", headers=_h(owner), json={"name": "k", "value": "v"})).json()["id"]
        apollo = (await c.post(f"/orgs/{org_id}/projects", headers=_h(owner),
                               json={"name": "Apollo"})).json()
        gemini = (await c.post(f"/orgs/{org_id}/projects", headers=_h(owner),
                               json={"name": "Gemini"})).json()
        # one tool per project, plus one left org-wide
        for name, proj in (("a-tool", apollo["slug"]), ("g-tool", gemini["slug"]), ("shared", None)):
            await c.post("/tools", headers=_h(owner), json={
                "name": name, "base_url": "http://upstream", "secret_id": sid, "project": proj})
        yield SimpleNamespace(c=c, org_id=org_id, owner=owner, member=member, member_uid=member_uid,
                              apollo=apollo, gemini=gemini, sid=sid)
    await app.state.http.aclose()


async def _scope(env, projects, tools=None) -> None:
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{env.member_uid}/access", headers=_h(env.owner),
                          json={"project_access": projects, "tool_access": tools})
    assert r.status_code == 200, r.text


# ---- nothing changes until you use it --------------------------------------------------------
async def test_unscoped_members_see_and_call_everything(env):
    """NULL project_access = the whole org. This is the regression guard for every existing org."""
    names = {t["name"] for t in (await env.c.get("/tools", headers=_h(env.member))).json()}
    assert names == {"a-tool", "g-tool", "shared"}
    for n in names:
        assert (await env.c.get(f"/call/{n}/ok", headers=_h(env.member))).status_code == 200


async def test_a_tool_with_no_project_is_org_wide(env):
    """Every tool that predates projects has project_id NULL — it must stay visible to everyone."""
    await _scope(env, [env.apollo["slug"]])
    names = {t["name"] for t in (await env.c.get("/tools", headers=_h(env.member))).json()}
    assert "shared" in names, "an org-wide tool must remain in scope for a project-scoped member"
    assert (await env.c.get("/call/shared/ok", headers=_h(env.member))).status_code == 200


# ---- the scope itself -------------------------------------------------------------------------
async def test_project_scope_gates_listing_and_calling(env):
    await _scope(env, [env.apollo["slug"]])
    names = {t["name"] for t in (await env.c.get("/tools", headers=_h(env.member))).json()}
    assert names == {"a-tool", "shared"}, "the ACL hides what it gates"
    assert (await env.c.get("/call/a-tool/ok", headers=_h(env.member))).status_code == 200
    blocked = await env.c.get("/call/g-tool/ok", headers=_h(env.member))
    assert blocked.status_code == 403 and "project" in blocked.text


async def test_the_owner_is_never_scoped(env):
    await _scope(env, [env.apollo["slug"]])
    assert (await env.c.get("/call/g-tool/ok", headers=_h(env.owner))).status_code == 200


async def test_project_and_tool_access_compose_as_and(env):
    """Both axes must allow it: in-project but not in tool_access is still refused."""
    await _scope(env, [env.apollo["slug"]], tools=["shared"])
    assert (await env.c.get("/call/shared/ok", headers=_h(env.member))).status_code == 200
    assert (await env.c.get("/call/a-tool/ok", headers=_h(env.member))).status_code == 403


async def test_a_scoped_member_auto_inherits_new_tools_in_their_project(env):
    """project_access=[X] + tool_access=NULL is the composition that makes the coarse dial useful."""
    await _scope(env, [env.apollo["slug"]])
    await env.c.post("/tools", headers=_h(env.owner), json={
        "name": "a-later", "base_url": "http://upstream", "secret_id": env.sid,
        "project": env.apollo["slug"]})
    names = {t["name"] for t in (await env.c.get("/tools", headers=_h(env.member))).json()}
    assert "a-later" in names
    assert (await env.c.get("/call/a-later/ok", headers=_h(env.member))).status_code == 200


async def test_selecting_every_project_collapses_to_unrestricted(env):
    """Mirrors _normalize_tool_access: 'all checked' is stored as NULL so later projects are inherited."""
    await _scope(env, [env.apollo["slug"], env.gemini["slug"]])
    members = (await env.c.get(f"/orgs/{env.org_id}/members", headers=_h(env.owner))).json()
    me = next(m for m in members if m["user_id"] == env.member_uid)
    assert me["project_access"] is None


# ---- the reason _resolve_call filters BEFORE the tiebreak -------------------------------------
async def test_an_out_of_scope_tool_cannot_409_the_passthrough(env):
    """Two same-host tools in different projects would tie on prefix length. A caller scoped to one
    of them must resolve cleanly — the other is invisible to them, so it must not cause a 409."""
    await env.c.post("/tools", headers=_h(env.owner), json={
        "name": "g-same-host", "base_url": "http://upstream", "secret_id": env.sid,
        "project": env.gemini["slug"]})
    await _scope(env, [env.apollo["slug"]])
    # org-wide + apollo tools still share the host, so narrow the member to exactly one usable tool
    await _scope(env, [env.apollo["slug"]], tools=["a-tool"])
    r = await env.c.get("/call/http://upstream/x", headers=_h(env.member))
    assert r.status_code == 200, f"expected a clean resolve, got {r.status_code}: {r.text[:200]}"


# ---- lifecycle ---------------------------------------------------------------------------------
async def test_projects_are_listed_with_their_tool_counts(env):
    projects = (await env.c.get(f"/orgs/{env.org_id}/projects", headers=_h(env.owner))).json()
    counts = {p["slug"]: p["tool_count"] for p in projects}
    assert counts == {"apollo": 1, "gemini": 1}


async def test_a_scoped_member_only_sees_their_projects(env):
    await _scope(env, [env.apollo["slug"]])
    projects = (await env.c.get(f"/orgs/{env.org_id}/projects", headers=_h(env.member))).json()
    assert [p["slug"] for p in projects] == ["apollo"]


async def test_deleting_a_project_frees_its_tools_rather_than_hiding_them(env):
    r = await env.c.delete(f"/orgs/{env.org_id}/projects/{env.apollo['id']}", headers=_h(env.owner))
    assert r.status_code == 200 and r.json()["tools_made_org_wide"] == 1
    async with session_maker() as s:
        tool = (await s.execute(select(Tool).where(Tool.name == "a-tool"))).scalar_one()
        assert tool.project_id is None, "the tool survives as org-wide; it must not vanish"
    assert (await env.c.get("/call/a-tool/ok", headers=_h(env.member))).status_code == 200


async def test_deleting_a_project_neither_locks_a_member_out_nor_widens_them(env):
    """Dropping the LAST entry must leave an empty scope, never NULL.

    NULL reads as "every project", so collapsing `[]` to NULL would hand a member scoped to only
    Apollo the run of Gemini's tools — a privilege escalation fired by an unrelated delete. The
    member is still not locked out, because the freed tools became org-wide: what they could reach
    before the delete they can still reach after it. That is the invariant, not the NULL.
    """
    await _scope(env, [env.apollo["slug"]])
    await env.c.delete(f"/orgs/{env.org_id}/projects/{env.apollo['id']}", headers=_h(env.owner))
    members = (await env.c.get(f"/orgs/{env.org_id}/members", headers=_h(env.owner))).json()
    assert next(m for m in members if m["user_id"] == env.member_uid)["project_access"] == [], \
        "an emptied scope must stay empty — NULL would mean 'every project'"
    # kept: the tools they already had (a-tool was freed to org-wide, shared always was)
    assert (await env.c.get("/call/a-tool/ok", headers=_h(env.member))).status_code == 200
    assert (await env.c.get("/call/shared/ok", headers=_h(env.member))).status_code == 200
    # NOT gained: a project they were deliberately kept out of
    blocked = await env.c.get("/call/g-tool/ok", headers=_h(env.member))
    assert blocked.status_code == 403, "deleting Apollo must not grant Gemini"


async def test_a_tool_can_be_moved_between_projects_and_back(env):
    tools = (await env.c.get("/tools", headers=_h(env.owner))).json()
    tid = next(t["id"] for t in tools if t["name"] == "a-tool")
    r = await env.c.patch(f"/tools/{tid}", headers=_h(env.owner), json={"project": env.gemini["slug"]})
    assert r.status_code == 200 and r.json()["project_id"] == env.gemini["id"]
    r = await env.c.patch(f"/tools/{tid}", headers=_h(env.owner), json={"project": None})
    assert r.status_code == 200 and r.json()["project_id"] is None


# ---- guards --------------------------------------------------------------------------------------
async def test_bad_input_is_refused(env):
    assert (await env.c.post(f"/orgs/{env.org_id}/projects", headers=_h(env.owner),
                             json={"name": " "})).status_code == 422
    assert (await env.c.post(f"/orgs/{env.org_id}/projects", headers=_h(env.owner),
                             json={"name": "Apollo"})).status_code == 409, "slug must be unique per org"
    assert (await env.c.post("/tools", headers=_h(env.owner), json={
        "name": "bad", "base_url": "http://upstream", "project": "nope"})).status_code == 404
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{env.member_uid}/access", headers=_h(env.owner),
                          json={"project_access": ["nope"]})
    assert r.status_code == 422, "an unknown project must not be silently ignored"


async def test_only_admins_manage_projects(env):
    assert (await env.c.post(f"/orgs/{env.org_id}/projects", headers=_h(env.member),
                             json={"name": "Sneaky"})).status_code == 403
    assert (await env.c.delete(f"/orgs/{env.org_id}/projects/{env.apollo['id']}",
                               headers=_h(env.member))).status_code == 403


async def test_another_orgs_project_is_never_reachable(env):
    async with session_maker() as s:
        other = Org(name="Other", slug="other"); s.add(other); await s.commit(); await s.refresh(other)
        other_id = other.id
    tok, _ = await _mint("o@x.dev", other_id, "owner")
    assert (await env.c.delete(f"/orgs/{other_id}/projects/{env.apollo['id']}",
                               headers=_h(tok))).status_code == 404
    assert (await env.c.get(f"/orgs/{other_id}/projects", headers=_h(tok))).json() == []


async def test_an_invite_carries_the_project_scope_onto_the_membership(env):
    r = await env.c.post(f"/orgs/{env.org_id}/invites", headers=_h(env.owner), json={
        "email": "new@x.dev", "project_access": [env.apollo["slug"]]})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    joined = await env.c.post("/invites/accept", json={"code": code, "email": "new@x.dev"})
    assert joined.status_code == 200, joined.text
    names = {t["name"] for t in (await env.c.get("/tools", headers=_h(joined.json()["token"]))).json()}
    assert names == {"a-tool", "shared"}


async def test_an_access_patch_without_projects_keeps_the_scope(env):
    """The dashboard's local-run toggle PATCHes only tool_access + local_run_enabled. That must not
    silently clear the member's project scoping (the field defaults to None on the way in)."""
    await _scope(env, [env.apollo["slug"]])
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{env.member_uid}/access", headers=_h(env.owner),
                          json={"tool_access": None, "local_run_enabled": False})
    assert r.status_code == 200, r.text
    members = (await env.c.get(f"/orgs/{env.org_id}/members", headers=_h(env.owner))).json()
    me = next(m for m in members if m["user_id"] == env.member_uid)
    assert me["project_access"] == [env.apollo["id"]], "project scope must survive an unrelated PATCH"
    assert (await env.c.get("/call/g-tool/ok", headers=_h(env.member))).status_code == 403


# ---- agents can be project-scoped at creation -------------------------------------------------
async def test_agent_created_with_project_scope(env):
    """`AgentIn.project_access` — an agent can be scoped to a project at mint time (slugs or ids),
    instead of a second PATCH from the Team roster."""
    r = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                         json={"name": "scoped", "project_access": [env.apollo["slug"]]})
    assert r.status_code == 200, r.text
    assert r.json()["project_access"] == [env.apollo["id"]]
    tok = r.json()["token"]
    names = {t["name"] for t in (await env.c.get("/tools", headers=_h(tok))).json()}
    assert names == {"a-tool", "shared"}
    assert (await env.c.get("/call/g-tool/ok", headers=_h(tok))).status_code == 403


async def test_agent_rotate_without_projects_keeps_the_scope(env):
    """A rotate that does not mention project_access must not widen it — same `model_fields_set`
    contract as tool_access (round-4 blocker #2)."""
    made = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                            json={"name": "scoped", "project_access": [env.apollo["slug"]]})
    assert made.status_code == 200, made.text
    rot = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                           json={"name": "scoped"})  # the dashboard Rotate shape
    assert rot.status_code == 200, rot.text
    assert rot.json()["project_access"] == [env.apollo["id"]], "rotate must never widen the scope"
    assert (await env.c.get("/call/g-tool/ok", headers=_h(rot.json()["token"]))).status_code == 403


async def test_agent_created_with_unknown_project_is_rejected(env):
    r = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                         json={"name": "typo", "project_access": ["no-such-project"]})
    assert r.status_code == 422
