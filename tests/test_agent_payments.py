"""Agent payments: a caller with no treg credential pays a catalog call per call with MPP (Tempo
charge) or x402 (`exact`). docs/context/architecture/agent-payments.md.

The rails are the real adapters. Only the chain is replaced: the Tempo charge intent's
`validate`/`broadcast` (so the HMAC challenge, expiry and route binding are pympp's own), and the
x402 facilitator (so the EIP-3009 payload is a real signature made offline).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from eth_account import Account
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from treg.api import app
from treg.application import agent_payments
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.catalog import store as catalog_store
from treg.infra.agent_payments.evm import TRANSFER_TOPIC, Chain, topic_address
from treg.infra.agent_payments.tempo import MppTempoRail
from treg.infra.agent_payments.x402 import X402Rail
from treg.infra.db import session_maker
from treg.models import AgentPayer, AgentPayment, Hold, LedgerEntry, Membership

EP = "tikhub.tiktok.video.comments"   # $0.001 per call on treg's key
EP_MICRO = 1_000
FLOOR = 50_000
POLL = "bounceban.people.email.verify.status"
RECIPIENT = "0x742d35Cc6634c0532925a3b844bC9e7595F8fE00"
TEMPO_PAYER = "0x" + "ab" * 20
X402_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class FakeTempoChain:
    """Stands in for the Tempo RPC behind pympp's charge intent."""

    def __init__(self) -> None:
        self.broadcasts = 0
        self.fail = False
        self.unknown = False
        self.payer_verified = True
        self.co_signed = False
        self.lifetime: int | None = 60  # pympp's client signs an expiring nonce valid this long
        self.payer = TEMPO_PAYER        # who the faked transfers come from
        self.real_validate = None       # pympp's own check, for zero-amount proofs

    async def validate(self, intent, credential, request):
        from mpp._validation import Validation

        payload = credential.payload
        if payload.get("type") == "proof":
            return await self.real_validate(intent, credential, request)
        if payload.get("type") == "hash":
            details = {"mode": "push", "payer_verified": True,
                       "transaction_hash": payload["hash"].lower()}
        else:
            signed = payload["signature"]
            details = {"mode": "pull", "payer_verified": self.payer_verified,
                       "serialized_transaction": signed,
                       "valid_before": int(time.time()) + self.lifetime if self.lifetime else None,
                       "transaction_hash": None if self.co_signed else tx_hash(signed)}
        return Validation(credential=credential, details=details, intent="charge",
                          request=dict(request), payer=self.payer)

    async def broadcast(self, intent, credential, request):
        from mpp import Receipt
        from mpp.errors import VerificationFailedError

        if self.fail:
            raise VerificationFailedError("transaction reverted")
        if self.unknown:
            from mpp.errors import InternalPaymentError

            raise InternalPaymentError()
        self.broadcasts += 1
        return Receipt.success(reference=f"0x{self.broadcasts:064x}")


def tx_hash(signed: str) -> str:
    import hashlib

    return "0x" + hashlib.sha256(signed.encode()).hexdigest()


class FakeFacilitator:
    def __init__(self) -> None:
        self.settles = 0
        self.verifies = 0
        self.fail = False
        self.fail_transaction = ""
        self.unreachable = False
        self.gate: asyncio.Event | None = None

    async def verify(self, payload, requirements):
        self.verifies += 1
        return {"isValid": True, "payer": payload["payload"]["authorization"]["from"]}

    async def settle(self, payload, requirements):
        if self.gate is not None:
            await self.gate.wait()
        if self.unreachable:
            raise httpx.ReadTimeout("facilitator timed out after submitting")
        if self.fail:
            return {"success": False, "errorReason": "insufficient_funds",
                    "transaction": self.fail_transaction,
                    "network": requirements["network"]}
        self.settles += 1
        return {"success": True, "transaction": f"0x{self.settles:064x}",
                "network": requirements["network"],
                "payer": payload["payload"]["authorization"]["from"]}


@pytest.fixture
def chain(monkeypatch):
    from mpp.methods.tempo.intents import ChargeIntent

    fake = FakeTempoChain()
    real = ChargeIntent.validate
    fake.real_validate = lambda intent, c, r: real(intent, c, r)
    monkeypatch.setattr(ChargeIntent, "validate",
                        lambda self, c, r: fake.validate(self, c, r))
    monkeypatch.setattr(ChargeIntent, "broadcast",
                        lambda self, c, r: fake.broadcast(self, c, r))
    return fake


@pytest.fixture
def facilitator():
    return FakeFacilitator()


@pytest.fixture
def payments_on(monkeypatch, chain, facilitator):
    for name, value in {
        "TREG_PLATFORM_KEY_TIKHUB": "PLATFORM-TIKHUB-KEY",
        "TREG_PLATFORM_KEY_BOUNCEBAN": "PLATFORM-BOUNCEBAN-KEY",
        "TREG_PLATFORM_PROVIDERS": "tikhub,bounceban",
        "TREG_AGENT_PAYMENTS_ENABLED": "1",
        "TREG_AGENT_PAYMENTS_MPP_ENABLED": "1",
        "TREG_AGENT_PAYMENTS_X402_ENABLED": "1",
        "TREG_AGENT_PAYMENTS_TEMPO_RECIPIENT": RECIPIENT,
        "TREG_AGENT_PAYMENTS_X402_PAY_TO": RECIPIENT,
        "TREG_AGENT_PAYMENTS_SECRET": "s" * 40,
        "TREG_AGENT_PAYMENTS_MIN_PAYMENT_MICRO": str(FLOOR),
        "TREG_PUBLIC_URL": "https://registry.test",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    agent_payments.configure([
        MppTempoRail(secret="s" * 40, realm="registry.test", recipient=RECIPIENT, chain_id=42431),
        X402Rail(pay_to=RECIPIENT, network="eip155:84532", asset=X402_ASSET, asset_name="USDC",
                 asset_version="2", facilitator_url="https://facilitator.invalid",
                 max_timeout_s=120, facilitator=facilitator, origin="https://registry.test"),
    ])
    yield
    agent_payments.configure([])
    get_settings.cache_clear()


@pytest.fixture
async def anon(clients):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c


@pytest.fixture
async def wallet(clients):
    """A keyless client calling the MPP realm's own host, so it is offered the zero-amount proof."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry.test") as c:
        yield c


def _mpp_challenges(response: httpx.Response) -> list:
    from mpp import Challenge

    return [Challenge.from_www_authenticate(h) for h in response.headers.get_list("www-authenticate")]


async def _mpp_proof(challenge_response: httpx.Response, account) -> str:
    """A zero-amount proof signed by pympp's own client, as an agent's wallet signs it."""
    from mpp.methods.tempo import client as tempo_client
    from mpp.methods.tempo.account import TempoAccount
    from mpp.runtime import CredentialParams

    challenge = next(c for c in _mpp_challenges(challenge_response) if c.request["amount"] == "0")
    key = account.key.hex()
    method = tempo_client.charge(account=TempoAccount.from_key(key if key.startswith("0x") else "0x" + key),
                                 chain_id=42431, rpc_url="http://rpc.invalid")
    return await method.create_credential(CredentialParams(challenge=challenge))


def _mpp_credential(challenge_response: httpx.Response, signature: str = "0x76" + "11" * 40) -> str:
    from mpp import Credential

    challenge = next(c for c in _mpp_challenges(challenge_response) if c.request["amount"] != "0")
    return Credential(challenge=challenge.to_echo(),
                      payload={"type": "transaction", "signature": signature}).to_authorization()


def _x402_credential(challenge_response: httpx.Response, account=None,
                     lifetime: int | None = None) -> tuple[str, str]:
    import os

    from mpp.methods.evm._eip3009 import authorization_domain, sign_authorization
    from mpp.x402 import decode_payment_required, encode_payment_signature

    account = account or Account.create()
    required = decode_payment_required(challenge_response.headers["payment-required"])
    accepted = required["accepts"][0]
    now = int(time.time())
    authorization = {"from": account.address, "to": accepted["payTo"], "value": accepted["amount"],
                     "validAfter": str(now - 600),
                     "validBefore": str(now + (lifetime or int(accepted["maxTimeoutSeconds"]))),
                     "nonce": "0x" + os.urandom(32).hex()}
    signature = sign_authorization(account, payload=authorization, domain=authorization_domain(
        name=accepted["extra"]["name"], version=accepted["extra"]["version"],
        chain_id=int(accepted["network"].split(":")[1]), currency=accepted["asset"]))
    payload = {"x402Version": 2, "accepted": accepted, "resource": required["resource"],
               "payload": {"authorization": authorization, "signature": signature}}
    return encode_payment_signature(payload), account.address.lower()


async def _payer_org(address: str) -> int:
    async with session_maker() as db:
        return (await db.execute(
            select(AgentPayer).where(AgentPayer.address == address))).scalar_one().org_id


async def _balance(address: str) -> int:
    async with session_maker() as db:
        return await ledger.balance_of(db, await _payer_org(address))


async def _entries(address: str) -> list[LedgerEntry]:
    org_id = await _payer_org(address)
    async with session_maker() as db:
        return list((await db.execute(select(LedgerEntry).where(
            LedgerEntry.org_id == org_id))).scalars())


def _fake_relay(status: int, body: bytes = b"{}", headers=(), calls: list | None = None):
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None,
                     force_identity=False):
        if calls is not None:
            calls.append(upstream_url)

        async def _stream():
            yield body

        async def _close():
            return None

        return UpstreamResponse(status, tuple(headers), _stream(), _close)
    return _relay


# ---- who is asked to pay -------------------------------------------------------------------------
async def test_flag_off_a_keyless_call_is_refused_as_before(anon: AsyncClient):
    r = await anon.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 401
    assert r.json()["detail"] == "not authenticated"
    assert "www-authenticate" not in r.headers and "payment-required" not in r.headers


async def test_a_keyless_paid_call_is_challenged_by_both_protocols(anon: AsyncClient, payments_on):
    from mpp.x402 import decode_payment_required

    r = await anon.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402, r.text
    assert r.headers["X-Treg-Error"] == "1" and r.headers["cache-control"] == "no-store"
    detail = r.json()["detail"]
    assert detail["error"] == "payment_required" and detail["reason"] == "payment_required"
    assert detail["amount_micro"] == FLOOR and detail["estimated_cost_micro"] == EP_MICRO
    assert detail["minimum_payment_micro"] == FLOOR
    assert "smallest payment" in detail["message"] and "not paid back" in detail["message"]
    assert "costs at most $0.001" in detail["message"]
    assert all(v.startswith("Payment ") for v in r.headers.get_list("www-authenticate"))
    required = decode_payment_required(r.headers["payment-required"])
    assert required["accepts"][0]["amount"] == str(FLOOR)
    assert required["resource"]["url"] == f"https://registry.test/call/{EP}?aweme_id=7"


async def test_a_challenge_names_only_the_protocols_offered(anon: AsyncClient, payments_on):
    agent_payments.configure([
        MppTempoRail(secret="s" * 40, realm="registry.test", recipient=RECIPIENT, chain_id=42431)])
    r = await anon.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402, r.text
    detail = r.json()["detail"]
    assert detail["protocols"] == ["mpp"]
    assert "x402" not in detail["message"] and "PAYMENT-REQUIRED" not in detail["message"]
    assert "payment-required" not in r.headers


async def test_free_own_and_passthrough_calls_get_no_challenge(anon: AsyncClient, payments_on):
    free = next(ep["id"] for ep in catalog_store.load().by_id.values()
                if (ep.get("cost") or {}).get("type") == "free"
                and (ep.get("method") or "GET") == "GET")
    for path, reason in ((f"/call/{free}", "free"), ("/call/echo/anything", "not_a_catalog_tool"),
                         ("/call/https://api.tikhub.io/x", "not_a_catalog_tool"),
                         (f"/call/{EP}", "wrong_method")):
        if reason == "wrong_method":
            r = await anon.delete(path)
            assert r.status_code == 401 and r.json()["detail"]["reason"] == reason, r.text
            continue
        r = await anon.get(path)
        assert r.status_code == 401, (path, r.text)
        assert "payment-required" not in r.headers and "www-authenticate" not in r.headers
        detail = r.json()["detail"]
        assert detail["reason"] == reason and "Sign in" in detail["message"], (path, detail)


async def test_a_request_treg_cannot_price_gets_no_challenge(anon: AsyncClient, payments_on):
    r = await anon.get("/call/tikhub.tiktok.user.profile?uniqueId=a&uniqueId=b")
    assert r.status_code == 401 and "payment-required" not in r.headers, r.text
    assert r.json()["detail"]["reason"] == "invalid_request"


async def test_any_treg_credential_takes_the_account_path(anon: AsyncClient, clients: AsyncClient,
                                                         payments_on):
    for headers in ({"X-Treg-Token": "not-a-real-token"}, {"X-Treg-Token": ""}):
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers=headers)
        assert r.status_code == 401, r.text
        assert "payment-required" not in r.headers
    anon.cookies.set("treg_session", "forged")
    r = await anon.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 401 and "payment-required" not in r.headers
    # A member's own payment-looking header is theirs to send upstream; treg never reads it.
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": "member-value"})
    assert r.status_code == 200, r.text
    assert r.json()["headers"]["payment-signature"] == "member-value"
    assert "X-Treg-Payment" not in r.headers
    async with session_maker() as db:
        assert (await db.execute(select(AgentPayment))).first() is None


# ---- what the catalog tells a keyless agent -------------------------------------------------------
async def test_search_and_details_say_whether_a_wallet_can_pay(anon: AsyncClient, payments_on):
    detail = (await anon.get(f"/catalog/endpoints/{EP}")).json()["endpoint"]["wallet"]
    assert detail["payable"] and detail["minimum_payment_micro"] == FLOOR
    assert detail["protocols"] == ["mpp", "x402"] and "not paid back" in detail["note"]
    poll = (await anon.get(f"/catalog/endpoints/{POLL}")).json()["endpoint"]["wallet"]
    assert poll["payable"] and poll["free_for_task_owner"]
    rows = (await anon.get("/catalog/search", params={"q": "tiktok video comments", "limit": 50})).json()["results"]
    row = next(r for r in rows if r["id"] == EP)
    assert row["wallet"]["payable"]
    assert any(r.get("wallet", {}).get("payable") is False and r["wallet"]["reason"] for r in rows)


async def test_the_catalog_says_nothing_about_wallets_while_payments_are_off(anon: AsyncClient):
    assert "wallet" not in (await anon.get(f"/catalog/endpoints/{EP}")).json()["endpoint"]


async def _agent_files(client: AsyncClient) -> list[str]:
    return [(await client.get(path)).text for path in ("/llms.txt", "/skill.md")]


async def test_the_agent_files_say_nothing_about_wallets_while_payments_are_off(anon: AsyncClient):
    for body in await _agent_files(anon):
        assert "Pay with a wallet" not in body and "agentpay" not in body


async def test_the_agent_files_teach_the_wallet_road_while_payments_are_on(anon: AsyncClient,
                                                                         payments_on):
    for body in await _agent_files(anon):
        assert "Pay with a wallet" in body
        assert "<!--agentpay-->" not in body and "<!--/agentpay-->" not in body


# ---- polling a task the wallet started ------------------------------------------------------------
async def test_a_poll_is_refused_to_a_wallet_treg_does_not_know(anon: AsyncClient, payments_on,
                                                               facilitator):
    challenge = await anon.get(f"/call/{POLL}?id=task-1")
    assert challenge.status_code == 402, challenge.text
    assert challenge.json()["detail"]["reason"] == "proof_required"
    assert "never settles" in challenge.json()["detail"]["message"]
    assert "No money moves" in challenge.json()["detail"]["message"]
    credential, payer = _x402_credential(challenge)
    r = await anon.get(f"/call/{POLL}?id=task-1", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "unknown_wallet"
    assert facilitator.settles == 0
    async with session_maker() as db:
        assert (await db.execute(select(AgentPayer).where(
            AgentPayer.address == payer))).first() is None


async def test_a_poll_needs_a_short_lived_signature(anon: AsyncClient, payments_on, facilitator,
                                                    monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    account = Account.create()
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge, account)
    assert (await anon.get(f"/call/{EP}?aweme_id=7",
                           headers={"PAYMENT-SIGNATURE": credential})).status_code == 200
    challenge = await anon.get(f"/call/{POLL}?id=task-1")
    credential, _ = _x402_credential(challenge, account, lifetime=3600)
    r = await anon.get(f"/call/{POLL}?id=task-1", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "signature_needed"
    assert "150 seconds" in r.json()["detail"]["message"]
    assert facilitator.settles == 1


async def test_a_known_wallet_polls_as_its_team_and_never_pays(anon: AsyncClient, payments_on,
                                                               facilitator, monkeypatch):
    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    account = Account.create()
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge, account)
    assert (await anon.get(f"/call/{EP}?aweme_id=7",
                           headers={"PAYMENT-SIGNATURE": credential})).status_code == 200
    balance = await _balance(payer)
    challenge = await anon.get(f"/call/{POLL}?id=someone-elses-task")
    credential, _ = _x402_credential(challenge, account)
    r = await anon.get(f"/call/{POLL}?id=someone-elses-task",
                       headers={"PAYMENT-SIGNATURE": credential})
    # The poll ran as the payer team, and treg's ownership check refused another team's task.
    assert r.status_code == 403 and r.json()["detail"]["error"] == "async_resource_not_owned"
    assert r.headers["X-Treg-Payment"] == "credit"
    assert facilitator.settles == 1 and len(calls) == 1
    assert await _balance(payer) == balance


# ---- paying -------------------------------------------------------------------------------------
async def test_an_mpp_payment_relays_the_answer_and_settles_the_cost(anon: AsyncClient, payments_on,
                                                                    chain):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"Authorization": _mpp_credential(challenge)})
    assert r.status_code == 200, r.text
    assert r.json()["query"] == {"aweme_id": "7"}
    upstream_auth = r.json()["headers"].get("authorization", "")
    assert not upstream_auth.startswith("Payment "), "the payment credential reached upstream"
    assert r.headers["X-Treg-Payment"] == "paid" and "payment-receipt" in r.headers
    assert int(r.headers["X-Treg-Cost-Micro"]) == EP_MICRO
    assert int(r.headers["X-Treg-Paid-Micro"]) == FLOOR
    assert int(r.headers["X-Treg-Credit-Micro"]) == FLOOR - EP_MICRO
    assert r.headers["cache-control"] == "private"
    assert chain.broadcasts == 1
    assert await _balance(TEMPO_PAYER) == FLOOR - EP_MICRO
    kinds = {e.kind: e.amount_micro for e in await _entries(TEMPO_PAYER)}
    assert sorted(kinds) == ["reserve", "settle", "topup"] and kinds["topup"] == FLOOR
    async with session_maker() as db:
        row = (await db.execute(select(AgentPayment))).scalar_one()
    assert (row.status, row.protocol, row.amount_micro) == ("settled", "mpp", FLOOR)


async def test_an_x402_payment_relays_the_answer_and_settles_the_cost(anon: AsyncClient,
                                                                     payments_on, facilitator):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200, r.text
    assert "payment-signature" not in r.json()["headers"]
    assert r.headers["X-Treg-Payment"] == "paid" and "payment-response" in r.headers
    assert facilitator.settles == 1
    assert await _balance(payer) == FLOOR - EP_MICRO


async def test_credit_pays_the_next_call_without_settling_again(anon: AsyncClient, payments_on,
                                                                facilitator):
    account = Account.create()
    for expected_settles, mode in ((1, "paid"), (1, "credit"), (1, "credit")):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        credential, payer = _x402_credential(challenge, account)
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
        assert r.status_code == 200, r.text
        assert r.headers["X-Treg-Payment"] == mode
        assert facilitator.settles == expected_settles
    assert "payment-response" not in r.headers  # nothing was settled for a credit draw
    assert await _balance(payer) == FLOOR - 3 * EP_MICRO


@pytest.mark.parametrize("target", ["org", "user"])
async def test_a_suspended_payer_is_refused_before_any_payment(anon: AsyncClient, payments_on,
                                                              facilitator, target):
    from treg.models import Membership, Org, User

    account = Account.create()
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge, account)
    assert (await anon.get(f"/call/{EP}?aweme_id=7",
                           headers={"PAYMENT-SIGNATURE": credential})).status_code == 200
    org_id = await _payer_org(payer)
    async with session_maker() as db:
        if target == "org":
            (await db.get(Org, org_id)).suspended = True
        else:
            member = (await db.execute(select(Membership).where(
                Membership.org_id == org_id))).scalar_one()
            (await db.get(User, member.user_id)).suspended = True
        await db.commit()
    for lifetime in (None, 3600):  # one would draw credit, the other would settle
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        credential, _ = _x402_credential(challenge, account, lifetime=lifetime)
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["reason"] == f"{'org' if target == 'org' else 'account'}_suspended"
    assert facilitator.settles == 1
    async with session_maker() as db:
        assert len((await db.execute(select(AgentPayment))).scalars().all()) == 1


async def test_concurrent_first_payments_share_one_payer_team(clients, payments_on, monkeypatch):
    real = agent_payments.make_payer_team
    address = "0x" + "cd" * 20

    async def _loses_the_race(db, network, addr):
        async with session_maker() as other:  # the concurrent request commits first
            await real(other, network, addr)
            await other.commit()
        return await real(db, network, addr)

    monkeypatch.setattr(agent_payments, "make_payer_team", _loses_the_race)
    org_id = await agent_payments._payer_org("eip155:84532", address)
    assert org_id == await _payer_org(address)


async def test_a_long_lived_payment_is_settled_even_when_credit_covers_the_call(
        anon: AsyncClient, payments_on, facilitator):
    account = Account.create()
    for lifetime, mode, settles in ((None, "paid", 1), (3600, "paid", 2), (None, "credit", 2)):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        credential, payer = _x402_credential(challenge, account, lifetime=lifetime)
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
        assert r.status_code == 200, r.text
        assert (r.headers["X-Treg-Payment"], facilitator.settles) == (mode, settles)
    assert await _balance(payer) == 2 * FLOOR - 3 * EP_MICRO


async def test_a_tempo_transfer_with_no_expiry_is_refused(anon: AsyncClient, payments_on, chain):
    chain.lifetime = None
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={
        "Authorization": _mpp_credential(challenge, _tempo_tx(b"\x01" * 65))})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "expiry_required", r.text
    assert chain.broadcasts == 0
    async with session_maker() as db:
        assert (await db.execute(select(AgentPayment))).scalars().all() == []


async def test_an_mpp_challenge_expires_as_configured(anon: AsyncClient, payments_on):
    from mpp import Challenge

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    parsed = Challenge.from_www_authenticate(challenge.headers.get_list("www-authenticate")[0])
    left = datetime.fromisoformat(parsed.expires.replace("Z", "+00:00")) - datetime.now(UTC)
    assert timedelta(seconds=100) < left <= timedelta(seconds=120)



async def test_the_fee_raises_the_payment_and_is_never_credited(anon: AsyncClient, payments_on,
                                                               facilitator, monkeypatch):
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_FEE_BPS", "500")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MIN_PAYMENT_MICRO", str(EP_MICRO))
    get_settings.cache_clear()
    assert (await anon.get(f"/catalog/endpoints/{EP}")).json()["endpoint"]["wallet"]["fee_bps"] == 500
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    detail = challenge.json()["detail"]
    assert detail["amount_micro"] == 1_053 and detail["fee_bps"] == 500 and "5% fee" in detail["message"]
    credential, payer = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        row = (await db.execute(select(AgentPayment))).scalars().one()
    assert (row.amount_micro, row.fee_micro) == (1_053, 52)
    assert await _balance(payer) == 1_053 - 52 - EP_MICRO

# ---- refusals -----------------------------------------------------------------------------------
async def test_a_replayed_payment_is_refused(anon: AsyncClient, payments_on, chain, facilitator):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    mpp_credential = _mpp_credential(challenge)
    x402_credential, _ = _x402_credential(challenge)
    for headers in ({"Authorization": mpp_credential}, {"PAYMENT-SIGNATURE": x402_credential}):
        first = await anon.get(f"/call/{EP}?aweme_id=7", headers=headers)
        assert first.status_code == 200, first.text
        again = await anon.get(f"/call/{EP}?aweme_id=7", headers=headers)
        assert again.status_code == 402
        assert again.json()["detail"]["reason"] == "payment_replayed"
        assert "payment-required" in again.headers  # a fresh challenge to pay again
    assert chain.broadcasts == 1 and facilitator.settles == 1


async def test_the_same_signed_transfer_cannot_pay_under_a_new_challenge(anon: AsyncClient,
                                                                        payments_on, chain):
    first = await anon.get(f"/call/{EP}?aweme_id=7")
    assert (await anon.get(f"/call/{EP}?aweme_id=7",
                           headers={"Authorization": _mpp_credential(first)})).status_code == 200
    second = await anon.get(f"/call/{EP}?aweme_id=7")
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": _mpp_credential(second)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "payment_replayed"


async def test_a_payment_for_another_request_is_refused(anon: AsyncClient, payments_on, chain,
                                                       facilitator):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    x402_credential, _ = _x402_credential(challenge)
    for headers in ({"Authorization": _mpp_credential(challenge)},
                    {"PAYMENT-SIGNATURE": x402_credential}):
        r = await anon.get(f"/call/{EP}?aweme_id=8", headers=headers)
        assert r.status_code == 402, r.text
        assert r.json()["detail"]["reason"] == "invalid_challenge"
    assert chain.broadcasts == 0 and facilitator.settles == 0


async def test_a_payment_for_a_different_amount_is_refused(anon: AsyncClient, payments_on,
                                                          facilitator, monkeypatch):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MIN_PAYMENT_MICRO", str(FLOOR * 2))
    get_settings.cache_clear()
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "invalid_challenge"
    assert facilitator.settles == 0


async def test_x402_version_1_is_refused_and_never_relayed(anon: AsyncClient, payments_on):
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"X-PAYMENT": "e30="})
    assert r.status_code == 402
    assert r.json()["detail"]["reason"] == "unsupported_version"


async def test_a_failed_settlement_runs_no_call_and_credits_nothing(anon: AsyncClient, payments_on,
                                                                   facilitator, monkeypatch):
    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    facilitator.fail = True
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "settlement_failed"
    assert calls == []
    assert await _balance(payer) == 0
    assert await _entries(payer) == []
    async with session_maker() as db:
        assert (await db.execute(select(AgentPayment.status))).scalar_one() == "failed"


# ---- a failed call keeps the payer's money --------------------------------------------------------
async def _released_once(address: str) -> None:
    kinds = sorted(e.kind for e in await _entries(address))
    assert kinds == ["release", "reserve", "topup"], kinds
    assert await _balance(address) == FLOOR
    async with session_maker() as db:
        assert (await db.execute(select(Hold).where(
            Hold.org_id == await _payer_org(address)))).first() is None


@pytest.mark.parametrize("failure", ["provider_5xx", "timeout"])
async def test_a_failed_call_releases_the_hold_once_and_keeps_the_credit(
        anon: AsyncClient, payments_on, facilitator, monkeypatch, failure):
    if failure == "provider_5xx":
        monkeypatch.setattr(call_service, "relay", _fake_relay(503, b'{"down":true}'))
    else:
        async def _timeout(*args, **kwargs):
            raise httpx.ReadTimeout("slow")
        monkeypatch.setattr(call_service, "relay", _timeout)
    account = Account.create()
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge, account)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code in (503, 502), r.text
    assert r.headers["X-Treg-Cost-Micro"] == "0"
    assert r.headers["X-Treg-Payment"] == "paid" and r.headers["X-Treg-Payment-Reference"]
    await _released_once(payer)
    # The payer was not charged: the next call draws the kept credit, nothing settles on chain.
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge, account)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200 and r.headers["X-Treg-Payment"] == "credit"
    assert facilitator.settles == 1


async def test_a_cancelled_call_releases_the_hold_once(anon: AsyncClient, payments_on, monkeypatch):
    async def _cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(call_service, "relay", _cancelled)
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge)
    with pytest.raises(asyncio.CancelledError):
        await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    await _released_once(payer)


async def test_payments_from_one_address_are_limited(anon: AsyncClient, payments_on, facilitator,
                                                     monkeypatch):
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MAX_PAYMENTS_PER_IP_MINUTE", "1")
    get_settings.cache_clear()
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    for expected in (200, 429):
        credential, _ = _x402_credential(challenge)
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
        assert r.status_code == expected, r.text
    assert r.json()["detail"]["reason"] == "rate_limited" and r.headers["retry-after"] == "60"
    assert facilitator.settles == 1
    # A request with no payment still gets its challenge.
    assert (await anon.get(f"/call/{EP}?aweme_id=7")).status_code == 402


# ---- 402s that are not payment challenges ---------------------------------------------------------
async def test_treg_own_balance_refusal_is_a_503_on_the_paid_path(anon: AsyncClient, payments_on,
                                                                 facilitator, monkeypatch):
    opened = agent_payments.open_call

    async def _drained(**kwargs):
        paid = await opened(**kwargs)
        async with session_maker() as db:  # a concurrent call spent the credit meanwhile
            org_id = int(paid.caller.org.id)
            await ledger.reserve(db, org_id, EP, await ledger.balance_of(db, org_id))
        return paid

    monkeypatch.setattr(agent_payments, "open_call", _drained)
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == "insufficient_balance"
    assert r.headers["X-Treg-Payment"] == "paid" and r.headers["X-Treg-Credit-Micro"] == "0"
    assert "payment-required" not in r.headers and "www-authenticate" not in r.headers


async def test_a_vendor_402_is_relayed_verbatim(anon: AsyncClient, payments_on, monkeypatch):
    vendor = b'{"vendor":"out of credit"}'
    monkeypatch.setattr(call_service, "relay", _fake_relay(
        402, vendor, headers=((b"www-authenticate", b'Payment id="vendor"'),
                              (b"content-type", b"application/json"))))
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 402 and r.content == vendor
    assert r.headers.get_list("www-authenticate") == ['Payment id="vendor"']
    assert "X-Treg-Error" not in r.headers and "payment-required" not in r.headers
    # treg's own headers only: which money paid, never a receipt on a non-2xx answer.
    assert r.headers["X-Treg-Payment"] == "paid" and "payment-response" not in r.headers


async def test_the_insufficient_balance_402_is_unchanged(clients: AsyncClient, payments_on):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, EP, await ledger.balance_of(db, org_id))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402
    assert r.json()["detail"]["error"] == "insufficient_balance"
    assert "payment-required" not in r.headers and "www-authenticate" not in r.headers


# ---- configuration ------------------------------------------------------------------------------
def test_the_feature_refuses_to_start_without_a_secret(monkeypatch):
    from treg.config import Settings

    monkeypatch.setenv("TREG_AGENT_PAYMENTS_ENABLED", "1")
    monkeypatch.delenv("TREG_AGENT_PAYMENTS_SECRET", raising=False)
    with pytest.raises(ValueError, match="TREG_AGENT_PAYMENTS_SECRET"):
        Settings().check_agent_payments()


def test_a_process_without_payment_code_starts_while_payments_lack_their_secrets(monkeypatch):
    from treg.bootstrap import configure_agent_payments
    from treg.config import Settings

    monkeypatch.setenv("TREG_AGENT_PAYMENTS_ENABLED", "1")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_SETTLEMENT", "stripe")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TEMPO_RECIPIENT", "not-an-address")
    for name in ("TREG_AGENT_PAYMENTS_SECRET", "TREG_AGENT_PAYMENTS_STRIPE_SECRET_KEY",
                 "TREG_STRIPE_SECRET_KEY"):
        monkeypatch.setenv(name, "")  # empty, not unset: an unset name still reads `.env`
    assert Settings().agent_payments_enabled
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="agent payments cannot start") as error:
            configure_agent_payments()
    finally:
        get_settings.cache_clear()
    for name in ("SECRET", "STRIPE_SECRET_KEY", "TEMPO_RECIPIENT"):
        assert f"TREG_AGENT_PAYMENTS_{name}" in str(error.value)


def test_a_challenge_carries_integer_micro_usd_as_token_base_units():
    from treg.infra.agent_payments import micro_to_decimal

    assert micro_to_decimal(1) == "0.000001"
    assert micro_to_decimal(1_000_000) == "1.000000"
    assert micro_to_decimal(12_345_678) == "12.345678"
    with pytest.raises(ValueError):
        micro_to_decimal(0)


def test_payment_headers_are_consumed_only_when_they_carry_a_payment(payments_on):
    assert agent_payments.consumes("Authorization", "Payment abc")
    assert not agent_payments.consumes("Authorization", "Bearer vendor-key")
    assert agent_payments.consumes("PAYMENT-SIGNATURE", "x")
    assert agent_payments.consumes("x-payment", "x")
    assert not agent_payments.consumes("content-type", "application/json")


# ---- what a Tempo signature can prove -------------------------------------------------------------
def _tempo_tx(signature: bytes) -> str:
    import rlp

    return "0x76" + rlp.encode([b""] * 13 + [signature]).hex()


async def test_an_unrecovered_mpp_signature_never_draws_credit(anon: AsyncClient, payments_on,
                                                              chain):
    chain.payer_verified = False  # a keychain, multisig, P256 or WebAuthn sender
    for n in (1, 2):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={
            "Authorization": _mpp_credential(challenge, _tempo_tx(bytes([n]) * 65))})
        assert r.status_code == 200, r.text
        assert r.headers["X-Treg-Payment"] == "paid"
    assert chain.broadcasts == 2  # the wallet held credit, but a named signer must settle


async def test_one_tempo_transfer_is_one_proof_in_either_mode(anon: AsyncClient, payments_on, chain):
    from mpp import Challenge, Credential

    signed = _tempo_tx(b"\x01" * 65)
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"Authorization": _mpp_credential(challenge, signed)})
    assert r.status_code == 200, r.text
    echo = Challenge.from_www_authenticate(challenge.headers.get_list("www-authenticate")[0]).to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": tx_hash(signed)})
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "payment_replayed"
    assert chain.broadcasts == 1


async def test_a_transfer_whose_hash_is_unknown_until_broadcast_is_refused(anon: AsyncClient,
                                                                          payments_on, chain):
    chain.co_signed = True  # a fee payer co-signs it, so there is no replay key before broadcast
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"Authorization": _mpp_credential(challenge, _tempo_tx(b"\x01" * 65))})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "malformed_credential"
    assert chain.broadcasts == 0


# ---- settlement that cannot be confirmed ----------------------------------------------------------
async def test_an_unknown_settlement_runs_no_call_and_stays_open(anon: AsyncClient, payments_on,
                                                                facilitator, monkeypatch):
    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, calls=calls))
    facilitator.unreachable = True
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "payment_outcome_unknown"
    assert r.headers["X-Treg-Error"] == "1" and "payment-required" not in r.headers
    assert calls == [] and await _entries(payer) == []
    async with session_maker() as db:
        row = (await db.execute(select(AgentPayment))).scalar_one()
    assert (row.status, row.error) == ("settling", "settlement_unknown")


async def test_a_caller_that_leaves_mid_settlement_is_still_credited(anon: AsyncClient, payments_on,
                                                                    facilitator):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge)
    paid = await agent_payments.quote(
        rest=EP, method="GET", path=f"/call/{EP}", query="aweme_id=7",
        query_items=(("aweme_id", "7"),), body=b"")
    facilitator.gate = asyncio.Event()
    admission = asyncio.create_task(agent_payments.admit(paid, {"payment-signature": credential}))
    while facilitator.gate is not None and not admission.done() and \
            not await _settling(payer):
        await asyncio.sleep(0.01)
    admission.cancel()
    facilitator.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await admission
    for _ in range(100):
        if await _settled(payer):
            break
        await asyncio.sleep(0.01)
    assert await _balance(payer) == FLOOR


async def _settling(payer: str) -> bool:
    async with session_maker() as db:
        return (await db.execute(select(AgentPayment).where(
            AgentPayment.payer_address == payer, AgentPayment.status == "settling"))).first() is not None


async def _settled(payer: str) -> bool:
    async with session_maker() as db:
        return (await db.execute(select(AgentPayment).where(
            AgentPayment.payer_address == payer, AgentPayment.status == "settled"))).first() is not None


# ---- a keyless body -----------------------------------------------------------------------------
async def test_a_keyless_body_is_capped(anon: AsyncClient, payments_on):
    post_ep = next(ep["id"] for ep in catalog_store.load().by_id.values()
                   if (ep.get("method") or "GET") == "POST" and ep["provider"] == "tikhub")
    big = b"x" * (agent_payments.MAX_BODY_BYTES + 1)
    r = await anon.post(f"/call/{post_ep}", content=big)
    assert r.status_code == 413
    r = await anon.post("/call/not-a-catalog-id/x", content=big)
    assert r.status_code == 401


# ---- the reconciler: payments whose settlement went unanswered -----------------------------------
class FakeEvm(Chain):
    """A chain the reconciler reads: receipts by hash, AuthorizationUsed logs, the head's time."""

    def __init__(self) -> None:
        super().__init__("http://chain.invalid")
        self.receipts: dict[str, dict] = {}
        self.authorizations: dict[tuple[str, str], str] = {}   # (payer, nonce) -> tx hash
        self.now = datetime.now(UTC).replace(tzinfo=None)
        self.tags: list[str] = []

    async def receipt(self, tx_hash):
        return self.receipts.get(tx_hash.lower())

    async def head(self, tag="latest"):
        self.tags.append(tag)
        return 1_000, self.now

    async def block_at(self, when):
        return 1

    async def logs(self, *, address, topics, from_block, to_block):
        tx = self.authorizations.get((topics[1], topics[2].lower()))
        return [{"transactionHash": tx}] if tx else []

    def pays(self, tx_hash: str, *, asset: str, payer: str, to: str, amount: int,
             status: str = "0x1") -> None:
        self.receipts[tx_hash.lower()] = {"transactionHash": tx_hash, "status": status, "blockNumber": "0x1", "logs": [{
            "address": asset, "data": "0x" + f"{amount:064x}",
            "topics": [TRANSFER_TOPIC, topic_address(payer), topic_address(to)]}]}


@pytest.fixture
def evm(payments_on):
    fake = FakeEvm()
    for rail in agent_payments._rails:
        rail._chain = fake
    return fake


async def _stuck(anon: AsyncClient, facilitator) -> AgentPayment:
    """One x402 payment left `settling` (the facilitator went silent), aged past the minimum."""
    facilitator.unreachable = True
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 503
    facilitator.unreachable = False
    return await _age_all()


async def _age_all() -> AgentPayment:
    async with session_maker() as db:
        row = (await db.execute(select(AgentPayment))).scalars().first()
        row.created_at = row.created_at - timedelta(minutes=10)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _row() -> AgentPayment:
    async with session_maker() as db:
        return (await db.execute(select(AgentPayment))).scalar_one()


async def test_the_reconciler_credits_an_x402_payment_that_landed(anon: AsyncClient, facilitator,
                                                                  evm):
    row = await _stuck(anon, facilitator)
    nonce = row.proof_id.rsplit(":", 1)[-1]
    evm.authorizations[(topic_address(row.payer_address), nonce)] = "0xfeed"
    evm.pays("0xfeed", asset=row.asset, payer=row.payer_address, to=row.pay_to, amount=FLOOR)
    result = await agent_payments.reconcile()
    assert (result.landed, result.failed, result.pending) == (1, 0, 0)
    assert await _balance(row.payer_address) == FLOOR
    assert ((await _row()).status, (await _row()).reference) == ("settled", "0xfeed")
    again = await agent_payments.reconcile()
    assert again.landed == 0
    assert [e.kind for e in await _entries(row.payer_address)] == ["topup"]


async def test_the_reconciler_credits_an_mpp_payment_that_landed(anon: AsyncClient, chain, evm):
    chain.unknown = True
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"Authorization": _mpp_credential(challenge, _tempo_tx(b"\x01" * 65))})
    assert r.status_code == 503, r.text
    row = await _age_all()
    tx_hash = row.proof_id.rsplit(":", 1)[-1]
    evm.pays(tx_hash, asset=row.asset, payer=row.payer_address, to=row.pay_to, amount=FLOOR)
    assert (await agent_payments.reconcile()).landed == 1
    assert await _balance(TEMPO_PAYER) == FLOOR


async def test_the_reconciler_fails_a_payment_that_can_no_longer_land(anon: AsyncClient,
                                                                       facilitator, evm):
    row = await _stuck(anon, facilitator)
    evm.now = row.expires_at + timedelta(minutes=5)
    result = await agent_payments.reconcile()
    assert result.failed == 1 and result.reasons == {"expired_unsettled": 1}
    assert ((await _row()).status, (await _row()).error) == ("failed", "expired_unsettled")
    assert await _entries(row.payer_address) == []


async def test_the_reconciler_waits_while_a_payment_can_still_land(anon: AsyncClient, facilitator,
                                                                   evm):
    row = await _stuck(anon, facilitator)
    evm.now = row.expires_at - timedelta(seconds=30)
    assert (await agent_payments.reconcile()).pending == 1
    assert (await _row()).status == "settling"


async def test_the_reconciler_never_credits_a_transfer_to_someone_else(anon: AsyncClient,
                                                                       facilitator, evm):
    row = await _stuck(anon, facilitator)
    nonce = row.proof_id.rsplit(":", 1)[-1]
    evm.authorizations[(topic_address(row.payer_address), nonce)] = "0xbad"
    evm.pays("0xbad", asset=row.asset, payer=row.payer_address, to="0x" + "99" * 20, amount=FLOOR)
    result = await agent_payments.reconcile()
    assert result.failed == 1 and result.reasons == {"transfer_mismatch": 1}
    assert await _entries(row.payer_address) == []


async def test_the_reconciler_leaves_young_payments_to_their_request(anon: AsyncClient, facilitator,
                                                                     evm):
    facilitator.unreachable = True
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    result = await agent_payments.reconcile()
    assert (result.landed, result.failed, result.pending) == (0, 0, 0)


async def test_the_reconciler_counts_payments_stuck_for_an_hour(anon: AsyncClient, facilitator,
                                                                evm):
    row = await _stuck(anon, facilitator)
    evm.now = row.created_at  # the authorization can still land: the row stays `settling`
    assert (await agent_payments.reconcile()).stuck == 0
    async with session_maker() as db:
        aged = await db.get(AgentPayment, row.id)
        aged.created_at = aged.created_at - timedelta(hours=1)
        db.add(aged)
        await db.commit()
    assert (await agent_payments.reconcile()).stuck == 1


async def test_a_payment_credited_twice_tops_up_once(anon: AsyncClient, facilitator, evm):
    row = await _stuck(anon, facilitator)
    await asyncio.gather(agent_payments._credit(row.id, "0xa"), agent_payments._credit(row.id, "0xa"))
    assert [e.kind for e in await _entries(row.payer_address)] == ["topup"]
    assert await _balance(row.payer_address) == FLOOR


async def _credit_proof(anon: AsyncClient, *, expired: bool = True) -> AgentPayment:
    """A paid call, then a call drawn on credit; returns the credit row, expired if asked."""
    account = Account.create()
    for mode in ("paid", "credit"):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        credential, _ = _x402_credential(challenge, account)
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
        assert r.headers["X-Treg-Payment"] == mode, r.text
    async with session_maker() as db:
        row = (await db.execute(select(AgentPayment).where(
            AgentPayment.status == "credit"))).scalar_one()
        if expired:
            row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row


async def _status(payment_id: int) -> str:
    async with session_maker() as db:
        return (await db.get(AgentPayment, payment_id)).status


async def test_a_credit_payment_settled_anyway_becomes_credit_once(anon: AsyncClient, facilitator,
                                                                   evm):
    row = await _credit_proof(anon)
    before = await _balance(row.payer_address)
    nonce = row.proof_id.rsplit(":", 1)[-1]
    evm.authorizations[(topic_address(row.payer_address), nonce)] = "0xlate"
    evm.pays("0xlate", asset=row.asset, payer=row.payer_address, to=row.pay_to, amount=FLOOR)
    assert (await agent_payments.reconcile()).late_landed == 1
    assert await _balance(row.payer_address) == before + FLOOR
    assert await _status(row.id) == "settled"
    assert (await agent_payments.reconcile()).late_landed == 0
    assert await _balance(row.payer_address) == before + FLOOR


async def test_an_expired_credit_payment_is_closed_once(anon: AsyncClient, facilitator, evm):
    row = await _credit_proof(anon)
    before = await _balance(row.payer_address)
    result = await agent_payments.reconcile()
    assert (result.closed, result.reasons) == (1, {"expired_unsettled": 1})
    assert await _status(row.id) == "expired"
    assert (await agent_payments.reconcile()).closed == 0
    assert await _balance(row.payer_address) == before


async def test_a_credit_payment_is_not_looked_up_before_it_expires(anon: AsyncClient, facilitator,
                                                                   evm):
    row = await _credit_proof(anon, expired=False)
    result = await agent_payments.reconcile()
    assert (result.closed, result.late_landed, result.reasons) == (0, 0, {})
    assert await _status(row.id) == "credit"


async def test_payments_of_a_switched_off_rail_never_block_the_reconciler(anon: AsyncClient,
                                                                            facilitator, evm):
    stuck = await _stuck(anon, facilitator)
    credit = await _credit_proof(anon)
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    async with session_maker() as db:
        for i, status in enumerate(["settling", "settling", "credit", "credit"]):
            db.add(AgentPayment(proof_id=f"old:{i}", protocol="x402", network="eip155:1",
                                payer_address=stuck.payer_address, org_id=stuck.org_id,
                                amount_micro=FLOOR, status=status, asset=X402_ASSET,
                                pay_to=RECIPIENT, expires_at=old, created_at=old))
        await db.commit()
    evm.now = stuck.expires_at + timedelta(minutes=5)
    result = await agent_payments.reconcile(limit=2)
    assert (result.failed, result.closed) == (1, 1)
    assert (await _status(stuck.id), await _status(credit.id)) == ("failed", "expired")


async def test_an_x402_payment_must_name_this_request(anon: AsyncClient, payments_on, facilitator):
    from mpp.x402 import decode_payment_signature, encode_payment_signature

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    payload = decode_payment_signature(credential)
    for resource in (None, {"url": "https://registry.test/call/tikhub.other?aweme_id=7"}):
        if resource is None:
            payload.pop("resource", None)
        else:
            payload["resource"] = resource
        r = await anon.get(f"/call/{EP}?aweme_id=7",
                           headers={"PAYMENT-SIGNATURE": encode_payment_signature(payload)})
        assert r.status_code == 402 and r.json()["detail"]["reason"] == "invalid_challenge", r.text
    assert (facilitator.verifies, facilitator.settles) == (0, 0)


async def test_a_malformed_x402_payment_is_refused_before_the_facilitator(anon: AsyncClient,
                                                                          payments_on, facilitator):
    import base64
    import json

    from mpp.x402 import decode_payment_signature

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    no_accepted = decode_payment_signature(credential)
    no_accepted.pop("accepted")
    no_expiry = decode_payment_signature(credential)
    no_expiry["payload"]["authorization"].pop("validBefore")
    for payload in (no_accepted, no_expiry):
        header = base64.b64encode(json.dumps(payload).encode()).decode()
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": header})
        assert r.status_code == 402 and r.json()["detail"]["reason"] == "malformed_credential", r.text
    assert facilitator.verifies == 0


async def test_payment_headers_never_reach_upstream_whichever_rails_are_on(payments_on, facilitator):
    agent_payments.configure([X402Rail(
        pay_to=RECIPIENT, network="eip155:84532", asset=X402_ASSET, asset_name="USDC",
        asset_version="2", facilitator_url="https://facilitator.invalid", max_timeout_s=120,
        facilitator=facilitator)])
    assert agent_payments.consumes("Authorization", "Payment id=1")
    assert agent_payments.consumes("PAYMENT-SIGNATURE", "x") and agent_payments.consumes("X-Payment", "x")
    assert not agent_payments.consumes("Authorization", "Bearer upstream-key")


async def test_a_wallet_whose_team_was_deleted_gets_a_new_team(anon: AsyncClient, payments_on):
    from sqlmodel import delete

    account = Account.create()
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge, account)
    assert (await anon.get(f"/call/{EP}?aweme_id=7",
                           headers={"PAYMENT-SIGNATURE": credential})).status_code == 200
    old_org = await _payer_org(payer)
    async with session_maker() as db:
        await db.execute(delete(AgentPayer).where(AgentPayer.address == payer))
        await db.execute(delete(Membership).where(Membership.org_id == old_org))
        await db.commit()
    credential, _ = _x402_credential(challenge, account)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200, r.text
    assert await _payer_org(payer) != old_org


async def test_the_reconciler_credits_a_transfer_larger_than_asked(anon: AsyncClient, facilitator,
                                                                  evm):
    row = await _stuck(anon, facilitator)
    nonce = row.proof_id.rsplit(":", 1)[-1]
    evm.authorizations[(topic_address(row.payer_address), nonce)] = "0xmore"
    evm.pays("0xmore", asset=row.asset, payer=row.payer_address, to=row.pay_to, amount=FLOOR + 1)
    assert (await agent_payments.reconcile()).landed == 1
    assert await _balance(row.payer_address) == FLOOR


async def test_a_failed_settlement_that_names_a_transaction_is_left_to_the_chain(
        anon: AsyncClient, payments_on, facilitator):
    facilitator.fail, facilitator.fail_transaction = True, "0x" + "ab" * 32
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 503, r.text
    assert (await _row()).status == "settling"


def test_usage_priced_tools_are_not_payable_with_a_wallet():
    from treg.application.call.resolve import wallet_refusal

    cat = catalog_store.load()
    usage = [ep for ep in cat.by_id.values()
             if "usage" in (ep.get("settle") or {})
             or "usage" in ((cat.cost_view(ep.get("cost"), ep["provider"]) or {}).get("settle") or {})]
    assert usage and all(wallet_refusal(ep) for ep in usage)
    assert any(wallet_refusal(ep) == "usage_priced" for ep in usage)


# ---- zero-amount proofs (MPP) ------------------------------------------------------------------
def _signature() -> str:
    import os

    return "0x76" + os.urandom(40).hex()


async def _mpp_paid(client: AsyncClient, chain, account, query: str = "aweme_id=7") -> httpx.Response:
    """One MPP payment from `account` for EP, settled."""
    chain.payer = account.address.lower()
    challenge = await client.get(f"/call/{EP}?{query}")
    return await client.get(f"/call/{EP}?{query}",
                            headers={"Authorization": _mpp_credential(challenge, _signature())})


async def test_the_proof_challenge_comes_first_on_the_realm_host(wallet: AsyncClient,
                                                                 anon: AsyncClient, payments_on):
    r = await wallet.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402, r.text
    amounts = [c.request["amount"] for c in _mpp_challenges(r)]
    proofs = [a == "0" for a in amounts]
    assert proofs[0] and not proofs[-1] and proofs == sorted(proofs, reverse=True), amounts
    assert "zero-amount MPP challenge" in r.json()["detail"]["message"]
    other = await anon.get(f"/call/{EP}?aweme_id=7")
    assert all(c.request["amount"] != "0" for c in _mpp_challenges(other))


async def test_a_proof_draws_credit_and_moves_no_money(wallet: AsyncClient, payments_on, chain):
    account = Account.create()
    assert (await _mpp_paid(wallet, chain, account)).status_code == 200
    payer = account.address.lower()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    proof = await _mpp_proof(challenge, account)
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": proof})
    assert r.status_code == 200, r.text
    assert r.headers["X-Treg-Payment"] == "credit" and "payment-receipt" not in r.headers
    assert not r.json()["headers"].get("authorization", "").startswith("Payment ")
    assert chain.broadcasts == 1
    assert await _balance(payer) == FLOOR - 2 * EP_MICRO
    async with session_maker() as db:
        assert len((await db.execute(select(AgentPayment))).scalars().all()) == 1
    again = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": proof})
    assert again.status_code == 402 and again.json()["detail"]["reason"] == "payment_replayed"


async def test_a_proof_with_too_little_credit_is_asked_to_pay(wallet: AsyncClient, payments_on,
                                                              chain):
    account = Account.create()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    r = await wallet.get(f"/call/{EP}?aweme_id=7",
                         headers={"Authorization": await _mpp_proof(challenge, account)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "credit_insufficient", r.text
    assert "credit ($0)" in r.json()["detail"]["message"]
    assert all(c.request["amount"] != "0" for c in _mpp_challenges(r)) and _mpp_challenges(r)
    assert chain.broadcasts == 0
    chain.payer = account.address.lower()
    paid = await wallet.get(f"/call/{EP}?aweme_id=7",
                            headers={"Authorization": _mpp_credential(r, _signature())})
    assert paid.status_code == 200 and paid.headers["X-Treg-Payment"] == "paid"


async def test_a_proof_for_another_request_is_refused(wallet: AsyncClient, payments_on, chain):
    account = Account.create()
    assert (await _mpp_paid(wallet, chain, account)).status_code == 200
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    r = await wallet.get(f"/call/{EP}?aweme_id=8",
                         headers={"Authorization": await _mpp_proof(challenge, account)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "invalid_challenge", r.text


async def test_an_mpp_payment_is_settled_even_when_credit_covers_the_call(wallet: AsyncClient,
                                                                         payments_on, chain):
    account = Account.create()
    assert (await _mpp_paid(wallet, chain, account)).status_code == 200
    r = await _mpp_paid(wallet, chain, account)
    assert r.status_code == 200 and r.headers["X-Treg-Payment"] == "paid"
    assert chain.broadcasts == 2
    async with session_maker() as db:
        rows = (await db.execute(select(AgentPayment))).scalars().all()
    assert [row.status for row in rows] == ["settled", "settled"]


async def test_credit_on_one_network_never_pays_on_another(wallet: AsyncClient, payments_on, chain,
                                                           facilitator):
    account = Account.create()
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge, account)
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"PAYMENT-SIGNATURE": credential})).status_code == 200
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    r = await wallet.get(f"/call/{EP}?aweme_id=7",
                         headers={"Authorization": await _mpp_proof(challenge, account)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "credit_insufficient"
    async with session_maker() as db:
        payers = (await db.execute(select(AgentPayer))).scalars().all()
    assert [(p.network, p.address) for p in payers] == [("eip155:84532", payer)]


async def test_a_wallet_polls_with_a_proof(wallet: AsyncClient, payments_on, chain, monkeypatch):
    account = Account.create()
    assert (await _mpp_paid(wallet, chain, account)).status_code == 200
    challenge = await wallet.get(f"/call/{POLL}?id=someone-elses-task")
    assert challenge.json()["detail"]["reason"] == "proof_required"
    assert [c.request["amount"] for c in _mpp_challenges(challenge)][:1] == ["0"]
    assert all(c.request["amount"] == "0" for c in _mpp_challenges(challenge))
    assert "payment-required" in challenge.headers   # x402 still proves with an unsettled payment
    r = await wallet.get(f"/call/{POLL}?id=someone-elses-task",
                         headers={"Authorization": await _mpp_proof(challenge, account)})
    # The poll ran as the payer team, and treg's ownership check refused another team's task.
    assert r.status_code == 403 and r.json()["detail"]["error"] == "async_resource_not_owned"
    assert chain.broadcasts == 1


async def test_a_wallet_polls_its_own_task_with_a_proof_and_no_other_wallet_can(
        wallet: AsyncClient, payments_on, chain, monkeypatch):
    calls: list = []
    monkeypatch.setattr(call_service, "relay", _fake_relay(
        200, b'{"id": "task-77", "status": "verifying"}', calls=calls))
    owner, other = Account.create(), Account.create()
    start = "bounceban.people.email.verify"
    for account in (owner, other):
        chain.payer = account.address.lower()
        challenge = await wallet.get(f"/call/{start}?email=a@b.co")
        r = await wallet.get(f"/call/{start}?email=a@b.co",
                             headers={"Authorization": _mpp_credential(challenge, _signature())})
        assert r.status_code == 200, r.text
        monkeypatch.setattr(call_service, "relay", _fake_relay(
            200, b'{"id": "task-88", "status": "verifying"}', calls=calls))
    paid = chain.broadcasts
    monkeypatch.setattr(call_service, "relay", _fake_relay(
        200, b'{"id": "task-77", "status": "success"}', calls=calls))

    challenge = await wallet.get(f"/call/{POLL}?id=task-77")
    r = await wallet.get(f"/call/{POLL}?id=task-77",
                         headers={"Authorization": await _mpp_proof(challenge, owner)})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"

    challenge = await wallet.get(f"/call/{POLL}?id=task-77")
    r = await wallet.get(f"/call/{POLL}?id=task-77",
                         headers={"Authorization": await _mpp_proof(challenge, other)})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "async_resource_not_owned"
    assert chain.broadcasts == paid


async def test_a_poll_proof_from_an_unknown_wallet_is_refused(wallet: AsyncClient, payments_on,
                                                              chain):
    challenge = await wallet.get(f"/call/{POLL}?id=task-1")
    r = await wallet.get(f"/call/{POLL}?id=task-1",
                         headers={"Authorization": await _mpp_proof(challenge, Account.create())})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "unknown_wallet"


async def test_credit_spent_by_another_call_asks_this_wallet_to_pay(wallet: AsyncClient, payments_on,
                                                                   chain, monkeypatch):
    from fastapi import HTTPException

    from treg.routers import call as call_router

    account = Account.create()
    assert (await _mpp_paid(wallet, chain, account)).status_code == 200

    async def _spent(*args, **kwargs):
        raise HTTPException(status_code=402, detail={"error": "insufficient_balance"})

    monkeypatch.setattr(call_router, "run_call_surface", _spent)
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    r = await wallet.get(f"/call/{EP}?aweme_id=7",
                         headers={"Authorization": await _mpp_proof(challenge, account)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "credit_insufficient", r.text
    assert _mpp_challenges(r) and all(c.request["amount"] != "0" for c in _mpp_challenges(r))


# ---- limits -------------------------------------------------------------------------------------
async def test_the_payment_check_cap_holds_across_addresses(anon: AsyncClient, payments_on,
                                                            facilitator, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MAX_VERIFICATIONS_MINUTE", "1")
    get_settings.cache_clear()
    for i, expected in enumerate((200, 429)):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        credential, _ = _x402_credential(challenge)
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={
            "PAYMENT-SIGNATURE": credential, "X-Forwarded-For": f"203.0.113.{i + 1}"})
        assert r.status_code == expected, r.text
    assert r.json()["detail"]["reason"] == "rate_limited" and r.headers["retry-after"] == "60"
    assert facilitator.verifies == 1


async def test_unpaid_requests_are_limited_per_address(anon: AsyncClient, payments_on, monkeypatch):
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MAX_QUOTES_PER_IP_MINUTE", "2")
    get_settings.cache_clear()
    statuses = [(await anon.get(f"/call/{EP}?aweme_id=7")).status_code for _ in range(3)]
    assert statuses == [402, 402, 429]


# ---- the credit route ---------------------------------------------------------------------------
async def test_the_credit_route_does_not_exist_while_payments_are_off(anon: AsyncClient):
    from treg.models import Ephemeral

    limit = get_settings().agent_payments_max_quotes_per_ip_minute
    for _ in range(limit + 10):
        assert (await anon.get("/agentpay/credit")).status_code == 404
    async with session_maker() as db:
        rows = (await db.execute(select(Ephemeral).where(
            Ephemeral.ns == agent_payments.RATE_NS))).scalars().all()
    assert rows == []


async def test_a_wallet_reads_its_credit_with_a_proof(wallet: AsyncClient, payments_on, chain):
    account = Account.create()
    assert (await _mpp_paid(wallet, chain, account)).status_code == 200
    challenge = await wallet.get("/agentpay/credit")
    assert challenge.status_code == 402 and challenge.json()["detail"]["reason"] == "proof_required"
    assert [c.request["amount"] for c in _mpp_challenges(challenge)] and all(
        c.request["amount"] == "0" for c in _mpp_challenges(challenge))
    proof = await _mpp_proof(challenge, account)
    r = await wallet.get("/agentpay/credit", headers={"Authorization": proof})
    assert r.status_code == 200, r.text
    view = r.json()
    assert (view["network"], view["address"]) == ("tempo:42431", account.address.lower())
    assert view["credit_micro"] == FLOOR - EP_MICRO
    assert [(p["protocol"], p["amount_micro"], p["status"]) for p in view["recent"]] == [
        ("mpp", FLOOR, "settled")]
    assert r.headers["cache-control"] == "no-store"
    again = await wallet.get("/agentpay/credit", headers={"Authorization": proof})
    assert again.status_code == 402 and again.json()["detail"]["reason"] == "payment_replayed"


async def test_an_unknown_wallet_reads_no_credit(wallet: AsyncClient, payments_on, chain):
    challenge = await wallet.get("/agentpay/credit")
    r = await wallet.get("/agentpay/credit",
                         headers={"Authorization": await _mpp_proof(challenge, Account.create())})
    assert r.status_code == 200 and (r.json()["credit_micro"], r.json()["recent"]) == (0, [])


async def test_the_credit_route_takes_no_payment(wallet: AsyncClient, payments_on, chain):
    challenge = await wallet.get("/agentpay/credit")
    payment = await wallet.get(f"/call/{EP}?aweme_id=7")
    r = await wallet.get("/agentpay/credit",
                         headers={"Authorization": _mpp_credential(payment, _signature())})
    assert r.status_code == 402 and challenge.status_code == 402
    assert chain.broadcasts == 0


async def test_expired_proof_claims_and_address_limits_are_swept(wallet: AsyncClient, payments_on,
                                                                 chain):
    from treg import ratestore
    from treg.models import Ephemeral

    async with session_maker() as db:
        for ns, k in ((agent_payments.PROOF_NS, "old-proof"), (agent_payments.RATE_NS, "ip:old")):
            await ratestore.kv_put(db, ns, k, {}, ttl_s=60)
            row = await db.get(Ephemeral, (ns, k))
            row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        await db.commit()
    challenge = await wallet.get("/agentpay/credit")
    r = await wallet.get("/agentpay/credit",
                         headers={"Authorization": await _mpp_proof(challenge, Account.create())})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        assert await db.get(Ephemeral, (agent_payments.PROOF_NS, "old-proof")) is None
    await wallet.get(f"/call/{EP}?aweme_id=7")
    async with session_maker() as db:
        assert await db.get(Ephemeral, (agent_payments.RATE_NS, "ip:old")) is None


async def test_a_payment_challenge_is_logged_as_a_payment_step(anon: AsyncClient, payments_on,
                                                                call_error_lines):
    r = await anon.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402
    (line,) = call_error_lines()
    assert (line["refused_by"], line["error_class"], line["caller_side"]) == (
        "payment", "payment_required", True)


def test_the_mpp_rail_starts_on_a_production_platform_with_treg_s_replay_store(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="no store configured"):
        MppTempoRail(secret="s" * 40, realm="registry.test", recipient=RECIPIENT, chain_id=42431)
    store = agent_payments.ReplayStore()
    rail = MppTempoRail(secret="s" * 40, realm="registry.test", recipient=RECIPIENT,
                        chain_id=42431, store=store)
    assert all(method._has_store() for method in rail._mpp.methods)


async def test_the_replay_store_claims_a_key_once(clients):
    store = agent_payments.ReplayStore()
    assert await store.put_if_absent("mpp:charge:0xabc", "0xabc") is True
    assert await store.put_if_absent("mpp:charge:0xabc", "0xabc") is False
    assert await store.get("mpp:charge:0xabc") == "0xabc"
    assert await store.put_if_absent_until("k2", "raw", time.time() + 60) is True
    assert await store.put_if_absent_until("k2", "other", time.time() + 60) is False
    await store.delete("mpp:charge:0xabc")
    assert await store.get("mpp:charge:0xabc") is None
    with pytest.raises(ValueError):
        await store.put_if_absent_until("k3", "raw", time.time() - 1)


# ---- x402 sign-in-with-x (SIWX) -----------------------------------------------------------------
def _siwx_extension(challenge_response: httpx.Response):
    from mpp.x402 import decode_payment_required
    from mpp.x402.siwx import from_payment_required

    return from_payment_required(decode_payment_required(challenge_response.headers["payment-required"]))


def _siwx_proof(challenge_response: httpx.Response, account, **override) -> str:
    """A SIWX proof signed the way pympp's SiwxTransport signs it."""
    from eth_account.messages import encode_defunct
    from mpp.x402 import siwx

    extension = _siwx_extension(challenge_response)
    unsigned = {**extension["info"], "address": account.address,
                "chainId": extension["supportedChains"][0]["chainId"], "type": "eip191", **override}
    signature = account.sign_message(encode_defunct(text=siwx.to_message(unsigned))).signature.hex()
    return siwx.encode({**unsigned, "signature": signature if signature.startswith("0x")
                        else "0x" + signature})


async def _x402_paid(client: AsyncClient, account) -> None:
    challenge = await client.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge, account)
    r = await client.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200, r.text


@pytest.fixture
async def siwx_agent(clients):
    """An agent on pympp's own SIWX client, calling treg's public origin."""
    from mpp.x402.siwx import SiwxTransport

    account = Account.create()
    transport = SiwxTransport(account=account, inner=ASGITransport(app=app))
    async with AsyncClient(transport=transport, base_url="https://registry.test") as c:
        c.account = account
        yield c


async def test_the_x402_402_offers_siwx_only_on_the_public_host(wallet: AsyncClient,
                                                               anon: AsyncClient, payments_on):
    extension = _siwx_extension(await wallet.get(f"/call/{EP}?aweme_id=7"))
    assert extension["info"]["domain"] == "registry.test"
    assert extension["info"]["uri"] == f"https://registry.test/call/{EP}?aweme_id=7"
    assert extension["supportedChains"] == [{"chainId": "eip155:84532", "type": "eip191"}]
    assert _siwx_extension(await anon.get(f"/call/{EP}?aweme_id=7")) is None


async def test_a_siwx_agent_with_credit_pays_nothing(siwx_agent: AsyncClient, payments_on,
                                                     facilitator):
    await _x402_paid(siwx_agent, siwx_agent.account)
    payer = siwx_agent.account.address.lower()
    balance, settles, verifies = await _balance(payer), facilitator.settles, facilitator.verifies
    r = await siwx_agent.get(f"/call/{EP}?aweme_id=7")   # SiwxTransport answers the 402 itself
    assert r.status_code == 200, r.text
    assert r.headers["X-Treg-Payment"] == "credit"
    assert "sign-in-with-x" not in {k.lower() for k in r.json()["headers"]}
    assert await _balance(payer) == balance - EP_MICRO
    assert (facilitator.settles, facilitator.verifies) == (settles, verifies)


async def test_a_new_wallet_proves_with_siwx_then_pays(siwx_agent: AsyncClient, payments_on,
                                                       facilitator):
    r = await siwx_agent.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "credit_insufficient"
    assert _siwx_extension(r) is None                  # the second 402 asks only for a payment
    credential, payer = _x402_credential(r, siwx_agent.account)
    paid = await siwx_agent.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert paid.status_code == 200, paid.text
    assert await _balance(payer) == FLOOR - EP_MICRO


async def test_a_siwx_proof_is_used_once_and_only_for_its_request(wallet: AsyncClient, payments_on,
                                                                  facilitator):
    account = Account.create()
    await _x402_paid(wallet, account)
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    proof = _siwx_proof(challenge, account)
    assert (await wallet.get(f"/call/{EP}?aweme_id=7",
                             headers={"SIGN-IN-WITH-X": proof})).status_code == 200
    again = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"SIGN-IN-WITH-X": proof})
    assert again.status_code == 402 and again.json()["detail"]["reason"] == "payment_replayed"
    other = await wallet.get(f"/call/{EP}?aweme_id=8",
                             headers={"SIGN-IN-WITH-X": _siwx_proof(challenge, account)})
    assert other.status_code == 402 and other.json()["detail"]["reason"] == "invalid_challenge"
    forged = _siwx_proof(challenge, account, address=Account.create().address)
    r = await wallet.get(f"/call/{EP}?aweme_id=7", headers={"SIGN-IN-WITH-X": forged})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "verification_failed"


async def test_a_tempo_credit_does_not_answer_a_siwx_proof(wallet: AsyncClient, payments_on, chain):
    account = Account.create()
    assert (await _mpp_paid(wallet, chain, account)).status_code == 200
    challenge = await wallet.get(f"/call/{EP}?aweme_id=7")
    r = await wallet.get(f"/call/{EP}?aweme_id=7",
                         headers={"SIGN-IN-WITH-X": _siwx_proof(challenge, account)})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "credit_insufficient"


async def test_a_wallet_polls_with_siwx(wallet: AsyncClient, payments_on, facilitator):
    account = Account.create()
    await _x402_paid(wallet, account)
    settles = facilitator.settles
    challenge = await wallet.get(f"/call/{POLL}?id=someone-elses-task")
    assert challenge.json()["detail"]["reason"] == "proof_required"
    assert _siwx_extension(challenge) is not None
    r = await wallet.get(f"/call/{POLL}?id=someone-elses-task",
                         headers={"SIGN-IN-WITH-X": _siwx_proof(challenge, account)})
    # The poll ran as the payer team, and treg's ownership check refused another team's task.
    assert r.status_code == 403 and r.json()["detail"]["error"] == "async_resource_not_owned"
    assert facilitator.settles == settles


async def test_a_wallet_reads_its_credit_with_siwx(wallet: AsyncClient, payments_on, facilitator):
    account = Account.create()
    await _x402_paid(wallet, account)
    challenge = await wallet.get("/agentpay/credit")
    assert challenge.status_code == 402 and _siwx_extension(challenge) is not None
    r = await wallet.get("/agentpay/credit",
                         headers={"SIGN-IN-WITH-X": _siwx_proof(challenge, account)})
    assert r.status_code == 200, r.text
    view = r.json()
    assert (view["network"], view["address"]) == ("eip155:84532", account.address.lower())
    assert view["credit_micro"] == await _balance(account.address.lower())
    payment, _ = _x402_credential(challenge, account)
    refused = await wallet.get("/agentpay/credit", headers={"PAYMENT-SIGNATURE": payment})
    assert refused.status_code == 402 and refused.json()["detail"]["reason"] == "proof_required"


# ---- Base finality and mainnet settings ---------------------------------------------------------
async def test_the_reconciler_reads_base_at_the_finalized_head(anon: AsyncClient, facilitator, evm):
    await _stuck(anon, facilitator)
    await agent_payments.reconcile()
    assert evm.tags and set(evm.tags) == {"finalized"}


def test_mainnet_x402_needs_its_own_rpc(monkeypatch):
    from treg.config import Settings

    _mainnet_x402(monkeypatch)
    monkeypatch.delenv("TREG_AGENT_PAYMENTS_X402_RPC_URL")
    with pytest.raises(ValueError, match="X402_RPC_URL"):
        Settings().check_agent_payments()


# ---- the Coinbase facilitator's API key -----------------------------------------------------------
COINBASE = "https://api.cdp.coinbase.com/platform/v2/x402"


async def _signed(secret: str, key, endpoint: str):
    import jwt

    rail = X402Rail(pay_to=RECIPIENT, network="eip155:8453", asset=X402_ASSET, asset_name="USD Coin",
                    asset_version="2", facilitator_url=COINBASE, max_timeout_s=120,
                    cdp_api_key_id="key-1", cdp_api_key_secret=secret)
    token = (await rail._facilitator._auth_headers(endpoint))["Authorization"].removeprefix("Bearer ")
    return (jwt.get_unverified_header(token),
            jwt.decode(token, key, algorithms=["EdDSA", "ES256"]))


async def test_an_ed25519_cdp_key_signs_each_facilitator_request():
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    secret = base64.b64encode(key.private_bytes_raw() + public).decode()
    header, claims = await _signed(secret, key.public_key(), "verify")
    assert header["kid"] == "key-1" and header["alg"] == "EdDSA" and header["nonce"]
    assert claims["uris"] == ["POST api.cdp.coinbase.com/platform/v2/x402/verify"]
    assert (claims["sub"], claims["iss"]) == ("key-1", "cdp")
    _, settle = await _signed(secret, key.public_key(), "settle")
    assert settle["uris"] == ["POST api.cdp.coinbase.com/platform/v2/x402/settle"]


async def test_a_legacy_ec_cdp_key_signs_with_es256():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat)

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()).decode()
    header, claims = await _signed(pem.replace("\n", "\\n"), key.public_key(), "verify")
    assert header["alg"] == "ES256" and claims["uris"][0].endswith("/x402/verify")


def test_a_cdp_key_needs_both_halves(monkeypatch):
    from treg.config import Settings

    monkeypatch.setenv("TREG_AGENT_PAYMENTS_X402_CDP_API_KEY_ID", "k")
    monkeypatch.delenv("TREG_AGENT_PAYMENTS_X402_CDP_API_KEY_SECRET", raising=False)
    with pytest.raises(ValueError, match="CDP_API_KEY"):
        Settings().check_agent_payments()


def _mainnet_x402(monkeypatch):
    for name, value in {
        "TREG_AGENT_PAYMENTS_ENABLED": "1", "TREG_AGENT_PAYMENTS_SECRET": "s" * 40,
        "TREG_AGENT_PAYMENTS_X402_ENABLED": "1", "TREG_AGENT_PAYMENTS_X402_PAY_TO": RECIPIENT,
        "TREG_AGENT_PAYMENTS_X402_NETWORK": "eip155:8453",
        "TREG_AGENT_PAYMENTS_X402_ASSET": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "TREG_AGENT_PAYMENTS_X402_ASSET_NAME": "USD Coin",
        "TREG_AGENT_PAYMENTS_X402_FACILITATOR_URL": COINBASE,
        "TREG_AGENT_PAYMENTS_X402_CDP_API_KEY_ID": "k",
        "TREG_AGENT_PAYMENTS_X402_CDP_API_KEY_SECRET": "s",
        "TREG_AGENT_PAYMENTS_X402_RPC_URL": "https://base.example",
    }.items():
        monkeypatch.setenv(name, value)


def test_a_complete_mainnet_x402_configuration_starts(monkeypatch):
    from treg.config import Settings

    _mainnet_x402(monkeypatch)
    Settings().check_agent_payments()


@pytest.mark.parametrize(("name", "value", "named"), [
    ("TREG_AGENT_PAYMENTS_X402_ASSET_NAME", "USDC", "X402_ASSET_NAME"),
    ("TREG_AGENT_PAYMENTS_X402_FACILITATOR_URL", "https://x402.org/facilitator",
     "X402_FACILITATOR_URL"),
    ("TREG_AGENT_PAYMENTS_X402_ASSET", X402_ASSET, "X402_ASSET="),
])
def test_a_wrong_mainnet_setting_is_named(monkeypatch, name, value, named):
    from treg.config import Settings

    _mainnet_x402(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=named):
        Settings().check_agent_payments()


def test_one_error_names_every_missing_setting(monkeypatch):
    from treg.config import Settings

    monkeypatch.setenv("TREG_AGENT_PAYMENTS_ENABLED", "1")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MPP_ENABLED", "1")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_X402_ENABLED", "1")
    for name in ("SECRET", "TEMPO_RECIPIENT", "X402_PAY_TO"):
        monkeypatch.delenv(f"TREG_AGENT_PAYMENTS_{name}", raising=False)
    with pytest.raises(ValueError) as error:
        Settings().check_agent_payments()
    for name in ("SECRET", "TEMPO_RECIPIENT", "X402_PAY_TO"):
        assert f"TREG_AGENT_PAYMENTS_{name}" in str(error.value)


def test_mainnet_and_testnet_rails_never_mix(monkeypatch):
    from treg.config import Settings

    _mainnet_x402(monkeypatch)
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MPP_ENABLED", "1")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TEMPO_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TEMPO_CHAIN_ID", "42431")
    with pytest.raises(ValueError, match="both mainnet or both testnet"):
        Settings().check_agent_payments()
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TEMPO_CHAIN_ID", "4217")
    Settings().check_agent_payments()


# ---- settlement to Stripe ------------------------------------------------------------------------
@pytest.fixture
def stripe_mode(monkeypatch, payments_on):
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_SETTLEMENT", "stripe")
    monkeypatch.setenv("TREG_STRIPE_SECRET_KEY", "sk_test_agentpay")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TEMPO_CURRENCY", "0x" + "20" * 20)
    get_settings.cache_clear()
    recorded: list[dict] = []

    async def _record(**kwargs):
        recorded.append(kwargs)
        return {"id": f"pi_{len(recorded)}"}

    monkeypatch.setattr(agent_payments.stripe_api, "record_crypto_payment", _record)
    yield recorded
    get_settings.cache_clear()


async def test_settling_to_stripe_charges_whole_cents(monkeypatch, stripe_mode):
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MIN_PAYMENT_MICRO", "12345")
    get_settings.cache_clear()
    paid = await agent_payments.quote(rest=EP, method="GET", path=f"/call/{EP}",
                                      query="aweme_id=7", query_items=(("aweme_id", "7"),), body=b"")
    assert paid.offer.amount_micro == 20_000


async def test_each_payment_settled_to_stripe_is_recorded_once(anon: AsyncClient, stripe_mode):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert r.status_code == 200, r.text
    reference = r.headers["X-Treg-Payment-Reference"]
    assert (await agent_payments.reconcile()).recorded == 1
    row = await _row()
    assert stripe_mode == [{"amount_cents": FLOOR // 10_000, "network": "base",
                            "transaction_hash": reference, "api_key": "sk_test_agentpay",
                            "metadata": {"treg_agent_payment_id": str(row.id),
                                         "treg_team_id": str(row.org_id),
                                         "payer_address": row.payer_address, "protocol": "x402",
                                         "network": row.network}}]
    assert (await _row()).stripe_payment_intent == "pi_1"
    assert (await agent_payments.reconcile()).recorded == 0


async def test_a_payment_settled_to_an_earlier_wallet_is_not_stripes(anon: AsyncClient,
                                                                    stripe_mode):
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    async with session_maker() as db:
        row = (await db.execute(select(AgentPayment))).scalar_one()
        row.pay_to = "0x" + "77" * 20
        db.add(row)
        await db.commit()
    assert (await agent_payments.reconcile()).recorded == 0 and stripe_mode == []


async def test_payments_to_earlier_wallets_never_block_stripe_recording(anon: AsyncClient,
                                                                        stripe_mode):
    for _ in range(3):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        credential, _ = _x402_credential(challenge)
        await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    async with session_maker() as db:
        rows = (await db.execute(select(AgentPayment).order_by(AgentPayment.id))).scalars().all()
        for row in rows[:2]:  # the two oldest went to an address treg no longer offers
            row.pay_to = "0x" + "77" * 20
            db.add(row)
        await db.commit()
        newest = rows[2].id
    assert (await agent_payments.reconcile(limit=2)).recorded == 1
    assert [r["metadata"]["treg_agent_payment_id"] for r in stripe_mode] == [str(newest)]


async def test_stripe_settlement_can_use_its_own_stripe_account(anon: AsyncClient, monkeypatch,
                                                                stripe_mode):
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_STRIPE_SECRET_KEY", "sk_test_123")
    get_settings.cache_clear()
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, _ = _x402_credential(challenge)
    await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
    assert (await agent_payments.reconcile()).recorded == 1
    assert stripe_mode[0]["api_key"] == "sk_test_123"


def test_settling_to_stripe_needs_a_stripe_key(monkeypatch):
    from treg.config import Settings

    monkeypatch.setenv("TREG_AGENT_PAYMENTS_ENABLED", "1")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_SECRET", "s" * 40)
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_SETTLEMENT", "stripe")
    for name in ("TREG_AGENT_PAYMENTS_STRIPE_SECRET_KEY", "TREG_STRIPE_SECRET_KEY"):
        monkeypatch.setenv(name, "")  # empty, not unset: an unset name still reads `.env`
    with pytest.raises(ValueError, match="TREG_STRIPE_SECRET_KEY"):
        Settings().check_agent_payments()


async def test_a_tempo_rpc_key_goes_with_every_rpc_request(monkeypatch):
    from mpp.methods.tempo import server as tempo_server

    from treg.infra.agent_payments import tempo as tempo_rail

    charge, given = tempo_server.charge, {}

    def spy(**kwargs):
        given.update(kwargs)
        return charge(**kwargs)

    monkeypatch.setattr(tempo_server, "charge", spy)
    sent: list[httpx.Request] = []

    def node(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"number": "0x1", "timestamp": "0x1"}})

    class Node(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(node), **kwargs)

    monkeypatch.setattr(tempo_rail, "httpx", SimpleNamespace(AsyncClient=Node))
    rail = MppTempoRail(secret="s" * 40, realm="registry.test", recipient=RECIPIENT,
                        chain_id=42431, rpc_url="https://rpc.example/testnet",
                        rpc_api_key="tempo_sandbox:sk:k")
    assert given["http_client"].headers["tempo-api-key"] == "tempo_sandbox:sk:k"
    await rail._chain.head()
    assert sent[0].headers["tempo-api-key"] == "tempo_sandbox:sk:k"
    assert str(sent[0].url) == "https://rpc.example/testnet"

    MppTempoRail(secret="s" * 40, realm="registry.test", recipient=RECIPIENT, chain_id=42431)
    assert "tempo-api-key" not in given["http_client"].headers


def test_a_tempo_rpc_key_needs_its_rpc_url(monkeypatch):
    from treg.config import Settings

    monkeypatch.setenv("TREG_AGENT_PAYMENTS_ENABLED", "1")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_SECRET", "s" * 40)
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MPP_ENABLED", "1")
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TEMPO_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TEMPO_RPC_API_KEY", "tempo_sandbox:sk:k")
    monkeypatch.delenv("TREG_AGENT_PAYMENTS_TEMPO_RPC_URL", raising=False)
    with pytest.raises(ValueError, match="TREG_AGENT_PAYMENTS_TEMPO_RPC_URL"):
        Settings().check_agent_payments()
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_TEMPO_RPC_URL", "https://rpc.example/testnet")
    Settings().check_agent_payments()


async def test_the_chain_search_stays_near_the_head_and_asks_for_logs_in_spans():
    from treg.infra.agent_payments import evm as evm_module

    class Pruned(Chain):
        """10,000,000 blocks two seconds apart; nothing below block 9,990,000 is kept."""

        def __init__(self):
            super().__init__("http://chain.invalid")
            self.ranges: list[tuple[int, int]] = []

        async def call(self, method, params):
            if method == "eth_getBlockByNumber":
                n = 10_000_000 if params[0] == "latest" else int(params[0], 16)
                if n < 9_990_000:
                    raise evm_module.ChainUnavailable("pruned history unavailable")
                return {"number": hex(n), "timestamp": hex(1_700_000_000 + 2 * n)}
            assert method == "eth_getLogs"
            start, end = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            assert end - start < evm_module.LOG_SPAN
            self.ranges.append((start, end))
            return [{"transactionHash": "0xhit"}] if start <= 9_999_500 <= end else []

    chain = Pruned()
    when = datetime.fromtimestamp(1_700_000_000 + 2 * 9_999_000 + 1, UTC).replace(tzinfo=None)
    assert await chain.block_at(when) == 9_999_000
    logs = await chain.logs(address="0x", topics=[], from_block=9_999_000, to_block=9_999_900)
    assert logs == [{"transactionHash": "0xhit"}] and len(chain.ranges) == 3


# ---- what a check costs the global cap ----------------------------------------------------------
async def test_credentials_refused_locally_do_not_spend_the_check_cap(anon: AsyncClient,
                                                                       payments_on, facilitator,
                                                                       monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MAX_VERIFICATIONS_MINUTE", "1")
    get_settings.cache_clear()
    junk = [{"SIGN-IN-WITH-X": "x"}, {"PAYMENT-SIGNATURE": "x"}, {"Authorization": "Payment x"}]
    for i, headers in enumerate(junk):
        r = await anon.get(f"/call/{EP}?aweme_id=7",
                           headers={**headers, "X-Forwarded-For": f"203.0.113.{i + 1}"})
        assert r.status_code == 402, r.text
    for expected in (200, 429):   # the one slot goes to the first check that reaches the facilitator
        credential, _ = _x402_credential(await anon.get(f"/call/{EP}?aweme_id=7"))
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
        assert r.status_code == expected, r.text
    assert facilitator.verifies == 1


async def test_a_payment_checked_locally_still_spends_the_check_cap(anon: AsyncClient,
                                                                    payments_on, chain,
                                                                    monkeypatch):
    """An MPP pull transfer is checked without the chain, but settling it is a broadcast."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(200))
    monkeypatch.setenv("TREG_AGENT_PAYMENTS_MAX_VERIFICATIONS_MINUTE", "1")
    get_settings.cache_clear()
    for i, expected in enumerate((200, 429)):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={
            "Authorization": _mpp_credential(challenge, "0x76" + f"{i + 1}{i + 1}" * 40),
            "X-Forwarded-For": f"203.0.113.{i + 1}"})
        assert r.status_code == expected, r.text
    assert chain.broadcasts == 1


async def test_an_mpp_check_spends_the_cap_once_at_its_first_rpc(monkeypatch):
    import json

    from mpp import Challenge, Credential

    from treg.infra.agent_payments import Offer, PaymentRejected
    from treg.infra.agent_payments import tempo as tempo_rail

    rpc: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        rpc.append(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": hex(42431) if method == "eth_chainId" else None})

    # The node is handed over at construction: httpx routes requests through the environment's proxy
    # (on macOS, the system proxy) and past a `_transport` swapped in afterwards.
    class Node(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(tempo_rail, "httpx", SimpleNamespace(AsyncClient=Node))
    rail = MppTempoRail(secret="s" * 40, realm="registry.test", recipient=RECIPIENT,
                        chain_id=42431, rpc_url="http://rpc.invalid")

    def offer(path: str) -> Offer:
        return Offer(amount_micro=FLOOR, method="GET", path=path, query="",
                     resource_url=f"https://registry.test{path}", description="x",
                     host="registry.test")

    async def credential(path: str) -> str:
        challenge = Challenge.from_www_authenticate((await rail.challenge(offer(path)))[0][1])
        return Credential(challenge=challenge.to_echo(),
                          payload={"type": "hash", "hash": "0x" + "ab" * 32}).to_authorization()

    spent: list[int] = []

    async def hook() -> None:
        spent.append(1)

    with pytest.raises(PaymentRejected):   # issued for another route: refused before any RPC
        await rail.verify(await credential("/call/other"), offer("/call/x"), before_remote=hook)
    assert (spent, rpc) == ([], [])
    with pytest.raises(PaymentRejected):   # reaches the chain: one slot, however many RPCs
        await rail.verify(await credential("/call/x"), offer("/call/x"), before_remote=hook)
    assert spent == [1] and rpc

    class CapReached(Exception):
        pass

    async def full() -> None:
        raise CapReached

    calls = len(rpc)
    with pytest.raises(CapReached):        # the cap's refusal is what the caller sees
        await rail.verify(await credential("/call/x"), offer("/call/x"), before_remote=full)
    assert len(rpc) == calls
