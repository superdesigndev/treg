"""Project-scoped deny rules + the read-only per-tool CLI deny listing.

A DenyRule may now name a project: it fires only on calls made THROUGH that project's tools —
the third optional scope axis (host/path/method, member, project), each NULL-means-any.

Proves: a project rule blocks that project's tools and leaves org-wide + other-project tools
alone; a NULL-project rule keeps its old meaning; deleting the project sweeps its rules (a
dangling FK would break Postgres); and /policy/cli-deny reports each CLI tool's effective argv
deny patterns with their source, read-only.
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
from treg.models import DenyRule, Membership, Org, User


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
        owner, owner_uid = await _mint("owner@x.dev", org_id, "owner")
        member, member_uid = await _mint("m@x.dev", org_id, "member")
        sid = (await c.post("/secrets", headers=_h(owner), json={"name": "k", "value": "v"})).json()["id"]
        apollo = (await c.post(f"/orgs/{org_id}/projects", headers=_h(owner),
                               json={"name": "Apollo"})).json()
        for name, proj in (("a-tool", apollo["slug"]), ("shared", None)):
            await c.post("/tools", headers=_h(owner), json={
                "name": name, "base_url": "http://upstream", "secret_id": sid, "project": proj})
        yield SimpleNamespace(c=c, org_id=org_id, owner=owner, owner_uid=owner_uid,
                              member=member, member_uid=member_uid, apollo=apollo, sid=sid)
    await app.state.http.aclose()


async def _rule(env, **body) -> dict:
    r = await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner), json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- the project axis --------------------------------------------------------------------------
async def test_project_rule_blocks_only_that_projects_tools(env):
    await _rule(env, host="upstream", project_id=env.apollo["id"], note="apollo frozen")
    blocked = await env.c.get("/call/a-tool/ok", headers=_h(env.member))
    assert blocked.status_code == 403 and "apollo frozen" in blocked.text
    assert "project" in blocked.text, "the refusal names its project scope"
    # the SAME host through an org-wide tool stays open — the rule is about the project, not the host
    assert (await env.c.get("/call/shared/ok", headers=_h(env.member))).status_code == 200


async def test_null_project_rule_keeps_blocking_everything(env):
    """Regression: existing rules (project_id NULL) must keep their exact old meaning."""
    await _rule(env, host="upstream")
    for name in ("a-tool", "shared"):
        assert (await env.c.get(f"/call/{name}/ok", headers=_h(env.member))).status_code == 403


async def test_project_rule_composes_with_member_scope(env):
    """user_id AND project_id: the rule fires only for that member on that project's tools."""
    await _rule(env, host="upstream", project_id=env.apollo["id"], user_id=env.member_uid)
    assert (await env.c.get("/call/a-tool/ok", headers=_h(env.member))).status_code == 403
    assert (await env.c.get("/call/a-tool/ok", headers=_h(env.owner))).status_code == 200
    assert (await env.c.get("/call/shared/ok", headers=_h(env.member))).status_code == 200


async def test_project_rule_applies_to_the_owner_too(env):
    """Still a guardrail, not a permission tier."""
    await _rule(env, host="upstream", project_id=env.apollo["id"])
    assert (await env.c.get("/call/a-tool/ok", headers=_h(env.owner))).status_code == 403


async def test_unknown_or_foreign_project_is_rejected(env):
    r = await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner),
                         json={"host": "upstream", "project_id": 9999})
    assert r.status_code == 404


async def test_deleting_the_project_sweeps_its_rules(env):
    """DenyRule.project_id is a foreign key — a surviving rule would dangle (Postgres rejects it,
    SQLite hides it), and it could never fire again anyway."""
    await _rule(env, host="upstream", project_id=env.apollo["id"])
    await _rule(env, host="upstream", path_prefix="/keep")  # org-wide: must survive
    r = await env.c.delete(f"/orgs/{env.org_id}/projects/{env.apollo['id']}", headers=_h(env.owner))
    assert r.status_code == 200, r.text
    rules = (await env.c.get(f"/orgs/{env.org_id}/deny", headers=_h(env.owner))).json()
    assert [x["path_prefix"] for x in rules] == ["/keep"], "only the project's rule is swept"
    async with session_maker() as s:
        assert not (await s.execute(select(DenyRule).where(
            DenyRule.project_id.is_not(None)))).scalars().all()


# ---- the read-only CLI deny listing ------------------------------------------------------------
async def test_cli_deny_reports_patterns_with_their_source(env):
    made = await env.c.post("/tools", headers=_h(env.owner), json={
        "name": "gh-cli", "base_url": "http://upstream", "secret_id": env.sid, "kind": "cli",
        "cli": {"enabled": True, "bin": "gh", "deny": ["^auth\\b"], "deny_defaults": False,
                "inject": [{"secret": "k", "via": "env", "name": "GH_TOKEN"}]}})
    assert made.status_code == 200, made.text
    rows = (await env.c.get(f"/orgs/{env.org_id}/policy/cli-deny", headers=_h(env.owner))).json()
    mine = next((r for r in rows if r["tool"] == "gh-cli"), None)
    assert mine is not None and mine["enabled"] is True
    assert {"pattern": "^auth\\b", "source": "skill"} in mine["patterns"]
    # plain HTTP tools with no cli profile stay out of the listing
    assert not any(r["tool"] == "shared" for r in rows)


async def test_cli_deny_is_admin_only(env):
    r = await env.c.get(f"/orgs/{env.org_id}/policy/cli-deny", headers=_h(env.member))
    assert r.status_code == 403
