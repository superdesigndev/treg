"""The `call_admission` decision point on the call path.

Asked only before treg's own money pays, with the facts its context promises and no pooled
connection held; a refusal is a 403 that names the remedy and leaves no hold behind.
"""
import logging

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit, policy
from treg.domain import money as ledger
from treg.infra.db import _engine, session_maker
from treg.models import Hold, LedgerEntry

from test_marketplace_call import EP, EP_MICRO, _balance, platform_on  # noqa: F401 - fixture reuse

SIGNUP_EMAIL = "tim@superdesign.dev"  # the `clients` fixture's verified owner


@pytest.fixture
def admission(monkeypatch):
    """A registered handler that records what it was asked and answers `state["decision"]`."""
    state = {"decision": policy.CallDecision.ALLOW, "raise": None, "asked": []}

    async def handler(ctx):
        state["asked"].append((ctx, _engine.pool.checkedout()))
        if state["raise"] is not None:
            raise state["raise"]
        return state["decision"]
    registry = policy.PolicyRegistry()
    registry.on_call_admission(handler)
    monkeypatch.setattr(policy, "registry", registry)
    return state


async def _org_id(clients: AsyncClient) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def test_a_platform_call_asks_with_the_promised_facts(clients, platform_on, admission):
    org_id = await _org_id(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    [(ctx, checked_out)] = admission["asked"]
    assert checked_out == 0, "no pooled connection may wait on the extension"
    assert (ctx.org_id, ctx.caller_email, ctx.owner_email) == (org_id, SIGNUP_EMAIL, SIGNUP_EMAIL)
    assert (ctx.endpoint_id, ctx.provider, ctx.platform, ctx.funding) == (EP, "tikhub", "tiktok", "platform")
    assert ctx.estimate_micro == EP_MICRO and ctx.org_has_paid is False
    assert ctx.org_created_at is not None and ctx.user_id
    assert await _balance(clients) == 1_000_000 - EP_MICRO


async def test_a_denied_call_is_a_403_with_the_remedy_and_moves_no_money(clients, platform_on, admission):
    admission["decision"] = policy.CallDecision.DENY
    org_id = await _org_id(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 403, r.text
    d = r.json()["detail"]
    assert (d["error"], d["endpoint_id"], d["provider"], d["topup_url"]) == (
        "funds_required", EP, "tikhub", "/app#billing")
    assert "add funds" in d["message"] and "treg connections connect --provider tikhub" in d["message"]
    assert "Nothing was charged" in d["message"]
    assert await _balance(clients) == 1_000_000
    async with session_maker() as db:
        assert (await db.execute(select(Hold).where(Hold.org_id == org_id))).first() is None
        kinds = (await db.execute(select(LedgerEntry.kind).where(LedgerEntry.org_id == org_id))).scalars().all()
    assert set(kinds) == {"grant"}, "a refusal before the reserve writes no money entry"
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert (row["status_code"], row["refused_by"]) == (403, "policy")


async def test_a_team_that_bought_credit_says_so(clients, platform_on, admission):
    org_id = await _org_id(clients)
    async with session_maker() as db:
        await ledger.topup(db, org_id, 5_000_000, "pi_admission_test")
        await db.commit()
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert admission["asked"][0][0].org_has_paid is True


async def test_an_agent_identity_carries_its_owner(clients, platform_on, admission):
    org_id = await _org_id(clients)
    agent = (await clients.post(f"/orgs/{org_id}/agents", json={"name": "Worker"})).json()
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Token": agent["token"]})
    assert r.status_code == 200, r.text
    ctx = admission["asked"][0][0]
    assert ctx.caller_email.endswith("@agents.treg.local") and ctx.owner_email == SIGNUP_EMAIL


async def test_a_call_on_the_teams_own_key_never_asks(clients, platform_on, admission):
    admission["decision"] = policy.CallDecision.DENY
    await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert admission["asked"] == []


async def test_a_failing_extension_lets_the_call_through(clients, platform_on, admission, caplog):
    admission["raise"] = RuntimeError("private-rule-name")
    with caplog.at_level(logging.WARNING, logger="treg"):
        r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert "event=policy_failed point=call_admission error=RuntimeError outcome=allow" in caplog.text
    assert "private-rule-name" not in caplog.text + r.text
    assert await _balance(clients) == 1_000_000 - EP_MICRO


async def test_without_a_handler_nothing_is_asked(clients, platform_on, monkeypatch):
    registry = policy.PolicyRegistry()
    monkeypatch.setattr(policy, "registry", registry)
    assert registry.asks_call_admission is False
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200


async def test_a_denied_call_does_not_hold_its_idempotency_key(clients, platform_on, admission):
    """After the team adds funds, the same key runs the call instead of replaying the refusal."""
    admission["decision"] = policy.CallDecision.DENY
    headers = {"Idempotency-Key": "admission-retry-1"}
    assert (await clients.get(f"/call/{EP}?aweme_id=7", headers=headers)).status_code == 403
    admission["decision"] = policy.CallDecision.ALLOW
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["X-Treg-Cost-Micro"] == str(EP_MICRO)
