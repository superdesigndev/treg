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
from treg.application.call import wallet as call_wallet
from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.db import session_maker
from treg.models import CallRecord, Hold, LedgerEntry

from test_agent_payments import (  # noqa: F401 - fixtures
    EP, EP_MICRO, FLOOR, _balance, _entries, _fake_relay, _mpp_challenges, _mpp_credential,
    _mpp_paid, _mpp_proof, _siwx_proof, _x402_credential, anon, chain, facilitator, payments_on,
    wallet)
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
    # The ledger keeps what the aggregator reported beside what the wallet paid.
    (settle,) = [e for e in await _entries(payer) if e.kind == "settle"]
    assert (settle.meta["observed_micro"], -settle.amount_micro) == (3 * EP_MICRO, EP_MICRO)


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
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "price_changed", r.text
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
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "price_changed", r.text
    assert chain.broadcasts == 0


def _raise_margin(monkeypatch) -> None:
    monkeypatch.setenv("TREG_PLATFORM_MARGIN", "1.0")   # the quote doubles, the floor covers it
    get_settings.cache_clear()


@pytest.mark.parametrize("proof", ["mpp", "siwx"])
async def test_credit_does_not_pay_a_price_raised_after_the_proof(
        wallet: AsyncClient, payments_on, chain, monkeypatch, proof):
    account = Account.create()
    if proof == "mpp":
        assert (await _mpp_paid(wallet, chain, account)).status_code == 200
    else:   # credit is per network: fund the x402 one
        assert (await _x402_paid(wallet, account=account))[0].status_code == 200
    payer = account.address.lower()
    credit = await _balance(payer)
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    signed = ({"Authorization": await _mpp_proof(challenge, account)} if proof == "mpp" else
              {"SIGN-IN-WITH-X": _siwx_proof(challenge, account)})
    _raise_margin(monkeypatch)
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers=signed)
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "price_changed", r.text
    assert await _balance(payer) == credit
    fresh = ({"Authorization": await _mpp_proof(r, account)} if proof == "mpp" else
             {"SIGN-IN-WITH-X": _siwx_proof(r, account)})
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers=fresh)
    assert r.status_code == 200 and r.headers["X-Treg-Cost-Micro"] == str(2 * EP_MICRO), r.text


async def test_a_pushed_transfer_signed_before_a_price_change_is_kept_as_credit(
        wallet: AsyncClient, payments_on, chain, monkeypatch):
    """A push transfer is on chain before treg sees it: it is credited on the terms it was sent
    under, and the wallet confirms the new price with its credit."""
    from mpp import Credential

    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "cd" * 32})
    _raise_margin(monkeypatch)
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "price_changed", r.text
    assert await _balance(chain.payer) == FLOOR, "the transfer that landed is the wallet's credit"
    r = await wallet.get(f"/call/{EP}?aweme_id=7",
                         headers={"Authorization": await _mpp_proof(r, account)})
    assert r.status_code == 200 and r.headers["X-Treg-Cost-Micro"] == str(2 * EP_MICRO), r.text


async def test_a_payment_is_credited_at_the_fee_it_was_offered(anon: AsyncClient, payments_on,
                                                               monkeypatch):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge)
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_FEE_BPS", "500")   # the payment floor is unchanged
    get_settings.cache_clear()
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200, r.text
    assert await _balance(payer) == FLOOR - EP_MICRO


async def test_an_x402_payment_without_a_resource_description_still_pays(anon: AsyncClient,
                                                                         payments_on):
    """x402 v2's resource description is optional: a client that echoes only the URL pays."""
    from mpp.x402 import decode_payment_signature, encode_payment_signature

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    payload = decode_payment_signature(credential)
    payload["resource"] = {"url": payload["resource"]["url"]}
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"PAYMENT-SIGNATURE": encode_payment_signature(payload)})
    assert r.status_code == 200, r.text


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


async def test_an_overflow_s_own_refusal_is_recorded_as_the_answer(clients: AsyncClient, overflow_on,
                                                                   monkeypatch):
    """Skip-direct: no direct attempt; the child's reservation is refused for the team's balance.
    The caller gets that 402, and so does its record, not the 503 the attempt began as."""
    await _route(price_micro=3 * EP_MICRO)
    await _exhausted()
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, EP, await ledger.balance_of(db, org_id) - EP_MICRO)
    monkeypatch.setattr(O, "_send", _orthogonal([], []))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and r.json()["detail"]["error"] == "insufficient_balance", r.text
    assert [rec[:2] for rec in await _records() if rec[2] != "OVERFLOW"][-1] == (402, "balance")
    async with session_maker() as db:
        assert {e.kind for e in (await db.execute(select(LedgerEntry).where(
            LedgerEntry.org_id == org_id))).scalars()} == {"grant", "reserve"}


async def test_a_wallet_s_task_settles_within_its_quote(clients: AsyncClient, monkeypatch):
    """The quote outlives the submission: the terminal settle, on a later poll, charges within
    it."""
    from test_asynctasks import _response
    monkeypatch.setenv("TREG_PLATFORM_KEY_FIRECRAWL", "PLATFORM-FIRECRAWL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "firecrawl")
    get_settings.cache_clear()
    job = "01a0e894-a4fd-77fe-8131-e3f2d5720dbe"

    async def submit(*args, **kwargs):
        return _response(200, {"success": True, "id": job,
                               "url": f"https://api.firecrawl.dev/v2/crawl/{job}"})

    async def poll(*args, **kwargs):
        return _response(200, {"success": True, "status": "completed", "total": 3,
                               "completed": 3, "creditsUsed": 4, "data": []})

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    balance = f"/orgs/{org_id}/balance"
    before = (await clients.get(balance)).json()["balance_micro"]
    monkeypatch.setattr(call_service, "relay", submit)
    quote = 15_000   # the crawl's hold
    marked = call_wallet.current.set(call_wallet.WalletCall(quote_micro=quote, paid=True))
    try:
        submitted = await clients.post("/call/firecrawl.web.crawl", json={
            "url": "https://example.com", "limit": 3, "scrapeOptions": {"parsers": []}})
    finally:
        call_wallet.current.reset(marked)
    assert submitted.status_code == 200, submitted.text
    monkeypatch.setattr(call_service, "relay", poll)
    assert (await clients.get(f"/call/firecrawl.web.crawl.status?id={job}")).status_code == 200
    assert before - (await clients.get(balance)).json()["balance_micro"] == quote
    async with session_maker() as db:
        settle = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == submitted.headers["X-Treg-Call-Id"],
            LedgerEntry.kind == "settle"))).scalar_one()
    assert settle.meta["observed_micro"] == 20_000
