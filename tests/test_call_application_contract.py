"""The no-money gates run in a frozen order: tool access, deny rules, the member's daily cap, then
the public-demo IP cap. The order shows in which refusal a caller gets when two gates would refuse,
and in whether a refused call spends a later gate's allowance."""

from __future__ import annotations

from treg.domain.governance import publicdemo
from treg.infra.db import session_maker
from treg.models import Org
from test_access import _h, _set_access, env  # noqa: F401


# The words in each refusal a caller reads, to tell them apart.
REFUSALS = {"tool_access_denied": "don't have access to the tool", "policy_denied": "blocked by a policy rule",
            "daily_cap_reached": "daily usage limit reached", "public_demo_rate_limited": "demo limit reached"}


async def _refusal(env, path):
    response = await env.c.get(path, headers=_h(env.member))
    if response.status_code < 400:
        return response.status_code, None
    detail = response.json()["detail"]
    return response.status_code, next((code for code, words in REFUSALS.items() if words in detail), detail)


async def _deny(env, path_prefix):
    r = await env.c.post(f"/orgs/{env.org_id}/deny", headers=_h(env.owner),
                         json={"host": "upstream", "path_prefix": path_prefix})
    assert r.status_code == 200, r.text


async def _cap(env, cap):
    r = await env.c.patch(f"/orgs/{env.org_id}/members/{env.member_uid}/cap", headers=_h(env.owner),
                          json={"daily_call_cap": cap})
    assert r.status_code == 200, r.text


async def test_a_tool_outside_the_members_access_is_refused_as_access_before_deny_rules(env):
    await env.c.post("/tools", headers=_h(env.owner), json={"name": "gamma", "base_url": "http://upstream"})
    await _set_access(env, ["alpha"])
    await _deny(env, "/")
    assert await _refusal(env, "/call/gamma/x") == (403, "tool_access_denied")
    assert await _refusal(env, "/call/alpha/x") == (403, "policy_denied")


async def test_a_denied_call_leaves_the_members_daily_call_unspent(env):
    await _deny(env, "/blocked")
    await _cap(env, 1)
    assert await _refusal(env, "/call/alpha/blocked") == (403, "policy_denied")
    assert await _refusal(env, "/call/alpha/open") == (200, None)
    assert await _refusal(env, "/call/alpha/blocked") == (403, "policy_denied")
    assert await _refusal(env, "/call/alpha/open") == (429, "daily_cap_reached")


async def test_a_member_past_both_caps_on_a_public_demo_team_hears_about_their_own_cap(env, monkeypatch):
    monkeypatch.setattr(publicdemo, "PUBLIC_DEMO_RATE_MAX", 2)
    async with session_maker() as db:
        org = await db.get(Org, env.org_id)
        org.public_demo = True
        await db.commit()
    assert [await _refusal(env, "/call/alpha/x") for _ in range(3)] == [
        (200, None), (200, None), (429, "public_demo_rate_limited")]
    await _cap(env, 0)
    assert await _refusal(env, "/call/alpha/x") == (429, "daily_cap_reached")
