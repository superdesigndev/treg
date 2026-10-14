"""The complete HTTP-only user journey from signup through a paid recovery."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from httpx import AsyncClient

from treg import audit
from treg.config import get_settings
from treg.domain.money import with_margin

from test_marketplace_call import EP, EP_DFS, EP_MICRO

from .provider import FakeProvider


_WEBHOOK_SECRET = "whsec_callmatrix_journey"


def _auth(token: str) -> dict[str, str]:
    return {"X-Treg-Token": token}


def _stripe_signature(payload: bytes) -> str:
    timestamp = int(time.time())
    digest = hmac.new(
        _WEBHOOK_SECRET.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


async def test_j1_signup_to_topup_and_recovery(
    matrix_clients: AsyncClient,
    fake_provider: FakeProvider,
    platform_on,
    monkeypatch,
) -> None:
    # Register by email, create the first team, and verify its $1 promotional balance.
    email = "journey-owner@example.com"
    delivered_codes: dict[str, str] = {}

    async def capture_otp(address: str, code: str, *, ttl_minutes: int) -> bool:
        delivered_codes[address] = code
        return True

    monkeypatch.setattr("treg.email.send_otp", capture_otp)
    start = await matrix_clients.post("/auth/email/start", json={"email": email})
    assert start.status_code == 200, start.text
    code = start.json().get("dev_code") or delivered_codes[email]
    verify = await matrix_clients.post(
        "/auth/email/verify", json={"email": email, "code": code},
    )
    assert verify.status_code == 200, verify.text
    identity_token = verify.json()["token"]
    assert verify.json()["email"] == email

    team = await matrix_clients.post(
        "/orgs", headers=_auth(identity_token), json={"name": "Call Matrix Journey"},
    )
    assert team.status_code == 200, team.text
    org_id = team.json()["org_id"]
    owner_token = team.json()["token"]
    owner_headers = _auth(owner_token)
    balance = await matrix_clients.get(f"/orgs/{org_id}/balance", headers=owner_headers)
    assert balance.status_code == 200, balance.text
    assert balance.json()["balance_micro"] == 1_000_000
    assert [(row["kind"], row["amount_micro"]) for row in balance.json()["entries"]["items"]] == [
        ("grant", 1_000_000),
    ]

    # Register an own tool and prove its write-only credential was injected.
    secret = await matrix_clients.post(
        "/secrets", headers=owner_headers,
        json={"name": "journey-key", "value": "JOURNEY-SECRET"},
    )
    assert secret.status_code == 200, secret.text
    tool = await matrix_clients.post(
        "/tools", headers=owner_headers, json={
            "name": "journey-echo",
            "base_url": "https://journey-provider.example",
            "secret_id": secret.json()["id"],
        },
    )
    assert tool.status_code == 200, tool.text
    own_call = await matrix_clients.get(
        "/call/journey-echo/proof", headers={
            **owner_headers, "X-Fake-Body": '{"own":"ok"}',
        },
    )
    assert own_call.status_code == 200 and own_call.json() == {"own": "ok"}
    assert fake_provider.hits[-1].headers["authorization"] == "Bearer JOURNEY-SECRET"

    # A catalog call uses treg's key and settles its reserved estimate.
    before_platform = balance.json()["balance_micro"]
    platform_call = await matrix_clients.get(
        f"/call/{EP}?aweme_id=journey", headers=owner_headers,
    )
    assert platform_call.status_code == 200, platform_call.text
    charged = with_margin(EP_MICRO)
    assert platform_call.headers["X-Treg-Cost-Micro"] == str(charged)
    assert fake_provider.hits[-1].headers["authorization"].startswith("Bearer PLATFORM-")
    after_platform = await matrix_clients.get(f"/orgs/{org_id}/balance", headers=owner_headers)
    assert after_platform.json()["balance_micro"] == before_platform - charged
    kinds = [row["kind"] for row in after_platform.json()["entries"]["items"]]
    assert kinds.count("reserve") == 1 and kinds.count("settle") == 1

    await audit.drain()
    calls = await matrix_clients.get("/calls", headers=owner_headers)
    assert calls.status_code == 200, calls.text
    assert len(calls.json()) == 2
    by_name = {row["endpoint_id"] or row["tool_name"]: row for row in calls.json()}
    assert by_name["journey-echo"]["status_code"] == 200
    assert by_name["journey-echo"]["cost_charged_micro"] is None
    assert by_name[EP]["status_code"] == 200
    assert by_name[EP]["cost_charged_micro"] == charged

    # A viewer with an empty tool ACL must be refused before the provider.
    invite = await matrix_clients.post(
        f"/orgs/{org_id}/invites", headers=owner_headers,
        json={
            "email": "journey-viewer@example.com",
            "role": "viewer",
            "tool_access": [],
        },
    )
    assert invite.status_code == 200, invite.text
    accepted = await matrix_clients.post(
        "/invites/accept",
        json={"code": invite.json()["code"], "email": "journey-viewer@example.com"},
    )
    assert accepted.status_code == 200, accepted.text
    hits_before_viewer = len(fake_provider.hits)
    viewer_call = await matrix_clients.get(
        "/call/journey-echo/viewer", headers=_auth(accepted.json()["token"]),
    )
    viewer_reached_provider = len(fake_provider.hits) != hits_before_viewer

    # Drain the promo through a provider-reported catalog cost, without direct ledger access.
    drain = await matrix_clients.post(
        f"/call/{EP_DFS}", headers={**owner_headers, "X-Fake-Cost": "10.0"},
        json=[{"url": "https://example.com/"}],
    )
    assert drain.status_code == 200, drain.text
    empty = await matrix_clients.get(f"/orgs/{org_id}/balance", headers=owner_headers)
    assert empty.json()["balance_micro"] == 0
    refused = await matrix_clients.get(
        f"/call/{EP}?aweme_id=out-of-credit", headers=owner_headers,
    )
    assert refused.status_code == 402, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "insufficient_balance"
    assert detail["balance_micro"] == 0
    # The margined figure, which is what `ledger.reserve` needs and therefore what the 402 quotes.
    # `EP_MICRO` passes only while TREG_PLATFORM_MARGIN is 0.
    assert detail["estimated_cost_micro"] == charged
    assert detail["topup_url"] == "/app#billing"

    # Deliver one signed payment twice, verify one credit, then retry the call.
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_webhook_secret", _WEBHOOK_SECRET, raising=False)
    event = {"id": "evt_journey_topup", "type": "payment_intent.succeeded", "data": {"object": {
        "id": "pi_journey_topup",
        "object": "payment_intent",
        "amount": 500,
        "amount_received": 500,
        "currency": "usd",
        "status": "succeeded",
        "payment_method": "pm_journey",
        "metadata": {
            "treg_org_id": str(org_id), "treg_kind": "topup", "treg_auto": "0",
        },
    }}}
    payload = json.dumps(event, separators=(",", ":")).encode()
    webhook_headers = {
        "Stripe-Signature": _stripe_signature(payload), "Content-Type": "application/json",
    }
    first_delivery = await matrix_clients.post(
        "/billing/stripe/webhook", content=payload, headers=webhook_headers,
    )
    second_delivery = await matrix_clients.post(
        "/billing/stripe/webhook", content=payload, headers=webhook_headers,
    )
    assert first_delivery.status_code == 200 and first_delivery.json()["credited"] is True
    assert second_delivery.status_code == 200 and second_delivery.json()["credited"] is False
    funded = await matrix_clients.get(f"/orgs/{org_id}/balance", headers=owner_headers)
    assert funded.json()["balance_micro"] == 5_000_000
    assert len([
        row for row in funded.json()["entries"]["items"] if row["kind"] == "topup"
    ]) == 1
    assert len([
        block for block in funded.json()["blocks"] if block["kind"] == "purchased"
    ]) == 1  # one payment, one block - the redelivery moved nothing

    recovered = await matrix_clients.get(
        f"/call/{EP}?aweme_id=after-topup", headers=owner_headers,
    )
    assert recovered.status_code == 200, recovered.text
    recovered_balance = await matrix_clients.get(f"/orgs/{org_id}/balance", headers=owner_headers)
    assert recovered_balance.json()["balance_micro"] == 5_000_000 - charged

    assert viewer_call.status_code == 403, viewer_call.text
    assert viewer_call.headers.get("X-Treg-Error") == "1"
    assert viewer_reached_provider is False
