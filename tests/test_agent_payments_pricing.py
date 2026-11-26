"""A keyless wallet is never charged more than the 402 told it: the quote is the hold, overflow and
settlement stay within it, a signed payment binds the price it was signed at, and every refusal is
recorded as the answer the wallet got. docs/context/architecture/agent-payments.md ("Prices")."""

from __future__ import annotations

import pytest
from eth_account import Account
from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.application import agent_payments
from treg.application.call import overflow as O
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.db import session_maker
from treg.models import CallRecord, Hold

from test_agent_payments import (  # noqa: F401 - fixtures
    EP, EP_MICRO, FLOOR, _balance, _entries, _fake_relay, _mpp_credential, _mpp_paid, _mpp_proof,
    _x402_credential, anon, chain, facilitator, payments_on, wallet)
from test_capacity_overflow import VENDOR_BODY, _exhausted, _orthogonal, _route

VERIFY = "millionphones.people.email.verify"   # 0.1 credit per verdict, worst case 1 credit


@pytest.fixture
def overflow_on(monkeypatch, payments_on):
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "on")
    monkeypatch.setenv("TREG_OVERFLOW_KEY_ORTHOGONAL", "ORTH-KEY")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _records() -> list[tuple[int, str | None, str]]:
    await audit.drain()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).order_by(CallRecord.id))).scalars()
        return [(r.status_code, r.refused_by, r.method) for r in rows]


async def _x402_paid(anon: AsyncClient, query: str = "aweme_id=7", account=None):
    challenge = await anon.get(f"/call/{EP}?{query}")
    credential, payer = _x402_credential(challenge, account)
    return await anon.get(f"/call/{EP}?{query}", headers={"PAYMENT-SIGNATURE": credential}), payer


# ---- the quote is the hold ----------------------------------------------------------------------
async def test_the_quote_is_what_the_call_holds(anon: AsyncClient, payments_on, monkeypatch):
    """A row whose settle reads the provider's figure holds its declared worst case; the 402 quotes
    that same hold, so credit that covers the quote covers the reservation."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_MILLIONPHONES", "PLATFORM-MP-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub,bounceban,millionphones")
    get_settings.cache_clear()
    monkeypatch.setattr(call_service, "relay", _fake_relay(
        200, b'{"result":"valid","credits_charged":0.1}',
        headers=((b"content-type", b"application/json"),)))
    query = "email=jane.doe@example.com"
    challenge = await anon.get(f"/call/{VERIFY}?{query}")
    assert challenge.status_code == 402, challenge.text
    quoted = challenge.json()["detail"]["estimated_cost_micro"]
    credential, payer = _x402_credential(challenge)
    r = await anon.get(f"/call/{VERIFY}?{query}", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200, r.text
    (reserve,) = [e for e in await _entries(payer) if e.kind == "reserve"]
    assert -reserve.amount_micro == quoted


# ---- overflow stays within the quote ------------------------------------------------------------
@pytest.mark.parametrize("skip_direct", [False, True], ids=["after-direct", "skip-direct"])
async def test_an_overflow_priced_above_the_quote_does_not_serve_a_wallet(
        anon: AsyncClient, overflow_on, monkeypatch, skip_direct):
    await _route(price_micro=3 * EP_MICRO)
    if skip_direct:
        await _exhausted()
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen: list = []
    monkeypatch.setattr(O, "_send", _orthogonal(
        [(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], seen))
    r, payer = await _x402_paid(anon)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == "provider_capacity_unavailable"
    assert seen == [], "the aggregator was asked for a call the wallet was not quoted"
    assert r.headers["X-Treg-Cost-Micro"] == "0"
    assert await _balance(payer) == FLOOR


async def test_a_wallet_s_overflow_settles_at_most_its_quote(anon: AsyncClient, overflow_on,
                                                             monkeypatch):
    """The aggregator's route fits the quote but it reports a higher cost: the wallet pays its
    quote, never more."""
    await _route(price_micro=EP_MICRO // 2)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    monkeypatch.setattr(O, "_send", _orthogonal(
        [(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], []))
    r, payer = await _x402_paid(anon)
    assert r.status_code == 200 and r.headers["X-Treg-Served-Via"] == "overflow:orthogonal", r.text
    assert r.headers["X-Treg-Cost-Micro"] == str(EP_MICRO)
    assert await _balance(payer) == FLOOR - EP_MICRO
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).first() is None


async def test_a_wallet_on_credit_is_not_asked_to_pay_again_for_an_overflow(
        wallet: AsyncClient, overflow_on, chain, monkeypatch):
    """A 402 makes a payment client pay again, and the next admission draws the same credit at
    the direct price: the answer to an overflow priced above the quote is never a 402."""
    account = Account.create()
    paid = await _mpp_paid(wallet, chain, account)
    assert paid.status_code == 200, paid.text
    await _route(price_micro=3 * EP_MICRO)
    await _exhausted()
    monkeypatch.setattr(O, "_send", _orthogonal(
        [(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], []))
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    r = await wallet.get(f"/call/{EP}?aweme_id=7",
                         headers={"Authorization": await _mpp_proof(challenge, account)})
    assert r.status_code == 503, r.text
    assert r.headers["X-Treg-Payment"] == "credit"


# ---- a signed payment binds its price -----------------------------------------------------------
async def test_an_x402_payment_signed_before_a_price_change_is_refused(
        anon: AsyncClient, payments_on, facilitator, monkeypatch):
    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    monkeypatch.setenv("TREG_PLATFORM_MARGIN", "1.0")   # the payment floor still covers it
    get_settings.cache_clear()
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "invalid_challenge", r.text
    assert r.json()["detail"]["estimated_cost_micro"] == 2 * EP_MICRO
    assert facilitator.settles == 0 and calls == []
    credential, _ = _x402_credential(r)   # the fresh challenge names the new price
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200 and r.headers["X-Treg-Cost-Micro"] == str(2 * EP_MICRO), r.text


async def test_an_mpp_payment_signed_before_a_price_change_is_refused(
        anon: AsyncClient, payments_on, chain, monkeypatch):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    monkeypatch.setenv("TREG_PLATFORM_MARGIN", "1.0")
    get_settings.cache_clear()
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"Authorization": _mpp_credential(challenge)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "invalid_challenge", r.text
    assert chain.broadcasts == 0


# ---- the record is the answer -------------------------------------------------------------------
async def test_a_refused_settlement_is_recorded_as_a_payment_step(anon: AsyncClient, payments_on,
                                                                   facilitator):
    facilitator.fail = True
    r, _ = await _x402_paid(anon)
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "settlement_failed"
    assert (await _records())[-1][:2] == (402, "payment")


async def test_a_paid_call_refused_for_balance_is_recorded_as_its_503(anon: AsyncClient,
                                                                      payments_on, monkeypatch):
    opened = agent_payments.open_call

    async def _drained(**kwargs):
        paid = await opened(**kwargs)
        async with session_maker() as db:  # a concurrent call spent the credit meanwhile
            org_id = int(paid.caller.org.id)
            await ledger.reserve(db, org_id, EP, await ledger.balance_of(db, org_id))
        return paid

    monkeypatch.setattr(agent_payments, "open_call", _drained)
    r, _ = await _x402_paid(anon)
    assert r.status_code == 503 and r.json()["detail"]["error"] == "insufficient_balance"
    assert (await _records())[-1][:2] == (503, "balance")


async def test_a_credit_draw_refused_for_balance_is_recorded_as_a_payment_step(
        wallet: AsyncClient, payments_on, chain, monkeypatch):
    account = Account.create()
    assert (await _mpp_paid(wallet, chain, account)).status_code == 200
    opened = agent_payments.open_call

    async def _drained(**kwargs):
        paid = await opened(**kwargs)
        async with session_maker() as db:
            org_id = int(paid.caller.org.id)
            await ledger.reserve(db, org_id, EP, await ledger.balance_of(db, org_id))
        return paid

    monkeypatch.setattr(agent_payments, "open_call", _drained)
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    r = await wallet.get(f"/call/{EP}?aweme_id=7",
                         headers={"Authorization": await _mpp_proof(challenge, account)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "credit_insufficient", r.text
    assert (await _records())[-1][:2] == (402, "payment")
