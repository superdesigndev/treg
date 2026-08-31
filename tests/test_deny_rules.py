"""Deny rules — org policy over what may be called.

The third deny layer (the other two: `egress.py` = the OS firewall around an isolated local run,
`localrun.check_deny` = a local run's argv). This one sees the HTTP request the proxy is about to
relay: host + path + method.

Proves: an empty field means "any"; the path match can't be dodged by a lookalike prefix; a rule
aimed at one member leaves the rest of the team alone; it applies to the OWNER too (a guardrail, not
a permission tier); both run tiers are gated; and no rules = byte-for-byte the previous behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream

from treg import crypto
from treg.api import app
from treg.routers.orgs import _deny_match
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
        await c.post("/tools", headers=_h(owner),
                     json={"name": "alpha", "base_url": "http://upstream", "secret_id": sid})
        yield SimpleNamespace(c=c, org_id=org_id, owner=owner, owner_uid=owner_uid,
                              member=member, member_uid=member_uid)
    await app.state.http.aclose()


async def _rule(env, **body) -> dict:
    r = await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner), json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- the pure matcher (no DB, like localrun.check_deny) -------------------------------------
def _r(**kw) -> DenyRule:
    return DenyRule(host=kw.get("host", ""), path_prefix=kw.get("path_prefix", ""),
                    method=kw.get("method", ""))


def test_an_empty_field_means_any():
    assert _deny_match([_r(method="DELETE")], "any.host", "/x", "DELETE") is not None
    assert _deny_match([_r(method="DELETE")], "any.host", "/x", "GET") is None
    assert _deny_match([_r(host="api.stripe.com")], "api.stripe.com", "/anything", "GET") is not None
    assert _deny_match([_r(host="api.stripe.com")], "api.other.com", "/anything", "GET") is None


def test_host_matching_is_case_insensitive():
    assert _deny_match([_r(host="API.Stripe.com")], "api.stripe.com", "/", "GET") is not None


def test_the_path_prefix_is_anchored_not_a_raw_string_prefix():
    """`/v1/charges` must not be dodged by `/v1/chargesX`, the same trap `_resolve_call` guards."""
    rule = [_r(path_prefix="/v1/charges")]
    assert _deny_match(rule, "h", "/v1/charges", "GET") is not None
    assert _deny_match(rule, "h", "/v1/charges/evt_1", "GET") is not None
    assert _deny_match(rule, "h", "/v1/chargesX", "GET") is None


def test_all_three_fields_must_match_together():
    rule = [_r(host="h", path_prefix="/admin", method="DELETE")]
    assert _deny_match(rule, "h", "/admin/users", "DELETE") is not None
    assert _deny_match(rule, "h", "/admin/users", "GET") is None
    assert _deny_match(rule, "other", "/admin/users", "DELETE") is None


# ---- enforcement on the proxy ---------------------------------------------------------------
async def test_no_rules_changes_nothing(env):
    assert (await env.c.get("/call/alpha/ok", headers=_h(env.member))).status_code == 200


async def test_a_method_rule_blocks_that_method_only(env):
    await _rule(env, method="DELETE", note="no deletes from agents")
    blocked = await env.c.delete("/call/alpha/thing", headers=_h(env.member))
    assert blocked.status_code == 403, blocked.text
    assert "blocked by a policy rule" in blocked.text and "no deletes from agents" in blocked.text
    assert (await env.c.get("/call/alpha/thing", headers=_h(env.member))).status_code == 200


async def test_a_path_rule_blocks_that_path_only(env):
    await _rule(env, path_prefix="/admin")
    assert (await env.c.get("/call/alpha/admin/users", headers=_h(env.member))).status_code == 403
    assert (await env.c.get("/call/alpha/public", headers=_h(env.member))).status_code == 200


async def test_a_host_rule_blocks_the_whole_upstream(env):
    await _rule(env, host="upstream")
    assert (await env.c.get("/call/alpha/anything", headers=_h(env.member))).status_code == 403


async def test_a_rule_aimed_at_one_member_leaves_the_team_alone(env):
    await _rule(env, method="DELETE", user_id=env.member_uid)
    assert (await env.c.delete("/call/alpha/x", headers=_h(env.member))).status_code == 403
    assert (await env.c.delete("/call/alpha/x", headers=_h(env.owner))).status_code == 200


async def test_an_org_rule_applies_to_the_owner_too(env):
    """A deny rule is a guardrail, not a permission tier — an owner who disagrees deletes the rule."""
    await _rule(env, method="DELETE")
    assert (await env.c.delete("/call/alpha/x", headers=_h(env.owner))).status_code == 403


async def test_the_url_passthrough_shape_is_gated_the_same(env):
    """The check runs on the RESOLVED upstream, so the caller can't dodge it by using the other shape."""
    await _rule(env, path_prefix="/admin")
    r = await env.c.get("/call/http://upstream/admin/users", headers=_h(env.member))
    assert r.status_code == 403, r.text


async def test_deleting_the_rule_restores_the_call(env):
    rule = await _rule(env, method="DELETE")
    assert (await env.c.delete("/call/alpha/x", headers=_h(env.member))).status_code == 403
    assert (await env.c.delete(f"/orgs/{env.org_id}/deny/{rule['id']}",
                               headers=_h(env.owner))).status_code == 200
    assert (await env.c.delete("/call/alpha/x", headers=_h(env.member))).status_code == 200


# ---- the run tiers are gated too -------------------------------------------------------------
async def test_a_host_rule_blocks_the_server_run_tier(env):
    await env.c.post("/skills", headers=_h(env.owner), json={
        "name": "beta", "recipe": "# beta",
        "secrets": [{"local_name": "k", "kind": "env", "value": "s"}],
        "tools": [{"name": "beta", "base_url": "http://upstream",
                   "cli": {"bin": "sh", "inject": [{"secret": "k", "via": "env", "name": "K"}],
                           "enabled": True}}]})
    await _rule(env, host="upstream")
    r = await env.c.post("/run", headers=_h(env.owner), json={"tool": "beta", "args": ["-c", "true"]})
    assert r.status_code == 403 and "blocked by a policy rule" in r.text


async def test_a_host_rule_blocks_the_local_grant(env):
    await _rule(env, host="upstream")
    r = await env.c.post("/tools/alpha/grant", headers=_h(env.owner), json={"argv": ["alpha"]})
    assert r.status_code == 403 and "blocked by a policy rule" in r.text


# ---- the admin surface ------------------------------------------------------------------------
async def test_rules_are_listed_and_scoped_to_the_org(env):
    await _rule(env, method="DELETE")
    await _rule(env, host="https://api.stripe.com/v1", user_id=env.member_uid)
    rules = (await env.c.get(f"/orgs/{env.org_id}/deny", headers=_h(env.owner))).json()
    assert len(rules) == 2
    assert {r["scope"] for r in rules} == {"org", "member"}
    # a full URL is accepted and reduced to its host, since pasting a base_url is the obvious try
    assert any(r["host"] == "api.stripe.com" for r in rules), rules
    assert all(r["verdict"] == "deny" for r in rules)


async def test_bad_input_is_refused(env):
    for payload in ({}, {"note": "nothing to match on"}, {"method": "TELEPORT"}):
        r = await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner), json=payload)
        assert r.status_code == 422, f"{payload} → {r.status_code}"
    r = await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner),
                         json={"method": "GET", "user_id": 9999})
    assert r.status_code == 404, "a rule can't target someone who isn't a member"


async def test_only_admins_manage_rules(env):
    assert (await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.member),
                             json={"method": "DELETE"})).status_code == 403
    assert (await env.c.get(f"/orgs/{env.org_id}/deny", headers=_h(env.member))).status_code == 403


async def test_another_orgs_rule_is_never_reachable(env):
    rule = await _rule(env, method="DELETE")
    async with session_maker() as s:
        other = Org(name="Other", slug="other"); s.add(other); await s.commit(); await s.refresh(other)
        other_id = other.id
    tok, _ = await _mint("o@x.dev", other_id, "owner")
    r = await env.c.delete(f"/orgs/{other_id}/deny/{rule['id']}", headers=_h(tok))
    assert r.status_code == 404, "never confirm another org's ids"
