"""Pay per call: a /call/ with no treg token pays its own price over x402 or MPP.

The payment rail is faked here (its validate, claim and take); pympp's own behaviour and the real
chains are exercised by the end-to-end run in docs/context/architecture/pay-per-call.md. These tests
pin what treg decides: which calls are offered, that the payment is taken only after a billable
answer, that the team's balance is topped back up exactly once, and that the payer's credential and
treg's controls never reach the provider.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from mpp import Receipt
from mpp.server import Response as PayResponse
from sqlmodel import select

from treg import api as A
from treg.application import pay_per_call
from treg.config import Settings, get_settings
from treg.infra import payments as payment_rail
from treg.infra.db import session_maker
from treg.models import CreditBlock, Org

from conftest import funded_user

EP = "trestleiq.people.phone.verify"   # GET, per_call $0.015, every 200 billable, 4xx/5xx free
PARAMS = {"phone": "+13005550103"}
PRICE = 15_000
RECIPIENT = "0x" + "11" * 20
SECRET = "s" * 32


class _Pending:
    def __init__(self, rail):
        self.rail = rail

    async def take(self):
        self.rail.taken += 1
        if self.rail.fail_take:
            raise payment_rail.PaymentFailed("insufficient funds at broadcast")
        return Receipt(status="success", timestamp=datetime.now(timezone.utc),
                       reference=f"0xtx{self.rail.taken}", method="tempo", payer="0xpayer")


class _Validated(payment_rail.Validated):
    @property
    def claim_key(self) -> str:
        return self.verified

    def receipt_headers(self, receipt):
        return {"Payment-Receipt": f"receipt-{receipt.reference}"}


class FakeRail:
    def __init__(self):
        self.taken = 0
        self.fail_take = False
        self.claimed: set[str] = set()
        self.requests = []

    async def validate(self, request, *, price_micro, description, meta):
        self.requests.append((request, price_micro, description, meta))
        credential = request.headers.get("authorization", "")
        if credential.startswith("Payment good"):
            return _Validated(verified=credential, pending=_Pending(self))
        return payment_rail.PaymentRequired(PayResponse(
            status=402, headers={"WWW-Authenticate": 'Payment id="c1"', "PAYMENT-REQUIRED": "e30"},
            body=b'{"type":"payment-required"}'))

    async def claim(self, validated):
        if validated.claim_key in self.claimed:
            return False
        self.claimed.add(validated.claim_key)
        return True


@pytest.fixture
async def paid(clients, monkeypatch):
    """Pay per call on, with a funded pay-per-call team and a fake rail; returns an anonymous
    client, the rail and the team's org id."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_TRESTLEIQ", "PLATFORM-TRESTLEIQ")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "trestleiq")
    get_settings.cache_clear()
    team = await funded_user(clients, "pay-per-call@superdesign.dev", micro=1_000_000)
    settings = get_settings()
    for name, value in {"mpp_enabled": True, "pay_per_call_team_token": team["token"],
                        "payments_secret_key": SECRET, "payments_realm": "registry",
                        "public_url": "https://treg.example"}.items():
        monkeypatch.setattr(settings, name, value)
    rail = FakeRail()
    monkeypatch.setattr(payment_rail, "payments", lambda: rail)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app),
                                 base_url="http://registry") as anon:
        yield anon, rail, team["org_id"]
    get_settings.cache_clear()


async def _balance(org_id: int) -> int:
    async with session_maker() as db:
        return (await db.get(Org, org_id)).balance_micro


async def _payment_blocks(org_id: int) -> list[str]:
    async with session_maker() as db:
        rows = (await db.execute(select(CreditBlock.stripe_payment_intent).where(
            CreditBlock.org_id == org_id, CreditBlock.kind == "purchased"))).scalars().all()
    return list(rows)


class _Body(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        yield self.body


def _upstream(status: int):
    """Replace the provider with one answering `status`, recording what it was sent."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers={"content-type": "application/json"},
                              stream=_Body(b'{"ok": true}' if status < 400 else b'{"ok": false}'))

    A.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


async def test_unpaid_call_gets_the_402_for_the_tools_price(paid):
    anon, rail, _ = paid
    response = await anon.get(f"/call/{EP}", params=PARAMS)
    assert response.status_code == 402
    assert response.headers["www-authenticate"].startswith("Payment ")
    assert response.headers["payment-required"] == "e30"
    request, price, description, meta = rail.requests[0]
    assert price == PRICE and description == EP and meta == {"endpoint": EP}
    # The resource URL is the public one, path and query as the caller sent them.
    assert request.url == f"https://treg.example/call/{EP}?phone=%2B13005550103"
    assert rail.taken == 0


async def test_billable_answer_takes_the_payment_and_tops_the_team_back_up(paid):
    anon, rail, org_id = paid
    before = await _balance(org_id)
    seen = _upstream(200)
    response = await anon.get(f"/call/{EP}", params=PARAMS, headers={
        "Authorization": "Payment good-1", "PAYMENT-SIGNATURE": "x402-sig",
        "Payment-Authorization": "Payment other", "Idempotency-Key": "shared-label",
        "X-Treg-Meta": '{"tags":{"customer":"spoof"}}', "X-Custom": "kept"})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert rail.taken == 1
    assert response.headers["x-treg-payment"] == "charged"
    assert response.headers["x-treg-paid-micro"] == str(PRICE)
    assert response.headers["payment-receipt"] == "receipt-0xtx1"
    assert response.headers["x-treg-cost-micro"] == str(PRICE)
    # The provider saw neither the payment nor treg's controls; other caller headers relay.
    sent = seen[0].headers
    for name in ("payment-signature", "payment-authorization", "idempotency-key", "x-treg-meta"):
        assert name not in sent
    assert sent.get("authorization") != "Payment good-1"
    assert sent["x-api-key"] == "PLATFORM-TRESTLEIQ"
    assert sent["x-custom"] == "kept"
    assert await _balance(org_id) == before
    assert await _payment_blocks(org_id) == ["tempo:0xtx1"]


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_non_billable_answer_leaves_the_payment_untaken(paid, status):
    anon, rail, org_id = paid
    before = await _balance(org_id)
    _upstream(status)
    response = await anon.get(f"/call/{EP}", params=PARAMS,
                              headers={"Authorization": "Payment good-2"})
    assert response.status_code == status
    assert response.headers["x-treg-payment"] == "not-charged"
    assert rail.taken == 0
    assert await _balance(org_id) == before
    assert await _payment_blocks(org_id) == []


async def test_a_credential_pays_for_one_call_only(paid):
    anon, rail, _ = paid
    _upstream(500)
    first = await anon.get(f"/call/{EP}", params=PARAMS, headers={"Authorization": "Payment good-3"})
    assert first.status_code == 500
    # Burned even though the first call was free: it cannot buy unlimited failing calls.
    again = await anon.get(f"/call/{EP}", params=PARAMS, headers={"Authorization": "Payment good-3"})
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "payment_already_used"
    assert again.headers["x-treg-payment"] == "not-charged"
    assert rail.taken == 0


async def test_failed_payment_withholds_the_answer_and_asks_again(paid):
    anon, rail, org_id = paid
    rail.fail_take = True
    before = await _balance(org_id)
    _upstream(200)
    response = await anon.get(f"/call/{EP}", params=PARAMS,
                              headers={"Authorization": "Payment good-4"})
    assert response.status_code == 402
    assert response.headers["www-authenticate"].startswith("Payment ")
    assert "ok" not in response.text
    assert rail.taken == 1
    # treg settled the provider's charge on the team and was not paid back.
    assert await _balance(org_id) == before - PRICE
    assert await _payment_blocks(org_id) == []


async def test_empty_team_balance_is_a_503_not_a_payment_challenge(paid):
    anon, rail, org_id = paid
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.balance_micro = 0
        for block in (await db.execute(select(CreditBlock).where(CreditBlock.org_id == org_id))).scalars():
            block.remaining_micro = 0
        await db.commit()
    _upstream(200)
    response = await anon.get(f"/call/{EP}", params=PARAMS,
                              headers={"Authorization": "Payment good-5"})
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "pay_per_call_unavailable"
    assert "balance" not in response.text
    assert response.headers["x-treg-payment"] == "not-charged"
    assert rail.taken == 0


@pytest.mark.parametrize(("method", "rest", "reason"), [
    ("GET", "keenable.web.search", "method_not_allowed"),       # a POST tool called with GET
    ("GET", "keenable.web.fetch", "below_minimum"),             # $0.004
    ("POST", "companyenrich.people.search", "variable_price"),  # priced per person returned
    ("GET", "not.a.tool", "not_a_catalog_tool"),
    ("GET", "my-own-tool", "not_a_catalog_tool"),
])
async def test_calls_that_cannot_be_paid_per_call_ask_to_sign_in(paid, method, rest, reason):
    anon, rail, _ = paid
    response = await anon.request(method, f"/call/{rest}", params={"url": "https://example.com"})
    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == reason
    assert rail.requests == []


async def test_signed_in_members_never_meet_the_payment_path(paid, clients):
    _, rail, _ = paid
    _upstream(200)
    response = await clients.get(f"/call/{EP}", params=PARAMS)
    assert response.status_code == 200
    assert "x-treg-payment" not in response.headers
    assert rail.requests == []


async def test_without_pay_per_call_an_anonymous_call_is_still_401(clients):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app),
                                 base_url="http://registry") as anon:
        response = await anon.get(f"/call/{EP}", params=PARAMS)
    assert response.status_code == 401
    assert response.json()["detail"] == "not authenticated"


def test_cdp_key_selects_coinbases_facilitator(monkeypatch):
    from mpp.x402.cdp import COINBASE_FACILITATOR_URL

    settings = get_settings()
    for name, value in {**SHARED, "x402_enabled": True, "x402_recipient": RECIPIENT,
                        "x402_cdp_api_key_id": "kid", "x402_cdp_api_key_secret": "secret"}.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "x402_facilitator_url", "")
    rail = payment_rail._build()
    [method] = rail._server.methods
    assert method.x402.facilitator.url == COINBASE_FACILITATOR_URL


def test_offer_prices_with_the_margin_reserve_uses(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_margin", 0.2)
    assert pay_per_call.offer_for(EP, "GET").price_micro == 18_000
    assert pay_per_call.offer_for("trestleiq.people.address.verify", "GET").price_micro == 12_000


def test_floor_is_inclusive_at_one_cent():
    assert pay_per_call.offer_for("trestleiq.people.address.verify", "GET").price_micro == 10_000


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


SHARED = {"pay_per_call_team_token": "t", "payments_secret_key": SECRET,
          "payments_realm": "treg.to", "kv_url": "redis://kv:6379"}
X402 = {"x402_enabled": True, "x402_recipient": RECIPIENT,
        "x402_facilitator_url": "https://facilitator.example"}
MPP = {"mpp_enabled": True, "mpp_tempo_recipient": RECIPIENT}


def test_settings_read_from_the_environment(monkeypatch):
    for name, value in {**SHARED, **X402, **MPP, "mpp_tempo_chain_id": 42431,
                        "x402_network": "base-sepolia"}.items():
        monkeypatch.setenv(f"TREG_{name.upper()}", str(value).lower() if value is True else str(value))
    settings = Settings(_env_file=None)
    assert settings.mpp_tempo_chain_id == 42431 and settings.x402_enabled and settings.mpp_enabled


def test_each_protocol_checks_only_its_own_settings():
    assert _settings(**SHARED, **X402).x402_enabled
    assert _settings(**SHARED, **MPP).mpp_enabled
    assert _settings(**SHARED, **X402, **MPP).mpp_enabled
    assert not _settings().x402_enabled   # off by default, nothing required
    # Coinbase's facilitator needs only its API key.
    cdp = {"x402_enabled": True, "x402_recipient": RECIPIENT, "x402_cdp_api_key_id": "kid",
           "x402_cdp_api_key_secret": "secret"}
    assert _settings(**SHARED, **cdp).x402_enabled
    # A facilitator on this machine may speak plain HTTP; any other must not.
    assert _settings(**SHARED, **{**X402, "x402_facilitator_url": "http://127.0.0.1:4020"}).x402_enabled


@pytest.mark.parametrize(("values", "message"), [
    ({**SHARED, "x402_enabled": True}, "TREG_X402_ENABLED requires TREG_X402_RECIPIENT"),
    ({**SHARED, "x402_enabled": True, "x402_recipient": RECIPIENT},
     "requires TREG_X402_FACILITATOR_URL or TREG_X402_CDP_API_KEY_ID"),
    ({**SHARED, **X402, "x402_cdp_api_key_id": "kid", "x402_cdp_api_key_secret": "s"},
     "not both"),
    ({**SHARED, "x402_enabled": True, "x402_recipient": RECIPIENT, "x402_cdp_api_key_id": "kid"},
     "are set together"),
    ({**SHARED, "mpp_enabled": True}, "TREG_MPP_ENABLED requires TREG_MPP_TEMPO_RECIPIENT"),
    ({**X402}, "TREG_X402_ENABLED requires TREG_PAY_PER_CALL_TEAM_TOKEN, "
               "TREG_PAYMENTS_SECRET_KEY, TREG_PAYMENTS_REALM, TREG_KV_URL"),
    ({**MPP}, "TREG_MPP_ENABLED requires TREG_PAY_PER_CALL_TEAM_TOKEN"),
    ({**SHARED, **MPP, "payments_secret_key": "short"}, "at least 32 bytes"),
    ({**SHARED, **X402, "x402_recipient": "0x123"}, "TREG_X402_RECIPIENT must be"),
    ({**SHARED, **X402, "x402_facilitator_url": "http://facilitator.example"}, "https URL"),
    ({**SHARED, **MPP, "mpp_tempo_currencies": "0x20c0000000000000000000006a37DA5C996874BE,nope"},
     "invalid address: nope"),
    ({**SHARED, **MPP, "mpp_tempo_fee_payer_key": "0xabc"}, "TREG_MPP_TEMPO_FEE_PAYER_KEY"),
    ({**SHARED, **X402, **MPP, "x402_network": "base-sepolia"}, "mix a mainnet and a testnet"),
    ({**SHARED, **MPP, "mpp_tempo_chain_id": 1}, r"4217 \(mainnet\) or 42431"),
    ({**SHARED, **MPP, "pay_per_call_min_price_micro": 9_999}, "greater than or equal to 10000"),
])
def test_misconfiguration_names_every_missing_or_bad_setting(values, message):
    with pytest.raises(ValueError, match=message.replace("$", r"\$")):
        _settings(**values)
