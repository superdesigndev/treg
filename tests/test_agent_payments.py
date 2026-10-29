"""Agent payments: a caller with no treg credential pays a catalog call per call with MPP (Tempo
charge) or x402 (`exact`). docs/context/architecture/agent-payments.md.

The rails are the real adapters. Only the chain is replaced: the Tempo charge intent's
`validate`/`broadcast` (so the HMAC challenge, expiry and route binding are pympp's own), and the
x402 facilitator (so the EIP-3009 payload is a real signature made offline).
"""

from __future__ import annotations

import asyncio

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
from treg.infra.agent_payments.tempo import MppTempoRail
from treg.infra.agent_payments.x402 import X402Rail
from treg.infra.db import session_maker
from treg.models import AgentPayer, AgentPayment, Hold, LedgerEntry

EP = "tikhub.tiktok.video.comments"   # $0.001 per call on treg's key
EP_MICRO = 1_000
FLOOR = 50_000
RECIPIENT = "0x742d35Cc6634c0532925a3b844bC9e7595F8fE00"
TEMPO_PAYER = "0x" + "ab" * 20
X402_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class FakeTempoChain:
    """Stands in for the Tempo RPC behind pympp's charge intent."""

    def __init__(self) -> None:
        self.broadcasts = 0
        self.fail = False

    async def validate(self, intent, credential, request):
        from mpp._validation import Validation

        return Validation(credential=credential, details={"mode": "pull"}, intent="charge",
                          request=dict(request), payer=TEMPO_PAYER)

    async def broadcast(self, intent, credential, request):
        from mpp import Receipt
        from mpp.errors import VerificationFailedError

        if self.fail:
            raise VerificationFailedError("transaction reverted")
        self.broadcasts += 1
        return Receipt.success(reference=f"0x{self.broadcasts:064x}")


class FakeFacilitator:
    def __init__(self) -> None:
        self.settles = 0
        self.fail = False
        self.unreachable = False
        self.gate: asyncio.Event | None = None

    async def verify(self, payload, requirements):
        from x402.schemas import VerifyResponse

        return VerifyResponse(is_valid=True, payer=payload.payload["authorization"]["from"])

    async def settle(self, payload, requirements):
        from x402.schemas import SettleResponse

        if self.gate is not None:
            await self.gate.wait()
        if self.unreachable:
            raise httpx.ReadTimeout("facilitator timed out after submitting")
        if self.fail:
            return SettleResponse(success=False, error_reason="insufficient_funds", transaction="",
                                  network=requirements.network)
        self.settles += 1
        return SettleResponse(success=True, transaction=f"0x{self.settles:064x}",
                              network=requirements.network,
                              payer=payload.payload["authorization"]["from"])


@pytest.fixture
def chain(monkeypatch):
    from mpp.methods.tempo.intents import ChargeIntent

    fake = FakeTempoChain()
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
        "TREG_PLATFORM_PROVIDERS": "tikhub",
        "TREG_AGENT_PAYMENTS_ENABLED": "1",
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
                 max_timeout_s=120, facilitator=facilitator),
    ])
    yield
    agent_payments.configure([])
    get_settings.cache_clear()


@pytest.fixture
async def anon(clients):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c


def _mpp_credential(challenge_response: httpx.Response, signature: str = "0x76" + "11" * 40) -> str:
    from mpp import Challenge, Credential

    header = challenge_response.headers.get_list("www-authenticate")[0]
    challenge = Challenge.from_www_authenticate(header)
    return Credential(challenge=challenge.to_echo(),
                      payload={"type": "transaction", "signature": signature}).to_authorization()


def _x402_credential(challenge_response: httpx.Response, account=None) -> tuple[str, str]:
    from x402.http import decode_payment_required_header, encode_payment_signature_header
    from x402.mechanisms.evm.exact.client import ExactEvmScheme
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.schemas import PaymentPayload

    account = account or Account.create()
    required = decode_payment_required_header(challenge_response.headers["payment-required"])
    accepted = required.accepts[0]
    signed = ExactEvmScheme(EthAccountSigner(account)).create_payment_payload(accepted)
    payload = PaymentPayload(x402_version=2, payload=signed, accepted=accepted,
                             resource=required.resource)
    return encode_payment_signature_header(payload), account.address.lower()


async def _payer_org(address: str) -> int:
    async with session_maker() as db:
        return (await db.get(AgentPayer, address)).org_id


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
    from x402.http import decode_payment_required_header

    r = await anon.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402, r.text
    assert r.headers["X-Treg-Error"] == "1" and r.headers["cache-control"] == "no-store"
    detail = r.json()["detail"]
    assert detail["error"] == "payment_required"
    assert detail["amount_micro"] == FLOOR and detail["estimated_cost_micro"] == EP_MICRO
    assert all(v.startswith("Payment ") for v in r.headers.get_list("www-authenticate"))
    required = decode_payment_required_header(r.headers["payment-required"])
    assert required.accepts[0].amount == str(FLOOR)
    assert required.resource.url == f"https://registry.test/call/{EP}?aweme_id=7"


async def test_free_own_and_passthrough_calls_get_no_challenge(anon: AsyncClient, payments_on):
    free = next(ep["id"] for ep in catalog_store.load().by_id.values()
                if (ep.get("cost") or {}).get("type") == "free")
    for path in (f"/call/{free}", "/call/echo/anything", "/call/https://api.tikhub.io/x"):
        r = await anon.get(path)
        assert r.status_code == 401, (path, r.text)
        assert "payment-required" not in r.headers and "www-authenticate" not in r.headers


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
    assert r.headers["X-Treg-Payment"] == "settled" and "payment-receipt" in r.headers
    assert int(r.headers["X-Treg-Cost-Micro"]) == EP_MICRO
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
    assert r.headers["X-Treg-Payment"] == "settled" and "payment-response" in r.headers
    assert facilitator.settles == 1
    assert await _balance(payer) == FLOOR - EP_MICRO


async def test_credit_pays_the_next_call_without_settling_again(anon: AsyncClient, payments_on,
                                                                facilitator):
    account = Account.create()
    for expected_settles, mode in ((1, "settled"), (1, "credit"), (1, "credit")):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        credential, payer = _x402_credential(challenge, account)
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"PAYMENT-SIGNATURE": credential})
        assert r.status_code == 200, r.text
        assert r.headers["X-Treg-Payment"] == mode
        assert facilitator.settles == expected_settles
    assert "payment-response" not in r.headers  # nothing was settled for a credit draw
    assert await _balance(payer) == FLOOR - 3 * EP_MICRO


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
    assert r.json()["detail"]["reason"] in {"unsupported_version", "malformed_credential"}


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
    assert r.headers["X-Treg-Payment"] == "settled" and r.headers["X-Treg-Payment-Reference"]
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


# ---- 402s that are not payment challenges ---------------------------------------------------------
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
    assert r.headers["X-Treg-Payment"] == "settled" and "payment-response" not in r.headers


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
        Settings()


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


def test_only_a_recovered_tempo_signature_may_draw_credit():
    from treg.infra.agent_payments.tempo import _recovered_sender

    assert _recovered_sender(_tempo_tx(b"\x01" * 65))
    # A keychain signature only NAMES its account (0x03 | address | inner signature); the chain
    # authenticates it at settlement, so before settlement it proves nothing.
    assert not _recovered_sender(_tempo_tx(b"\x03" + bytes.fromhex("ab" * 20) + b"\x02" * 65))
    assert not _recovered_sender("0x76zz")


async def test_an_unrecovered_mpp_signature_never_draws_credit(anon: AsyncClient, payments_on,
                                                              chain):
    for n in (1, 2):
        challenge = await anon.get(f"/call/{EP}?aweme_id=7")
        keychain = b"\x03" + bytes.fromhex(TEMPO_PAYER[2:]) + bytes([n]) * 65
        r = await anon.get(f"/call/{EP}?aweme_id=7", headers={
            "Authorization": _mpp_credential(challenge, _tempo_tx(keychain))})
        assert r.status_code == 200, r.text
        assert r.headers["X-Treg-Payment"] == "settled"
    assert chain.broadcasts == 2  # the wallet held credit, but a named signer must settle


async def test_one_tempo_transfer_is_one_proof_in_either_mode(anon: AsyncClient, payments_on, chain):
    from mpp import Challenge, Credential

    from treg.infra.agent_payments.tempo import _transaction_hash

    signed = _tempo_tx(b"\x01" * 65)
    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    r = await anon.get(f"/call/{EP}?aweme_id=7",
                       headers={"Authorization": _mpp_credential(challenge, signed)})
    assert r.status_code == 200, r.text
    echo = Challenge.from_www_authenticate(challenge.headers.get_list("www-authenticate")[0]).to_echo()
    pushed = Credential(challenge=echo, payload={"type": "hash", "hash": _transaction_hash(signed)})
    r = await anon.get(f"/call/{EP}?aweme_id=7", headers={"Authorization": pushed.to_authorization()})
    assert r.status_code == 402 and r.json()["detail"]["reason"] == "payment_replayed"
    assert chain.broadcasts == 1


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
    from x402.http import decode_payment_signature_header

    challenge = await anon.get(f"/call/{EP}?aweme_id=7")
    credential, payer = _x402_credential(challenge)
    assert decode_payment_signature_header(credential)
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
