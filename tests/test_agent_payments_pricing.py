"""A keyless wallet is never charged more than the 402 told it: the quote is the hold, overflow and
settlement stay within it, a signed payment binds the price it was signed at, and every refusal is
recorded as the answer the wallet got. docs/context/architecture/agent-payments.md ("Prices")."""

from __future__ import annotations

import json
import time
from datetime import UTC

import httpx

import pytest
from eth_account import Account
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg import audit
from treg.api import app
from treg.application import agent_payments
from treg.application.call import overflow as O
from treg.application.call import service as call_service
from treg.application.call import wallet as call_wallet
from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.agent_payments.tempo import MppTempoRail
from treg.infra.db import session_maker
from treg.timeutil import utcnow_naive
from treg.models import AgentPayer, AgentPayment, CallRecord, Ephemeral, Hold, LedgerEntry

from test_agent_payments import (  # noqa: F401 - fixtures
    EP, EP_MICRO, FLOOR, RECIPIENT, _balance, _payer_org, _entries, _fake_relay, _mpp_challenges, _mpp_credential,
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


@pytest.fixture
def terms_on(monkeypatch, payments_on):
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TERMS_BINDING", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _terms_switch(monkeypatch, on: bool) -> None:
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TERMS_BINDING", "1" if on else "0")
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
        anon: AsyncClient, payments_on, facilitator, monkeypatch, terms_on):
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
        anon: AsyncClient, payments_on, chain, monkeypatch, terms_on):
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
        wallet: AsyncClient, payments_on, chain, monkeypatch, proof, terms_on):
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
        wallet: AsyncClient, payments_on, chain, monkeypatch, terms_on):
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
    # The answer names the top-up, so the wallet can match it.
    assert (r.headers["X-Treg-Payment"], r.headers["X-Treg-Paid-Micro"]) == ("paid", str(FLOOR))
    assert r.headers["X-Treg-Payment-Reference"] == r.json()["detail"]["payment_reference"] != ""
    assert r.json()["detail"]["paid_micro"] == FLOOR
    # Only the paying rail's zero-amount proof: no challenge here can pay again.
    assert {c.request["amount"] for c in _mpp_challenges(r)} == {"0"}
    assert "payment-required" not in r.headers
    r = await wallet.get(f"/call/{EP}?aweme_id=7",
                         headers={"Authorization": await _mpp_proof(r, account)})
    assert r.status_code == 200 and r.headers["X-Treg-Cost-Micro"] == str(2 * EP_MICRO), r.text


async def test_a_kept_transfer_on_a_host_without_proofs_is_not_asked_to_pay_again(
        anon: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """This host is offered no zero-amount proof, so a 402 could only be answered by paying
    again: the kept transfer is answered 409, naming the credit and where to spend it."""
    from mpp import Credential

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d0" * 32})
    _raise_margin(monkeypatch)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "price_changed", r.text
    assert r.json()["detail"]["credit_micro"] == FLOOR
    assert r.headers["X-Treg-Paid-Micro"] == str(FLOOR) and r.headers["X-Treg-Payment-Reference"]
    assert "www-authenticate" not in r.headers and "payment-required" not in r.headers
    assert await _balance(chain.payer) == FLOOR


async def test_a_kept_tempo_transfer_names_the_url_where_its_proof_is_offered(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """An x402 proof cannot spend Tempo credit. Where the MPP realm is another host, the kept
    transfer is answered 409 with the full URL (scheme and port kept) where the Tempo proof is
    offered, and following it spends the credit."""
    from mpp import Credential

    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.test:8443")
    get_settings.cache_clear()
    x402 = next(rail for rail in agent_payments._rails if rail.protocol == "x402")
    agent_payments.configure([
        MppTempoRail(secret="s" * 40, realm="pay.registry.test", recipient=RECIPIENT,
                     chain_id=42431), x402])
    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d1" * 32})
    _raise_margin(monkeypatch)
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "price_changed", r.text
    there = r.json()["detail"]["proof_url"]
    assert there == f"https://pay.registry.test:8443/call/{EP}?aweme_id=7"
    async with AsyncClient(transport=ASGITransport(app=app)) as client:
        asked = await client.get(there)
        proofs = [c for c in _mpp_challenges(asked) if c.request["amount"] == "0"]
        assert proofs and {c.realm for c in proofs} == {"pay.registry.test"}
        paid = await client.get(there, headers={"Authorization": await _mpp_proof(asked, account)})
    assert paid.status_code == 200 and paid.headers["X-Treg-Payment"] == "credit", paid.text


async def test_a_challenge_used_before_a_price_rise_runs_one_call(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """The second transfer pushed under one challenge runs no call even after the price rose."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    first = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d4" * 32})
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": first.to_authorization()})).status_code == 200
    _raise_margin(monkeypatch)
    second = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d5" * 32})
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": second.to_authorization()})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "challenge_used", r.text
    assert len(calls) == 1 and await _balance(chain.payer) == 2 * FLOOR - EP_MICRO


async def test_a_kept_transfer_sent_again_gets_its_answer_again(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """The 409 was lost and the wallet resends the same transfer: it ran no call and gets the
    same final answer, never a challenge to pay anew."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    first = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d7" * 32})
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": first.to_authorization()})).status_code == 200
    second = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d8" * 32})
    answers = [await wallet.get(f"/call/{EP}?aweme_id=7",
                                headers={"Authorization": second.to_authorization()})
               for _ in range(2)]
    assert [a.status_code for a in answers] == [409, 409], answers[1].text
    assert answers[1].json()["detail"]["reason"] == "challenge_used"
    assert answers[1].headers["X-Treg-Payment-Reference"] == answers[0].headers["X-Treg-Payment-Reference"]
    assert "www-authenticate" not in answers[1].headers
    assert len(calls) == 1 and await _balance(chain.payer) == 2 * FLOOR - EP_MICRO


@pytest.mark.parametrize("public_url, realm, expected", [
    ("https://registry.test:8443/tools", "pay.registry.test",
     f"https://pay.registry.test:8443/tools/call/{EP}?aweme_id=7"),
    ("https://registry.test:8443", "::1", f"https://[::1]:8443/call/{EP}?aweme_id=7"),
    ("https://registry.test:8443", "[::1]", f"https://[::1]:8443/call/{EP}?aweme_id=7"),
])
async def test_the_proof_url_keeps_the_prefix_and_brackets_an_ipv6_host(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch, public_url, realm, expected):
    from mpp import Credential

    monkeypatch.setenv("TREG_PUBLIC_URL", public_url)
    get_settings.cache_clear()
    x402 = next(rail for rail in agent_payments._rails if rail.protocol == "x402")
    agent_payments.configure([
        MppTempoRail(secret="s" * 40, realm=realm, recipient=RECIPIENT, chain_id=42431), x402])
    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d9" * 32})
    _raise_margin(monkeypatch)
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 409 and r.json()["detail"]["proof_url"] == expected, r.text
    # The proxy in front of treg strips the prefix; the host is what the proof is offered on.
    there = expected.replace("/tools", "")
    async with AsyncClient(transport=ASGITransport(app=app)) as client:
        asked = await client.get(there)
        proofs = [c for c in _mpp_challenges(asked) if c.request["amount"] == "0"]
        # The realm a wallet compares with the host it called: never bracketed.
        assert proofs and {c.realm for c in proofs} == {realm.strip("[]")}, asked.text
        paid = await client.get(there, headers={"Authorization": await _mpp_proof(asked, account)})
    assert paid.status_code == 200 and paid.headers["X-Treg-Payment"] == "credit", paid.text


from mpp.methods.tempo.intents import ChargeIntent  # noqa: E402

# pympp's own checks, captured before the `chain` fixture replaces them.
_SDK_CHARGE = (ChargeIntent.validate, ChargeIntent.broadcast)


@pytest.fixture
def sdk_tempo(monkeypatch, payments_on):
    """The MPP rail as production runs it, pympp's validation, settlement and treg's replay store
    included; only the Tempo node is faked. `receipts` maps a transaction hash to its receipt."""
    monkeypatch.setattr(ChargeIntent, "validate", _SDK_CHARGE[0])
    monkeypatch.setattr(ChargeIntent, "broadcast", _SDK_CHARGE[1])
    receipts: dict[str, dict] = {}

    def node(request: httpx.Request) -> httpx.Response:
        call = json.loads(request.content)
        result = {"eth_chainId": hex(42431),
                  "eth_getTransactionReceipt": receipts.get((call["params"] or [""])[0])}.get(
                      call["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"], "result": result})

    rail = MppTempoRail(secret="s" * 40, realm="registry.test", recipient=RECIPIENT,
                        chain_id=42431, store=agent_payments.ReplayStore())
    rail._chain._client._transport = httpx.MockTransport(node)
    x402 = next(r for r in agent_payments._rails if r.protocol == "x402")
    agent_payments.configure([rail, x402])
    return receipts


def _landed(receipts: dict, challenge: httpx.Response, payer: str, tx: str) -> str:
    """A transfer of the challenge's amount, bound to it by its memo, as the node reports it;
    returns the push credential."""
    from mpp import Credential
    from mpp.methods.tempo._attribution import encode
    from mpp.methods.tempo.intents import TRANSFER_WITH_MEMO_TOPIC

    offered = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0")
    word = lambda address: "0x" + address.lower().removeprefix("0x").rjust(64, "0")  # noqa: E731
    receipts[tx] = {"status": "0x1", "transactionHash": tx, "from": payer, "logs": [{
        "address": offered.request["currency"],
        "topics": [TRANSFER_WITH_MEMO_TOPIC, word(payer), word(RECIPIENT),
                   encode(offered.id, offered.realm)],
        "data": "0x" + format(int(offered.request["amount"]), "064x")}]}
    return Credential(challenge=offered.to_echo(),
                      payload={"type": "hash", "hash": tx}).to_authorization()


async def test_a_kept_transfer_resent_through_the_sdk_gets_its_answer_again(
        wallet: AsyncClient, sdk_tempo, terms_on, monkeypatch):
    """pympp refuses a transaction hash it has seen, so the resend is answered before it is
    verified: the same final 409, never a challenge to pay."""
    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    payer = Account.create().address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    first = _landed(sdk_tempo, challenge, payer, "0x" + "e1" * 32)
    second = _landed(sdk_tempo, challenge, payer, "0x" + "e2" * 32)
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": first})).status_code == 200
    answers = [await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": second})
               for _ in range(2)]
    assert [a.status_code for a in answers] == [409, 409], answers[-1].text
    assert answers[1].json()["detail"]["reason"] == "challenge_used"
    assert answers[1].headers["X-Treg-Payment-Reference"] == answers[0].headers["X-Treg-Payment-Reference"]
    assert "www-authenticate" not in answers[1].headers
    assert len(calls) == 1 and await _balance(payer) == 2 * FLOOR - EP_MICRO


async def test_a_kept_transfer_settled_by_the_reconciler_keeps_its_answer(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """A kept transfer whose settlement went unanswered is credited later by the reconciler; a
    resend still gets the kept answer, recorded with the payment when it was claimed."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    first = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "e3" * 32})
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": first.to_authorization()})).status_code == 200
    second = Credential(challenge=echo,
                        payload={"type": "hash", "hash": "0x" + "e4" * 32}).to_authorization()
    chain.unknown = True
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": second})
    assert r.status_code == 503, r.text
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": second})).status_code == 503
    async with session_maker() as db:
        pending = (await db.execute(select(AgentPayment).where(
            AgentPayment.status == "settling"))).scalar_one()
    await agent_payments._credit(pending.id, "0x" + "ab" * 32)   # what the reconciler books
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": second})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "challenge_used", r.text
    assert r.headers["X-Treg-Payment-Reference"] == "0x" + "ab" * 32
    assert len(calls) == 1


async def test_a_concurrent_resend_refused_by_the_sdk_gets_the_kept_answer(
        wallet: AsyncClient, sdk_tempo, terms_on, monkeypatch):
    """Two sends of the same kept credential race: the one whose first look found nothing is
    refused by pympp as a seen hash, looks again and gets the kept answer, never a challenge."""
    payer = Account.create().address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    first = _landed(sdk_tempo, challenge, payer, "0x" + "f1" * 32)
    second = _landed(sdk_tempo, challenge, payer, "0x" + "f2" * 32)
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": first})).status_code == 200
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": second})).status_code == 409
    looked, real = [], agent_payments._resent_kept

    async def racing(*args):
        looked.append(1)
        return None if len(looked) == 1 else await real(*args)   # the race's first look

    monkeypatch.setattr(agent_payments, "_resent_kept", racing)
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": second})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "challenge_used", r.text
    assert len(looked) == 2


async def test_a_kept_receipt_answers_only_its_own_request_and_payment(
        wallet: AsyncClient, sdk_tempo, terms_on):
    payer = Account.create().address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    first = _landed(sdk_tempo, challenge, payer, "0x" + "f3" * 32)
    second = _landed(sdk_tempo, challenge, payer, "0x" + "f4" * 32)
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": first})).status_code == 200
    kept = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": second})
    assert kept.status_code == 409
    # Another request with the same credential is not that receipt's.
    other = await wallet.get(f"/call/{EP}?aweme_id=8", headers={"Authorization": second})
    assert other.status_code == 402 and "X-Treg-Payment-Reference" not in other.headers
    # A payment row that is gone (its team deleted) answers nothing, whatever reuses its id.
    async with session_maker() as db:
        row = (await db.execute(select(AgentPayment).where(
            AgentPayment.proof_id == f"mpp:tempo:42431:tx:{'0x' + 'f4' * 32}"))).scalar_one()
        stale_id = row.id
        await db.delete(row)
        await db.commit()
        db.add(AgentPayment(id=stale_id, proof_id="another-wallet", protocol="mpp",
                            network="tempo:42431", payer_address="0x" + "99" * 20, org_id=1,
                            amount_micro=FLOOR, fee_micro=0, asset="", pay_to="",
                            expires_at=utcnow_naive(), status="settled", reference="0xother"))
        await db.commit()
    paid = await agent_payments.quote(rest=EP, method="GET", path=f"/call/{EP}",
                                      query="aweme_id=7", query_items=(("aweme_id", "7"),),
                                      body=b"", host="registry.test")
    assert await agent_payments._resent_kept(paid, agent_payments._rails[0], second) is None


async def test_a_kept_transfer_s_recovery_runs_one_call_however_many_proofs(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """The kept 402 was lost and the transfer sent again: each answer offers a proof, and the
    wallet signs both. One runs the call; the other is told it ran none."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo,
                        payload={"type": "hash", "hash": "0x" + "f5" * 32}).to_authorization()
    _raise_margin(monkeypatch)
    answers = [await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
               for _ in range(2)]
    assert [a.status_code for a in answers] == [402, 402], answers[1].text
    proofs = [await _mpp_proof(a, account) for a in answers]
    runs = [await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": p})
            for p in proofs]
    assert [r.status_code for r in runs] == [200, 409], runs[1].text
    assert runs[1].json()["detail"]["reason"] == "challenge_used"
    assert len(calls) == 1


async def test_a_recovery_proof_repriced_again_keeps_its_recovery(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """The price rose again between the kept 402 and its proof: the new challenge offers only a
    proof on the same recovery terms, so the call it runs claims the recovery, and the transfer
    sent again after that answer was lost runs no second call."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo,
                        payload={"type": "hash", "hash": "0x" + "fa" * 32}).to_authorization()
    _raise_margin(monkeypatch)
    kept = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
    assert kept.status_code == 402, kept.text
    proof = await _mpp_proof(kept, account)
    monkeypatch.setenv("TREG_PLATFORM_MARGIN", "2.0")
    get_settings.cache_clear()
    repriced = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": proof})
    assert repriced.status_code == 402, repriced.text
    assert repriced.json()["detail"]["reason"] == "price_changed"
    assert {c.request["amount"] for c in _mpp_challenges(repriced)} == {"0"}
    assert "payment-required" not in repriced.headers
    run = await wallet.get(f"/call/{EP}?aweme_id=7",
                           headers={"Authorization": await _mpp_proof(repriced, account)})
    assert run.status_code == 200, run.text
    resent = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
    assert resent.status_code == 402, resent.text
    again = await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": await _mpp_proof(resent, account)})
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "challenge_used"
    assert len(calls) == 1


async def test_a_recovery_short_of_credit_pays_as_that_recovery(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """A recovery proof whose credit no longer covers the call is asked to pay on the same
    recovery terms: the payment that runs the call claims the recovery, so the kept transfer sent
    again after that answer was lost runs no second call."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo,
                        payload={"type": "hash", "hash": "0x" + "fb" * 32}).to_authorization()
    _raise_margin(monkeypatch)
    kept = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
    assert kept.status_code == 402, kept.text
    payer_org = await _payer_org(chain.payer)
    async with session_maker() as db:
        await ledger.reserve(db, payer_org, EP, await ledger.balance_of(db, payer_org) // 2)
    short = await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": await _mpp_proof(kept, account)})
    assert short.status_code == 402, short.text
    assert short.json()["detail"]["reason"] == "credit_insufficient"
    pay = next(c for c in _mpp_challenges(short) if c.request["amount"] != "0")
    paid = Credential(challenge=pay.to_echo(),
                      payload={"type": "hash", "hash": "0x" + "fc" * 32}).to_authorization()
    run = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": paid})
    assert run.status_code == 200, run.text
    # Another payment signed under the same funding challenge, not yet sent: final, no challenge.
    unsent = Credential(challenge=pay.to_echo(), payload={
        "type": "transaction", "signature": "0x76" + "22" * 40}).to_authorization()
    second = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": unsent})
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["reason"] == "challenge_used"
    assert "www-authenticate" not in second.headers
    resent = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
    assert resent.status_code == 402, resent.text
    again = await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": await _mpp_proof(resent, account)})
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "challenge_used"
    assert len(calls) == 1


async def test_a_refused_recovery_proof_keeps_its_recovery(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """A recovery proof its rail refuses (its challenge expired) still names its recovery: before
    the recovery ran it is offered that recovery's proof again, after it the final 409, never
    ordinary terms that would run the call a second time."""
    from mpp import Credential
    from treg.infra.agent_payments import PaymentRejected

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo,
                        payload={"type": "hash", "hash": "0x" + "fd" * 32}).to_authorization()
    _raise_margin(monkeypatch)
    kept = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
    assert kept.status_code == 402, kept.text
    proof = await _mpp_proof(kept, account)
    rail = next(r for r in agent_payments._rails if r.protocol == "mpp")
    verify = rail.verify

    async def expired(*args, **kwargs):
        raise PaymentRejected("payment_expired", "the challenge expired")

    monkeypatch.setattr(rail, "verify", expired)
    offered = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": proof})
    assert offered.status_code == 402, offered.text
    assert {c.request["amount"] for c in _mpp_challenges(offered)} == {"0"}
    assert "payment-required" not in offered.headers
    monkeypatch.setattr(rail, "verify", verify)
    run = await wallet.get(f"/call/{EP}?aweme_id=7",
                           headers={"Authorization": await _mpp_proof(offered, account)})
    assert run.status_code == 200, run.text
    monkeypatch.setattr(rail, "verify", expired)
    retried = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": proof})
    assert retried.status_code == 409, retried.text
    assert retried.json()["detail"]["reason"] == "challenge_used"
    assert "www-authenticate" not in retried.headers
    assert len(calls) == 1


@pytest.mark.parametrize("switch", [False, True], ids=["switch-off", "switch-on"])
async def test_a_challenge_issued_with_the_configured_realm_still_pays(
        wallet: AsyncClient, payments_on, chain, monkeypatch, switch):
    """Through a rolling deploy, an instance verifies a challenge an older one issued with the
    realm exactly as configured, so its pushed transfer is kept. It issues that realm itself until
    the switch is on (an older instance verifies only it), the normalized one after."""
    from mpp import Challenge, Credential

    _terms_switch(monkeypatch, switch)
    x402 = next(r for r in agent_payments._rails if r.protocol == "x402")
    rail = MppTempoRail(secret="s" * 40, realm="REGISTRY.TEST", recipient=RECIPIENT,
                        chain_id=42431)
    agent_payments.configure([rail, x402])
    paid = await agent_payments.quote(rest=EP, method="GET", path=f"/call/{EP}",
                                      query="aweme_id=7", query_items=(("aweme_id", "7"),),
                                      body=b"", host="registry.test")
    old = await rail._legacy["tempo/charge"](amount="0.050000", description="old",
                                             expires_in=rail._challenge_ttl)(rail._request(paid.offer))
    offered = Challenge.from_www_authenticate(old.challenge.headers.get_list("www-authenticate")[0])
    assert offered.realm == "REGISTRY.TEST"
    pushed = Credential(challenge=offered.to_echo(), payload={"type": "hash", "hash": "0x" + "f6" * 32})
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 200, r.text
    fresh = await wallet.get(f"/call/{EP}?aweme_id=7")
    assert {c.realm for c in _mpp_challenges(fresh)} == {"registry.test" if switch else "REGISTRY.TEST"}


async def test_a_spent_recovery_is_never_asked_to_pay(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """Once a kept transfer's recovery ran its call, any proof under its recovery terms, the same
    one again or another, answers the final 409 before price, credit or replay could ask for a
    payment, even when the credit left no longer covers the call."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo,
                        payload={"type": "hash", "hash": "0x" + "f7" * 32}).to_authorization()
    _raise_margin(monkeypatch)
    answers = [await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
               for _ in range(2)]
    proofs = [await _mpp_proof(a, account) for a in answers]
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"Authorization": proofs[0]})).status_code == 200
    payer_org = await _payer_org(chain.payer)
    async with session_maker() as db:
        # The margin doubles what a reserve holds: this leaves the credit well short of the call.
        await ledger.reserve(db, payer_org, EP, await ledger.balance_of(db, payer_org) // 2)
    for proof in (proofs[0], proofs[1]):
        r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": proof})
        assert r.status_code == 409 and r.json()["detail"]["reason"] == "challenge_used", r.text
        assert "www-authenticate" not in r.headers
    assert len(calls) == 1


async def _kept_on_another_host(wallet: AsyncClient, chain, monkeypatch, tx: str):
    """A pushed transfer kept on changed terms where the paying rail proves on another host: the
    409 that names `proof_url` and `recovery`, the wallet's account, the relayed calls, and the
    pushed credential."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    x402 = next(rail for rail in agent_payments._rails if rail.protocol == "x402")
    agent_payments.configure([
        MppTempoRail(secret="s" * 40, realm="pay.registry.test", recipient=RECIPIENT,
                     chain_id=42431), x402])
    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": tx}).to_authorization()
    _raise_margin(monkeypatch)
    kept = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
    assert kept.status_code == 409, kept.text
    return kept.json()["detail"], account, calls, pushed


async def _recovery_quote(ref: str):
    return await agent_payments.quote(rest=EP, method="GET", path=f"/call/{EP}",
                                      query="aweme_id=7", query_items=(("aweme_id", "7"),),
                                      body=b"", recovery=ref)


async def test_a_recovery_on_another_host_runs_one_call(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """The 409 hands a recovery reference for the proof's host: sent there in X-Treg-Recovery,
    it gets the transfer's own recovery terms, so following the receipt twice runs one call.
    Followed again past its deadline, it is a final 410 offering no challenge to sign."""
    detail, account, calls, _ = await _kept_on_another_host(
        wallet, chain, monkeypatch, "0x" + "f8" * 32)
    follow = {"X-Treg-Recovery": detail["recovery"]}
    async with AsyncClient(transport=ASGITransport(app=app)) as client:
        asked = [await client.get(detail["proof_url"], headers=follow) for _ in range(2)]
        proofs = [await _mpp_proof(a, account) for a in asked]
        runs = [await client.get(detail["proof_url"], headers={**follow, "Authorization": p})
                for p in proofs]
        assert [r.status_code for r in runs] == [200, 409], runs[1].text
        assert all("x-treg-recovery" not in str(call).lower() for call in calls)
        later = time.time() + agent_payments.REPLAY_KEEP_S + 1
        monkeypatch.setattr(agent_payments.time, "time", lambda: later)
        expired = await client.get(detail["proof_url"], headers=follow)
    assert expired.status_code == 410, expired.text
    assert expired.json()["detail"]["reason"] == "recovery_expired"
    assert "www-authenticate" not in expired.headers
    assert "payment-required" not in expired.headers
    assert len(calls) == 1


async def test_every_recovery_term_expires_by_the_transfer_s_deadline(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """A kept transfer has one recovery deadline, the same in its first answer and a resent one.
    Recovery terms offered just before it expire by it, even after the challenge lifetime is
    raised, so the spent mark of a recovery that already ran outlives every proof under them."""
    detail, account, calls, pushed = await _kept_on_another_host(
        wallet, chain, monkeypatch, "0x" + "f9" * 32)
    resent = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
    assert resent.status_code == 409, resent.text
    assert resent.json()["detail"]["recovery"] == detail["recovery"]
    follow = {"X-Treg-Recovery": detail["recovery"]}
    async with AsyncClient(transport=ASGITransport(app=app)) as client:
        asked = await client.get(detail["proof_url"], headers=follow)
        run = await client.get(detail["proof_url"],
                               headers={**follow, "Authorization": await _mpp_proof(asked, account)})
    assert run.status_code == 200, run.text
    ref = agent_payments._RECOVERY.fullmatch(detail["recovery"])
    nonce, until = ref.group(1), int(ref.group(2))
    async with session_maker() as db:
        mark = (await db.execute(select(Ephemeral).where(
            Ephemeral.ns == agent_payments.KEPT_NS, Ephemeral.k == f"spent:{nonce}"))).scalars().one()
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MPP_CHALLENGE_TTL_S", "3600")
    get_settings.cache_clear()
    monkeypatch.setattr(agent_payments.time, "time", lambda: until - 10)
    terms = agent_payments.signed_terms(
        (paid := await _recovery_quote(detail["recovery"])).offer.terms,
        paid.offer.resource_url, agent_payments.Terms(0, 0))
    assert terms.recovery and terms.expires <= until
    assert terms.expires <= mark.expires_at.replace(tzinfo=UTC).timestamp()
    assert len(calls) == 1


async def test_with_the_switch_off_a_kept_transfer_offers_a_plain_proof(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """Issued while the switch was on, kept after it went off: the proof offered carries no terms
    an older instance could not read."""
    from mpp import Credential

    account = Account.create()
    chain.payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo,
                        payload={"type": "hash", "hash": "0x" + "f9" * 32}).to_authorization()
    _raise_margin(monkeypatch)
    _terms_switch(monkeypatch, False)
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "price_changed", r.text
    assert all("terms" not in (c.opaque or {}) for c in _mpp_challenges(r)), "no terms in the proof"


async def test_challenge_used_does_not_claim_the_first_call_ran(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """The first payment's settlement failed, so no call ran: the 409 says only that another
    payment used the challenge and that this transfer ran none."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    chain.fail = True
    refused = await wallet.get(f"/call/{EP}?aweme_id=7",
                               headers={"Authorization": _mpp_credential(challenge)})
    assert refused.json()["detail"]["reason"] == "settlement_failed", refused.text
    chain.fail = False
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "d6" * 32})
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "challenge_used", r.text
    message = r.json()["detail"]["message"]
    assert "ran no call" in message and "already ran" not in message
    assert calls == [] and await _balance(chain.payer) == FLOOR


async def test_a_challenge_s_terms_run_one_call_for_two_pushed_transfers(
        wallet: AsyncClient, payments_on, terms_on, chain, monkeypatch):
    """Both transfers landed, so both are credited, but one challenge buys one call."""
    from mpp import Credential

    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    answers = []
    for tx in ("d2", "d3"):
        pushed = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + tx * 32})
        answers.append(await wallet.get(f"/call/{EP}?aweme_id=7",
                                        headers={"Authorization": pushed.to_authorization()}))
    assert answers[0].status_code == 200, answers[0].text
    # Final: the call already ran, so nothing invites the wallet to run it again.
    assert answers[1].status_code == 409, answers[1].text
    assert answers[1].json()["detail"]["reason"] == "challenge_used"
    assert "www-authenticate" not in answers[1].headers
    assert answers[1].headers["X-Treg-Payment-Reference"]
    assert len(calls) == 1
    assert await _balance(chain.payer) == 2 * FLOOR - EP_MICRO


async def test_a_malformed_terms_token_is_repriced(anon: AsyncClient, payments_on, terms_on,
                                                   facilitator):
    """Only a credential carrying no terms is taken on today's; anything else treg did not
    issue asks for a fresh challenge."""
    from mpp.x402 import decode_payment_signature, encode_payment_signature

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    payload = decode_payment_signature(credential)
    payload["accepted"]["extra"]["tregTerms"] = "garbage"
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"PAYMENT-SIGNATURE": encode_payment_signature(payload)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "price_changed", r.text
    assert facilitator.settles == 0


@pytest.mark.parametrize("terms, reason", [
    ("0123456789abcdef0123456789abcdef", "price_changed"),   # a SIWX nonce's shape, on a payment
    (12345, "malformed_credential"),
    ("", "malformed_credential"),
])
async def test_a_payment_s_damaged_terms_are_never_today_s(anon: AsyncClient, payments_on,
                                                           terms_on, facilitator, terms, reason):
    from mpp.x402 import decode_payment_signature, encode_payment_signature

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    payload = decode_payment_signature(credential)
    payload["accepted"]["extra"]["tregTerms"] = terms
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"PAYMENT-SIGNATURE": encode_payment_signature(payload)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == reason, r.text
    assert facilitator.settles == 0


async def test_a_payment_is_credited_at_the_fee_it_was_offered(anon: AsyncClient, payments_on,
                                                               monkeypatch, terms_on):
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


@pytest.mark.parametrize("issued_by", ["new", "old"])
async def test_a_rolling_deploy_reads_both_challenge_formats(wallet: AsyncClient, payments_on,
                                                             chain, monkeypatch, issued_by):
    """An instance with the switch off still reads a challenge that carries terms, and one with it
    on reads a challenge that carries none: a pushed transfer pays wherever its retry lands."""
    from mpp import Credential

    _terms_switch(monkeypatch, issued_by == "new")
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "ce" * 32})
    _terms_switch(monkeypatch, issued_by == "old")
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 200, r.text


async def test_terms_expire(anon: AsyncClient, payments_on, terms_on, facilitator, monkeypatch):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    later = time.time() + agent_payments._terms_ttl_s() + 1
    monkeypatch.setattr(agent_payments.time, "time", lambda: later)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "price_changed", r.text
    assert facilitator.settles == 0


async def test_one_x402_challenge_s_terms_buy_one_authorization(anon: AsyncClient, payments_on,
                                                                terms_on, facilitator):
    """The terms ride outside what the wallet signs, so they are claimed by the first
    authorization made under them; another one signed from the same challenge buys nothing."""
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    first, _ = _x402_credential(challenge)
    second, _ = _x402_credential(challenge)
    assert (await anon.get(f"/call/{EP}?aweme_id=7",
                           headers={"PAYMENT-SIGNATURE": first})).status_code == 200
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": second})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "challenge_used", r.text
    assert facilitator.settles == 1
    again = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": first})
    assert again.json()["detail"]["reason"] == "payment_replayed"


async def test_a_pushed_transfer_refused_at_settlement_is_left_to_the_reconciler(
        anon: AsyncClient, payments_on, chain):
    """A pushed transfer is on chain before treg sees it: a settlement that refuses it (its
    challenge expired since verification) leaves it for the reconciler, never `failed`."""
    from mpp import Credential

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    echo = next(c for c in _mpp_challenges(challenge) if c.request["amount"] != "0").to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": "0x" + "cf" * 32})
    chain.fail = True
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "payment_outcome_unknown", r.text
    async with session_maker() as db:
        assert (await db.execute(select(AgentPayment.status))).scalar_one() == "settling"


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


@pytest.mark.parametrize("stored", [True, False], ids=["quote-stored", "submitted-before"])
async def test_a_wallet_s_task_settles_within_its_quote(clients: AsyncClient, monkeypatch, stored):
    """The quote outlives the submission: the terminal settle, on a later poll, charges within
    it. A wallet's task submitted before the basis stored the quote settles within its hold."""
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
    if not stored:
        async with session_maker() as db:
            db.add(AgentPayer(network="eip155:84532", address="0x" + "aa" * 20, org_id=org_id))
            await db.commit()
    marked = call_wallet.current.set(
        call_wallet.WalletCall(quote_micro=quote, paid=True) if stored else None)
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
