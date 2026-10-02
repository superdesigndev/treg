"""Anymail Finder bring-your-own-key catalog: per-verified-result credit rows and a free account probe."""

from __future__ import annotations

import httpx
from httpx import AsyncClient

from treg import oauth_providers as P
from treg.api import app
from treg.domain.catalog.store import load


def test_anymailfinder_connects_with_a_free_raw_key_probe():
    provider = P.REGISTRY["anymailfinder"]
    assert provider.auth_kind == "key"
    assert provider.uses_pasted_secret is True
    assert P.is_configured(provider) is True
    assert provider.base_url == "https://api.anymailfinder.com/v5.1"
    assert provider.token_header == "Authorization"
    assert provider.token_format == "{secret}"
    assert provider.probe_path == "/account"
    assert provider.probe_method == "GET"
    assert provider.probe_cost_micro == 0
    assert provider.token_verify_field == "email"


async def test_anymailfinder_key_rides_raw_and_a_bad_key_is_rejected(clients, monkeypatch):
    def probe(request):
        assert request.method == "GET"
        assert request.url.host == "api.anymailfinder.com"
        assert request.url.path == "/v5.1/account"
        if request.headers["authorization"] == "bad":
            return httpx.Response(401, json={"error": "unauthorized", "message": "Missing or invalid API key."})
        assert request.headers["authorization"] == "own-key"  # raw, no scheme prefix
        return httpx.Response(200, json={"credits_left": 0, "email": "owner@example.com"})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post("/connections/token", json={"provider": "anymailfinder", "token": "bad"})
        assert bad.status_code == 422, bad.text
        # a spent account (credits_left 0) is still a VALID key: `email` is the verify field, not the balance
        good = await clients.post("/connections/token", json={"provider": "anymailfinder", "token": "own-key"})
        assert good.status_code == 200, good.text


def test_anymailfinder_paid_rows_bill_per_verified_result_and_report_the_charge():
    catalog = load()
    expected_credits = {
        "anymailfinder.find-email.person": ("per_success", 1),
        "anymailfinder.find-email.decision-maker": ("per_success", 2),
        "anymailfinder.find-email.company": ("per_success", 1),
        "anymailfinder.verify-email": ("per_call", 0.2),
    }
    rate = 0.0109  # fx.yaml credit rate: the smallest one-time credit pack, $1,090 / 100,000
    for endpoint_id, (cost_type, credits) in expected_credits.items():
        row = catalog.by_id[endpoint_id]
        cost = row["cost"]
        assert cost["type"] == cost_type, endpoint_id
        assert cost["currency"] == "credit" and cost["value"] == credits, endpoint_id
        # the response states the exact charge (0 on a miss or a 30-day repeat), so settlement reads it
        assert cost["reported_charge"] == {"path": "credits_charged", "unit": "credit"}, endpoint_id
        assert cost["confidence"] in {"verified", "documented"}, endpoint_id
        assert catalog.platform_eligible(row), endpoint_id
        view = catalog.cost_view(cost, "anymailfinder")
        assert abs(view["usd"] - credits * rate) < 1e-9, (endpoint_id, view)
    for endpoint_id in ("anymailfinder.report.bad-email", "anymailfinder.account"):
        assert catalog.by_id[endpoint_id]["cost"]["type"] == "free", endpoint_id


def test_anymailfinder_adapters_read_the_verified_field_and_judge_a_200_miss():
    catalog = load()
    find = catalog.adapters["anymailfinder.find-email.person"]
    assert find.verified, find.verify_note
    q, body = find.to_upstream({"full_name": "Patrick Collison", "first_name": "Patrick",
                                "last_name": "Collison", "domain": "stripe.com"})
    assert q == {} and body == {"full_name": "Patrick Collison", "domain": "stripe.com"}
    hit = {"email": "p@stripe.com", "valid_email": "p@stripe.com", "email_status": "valid", "credits_charged": 1}
    assert find.from_upstream(hit) == {"email": "p@stripe.com", "verified": True}
    # a miss is HTTP 200 with not_found; a `risky` guess has an email but no valid_email - also a miss
    assert find.is_miss({"email": None, "valid_email": None, "email_status": "not_found", "credits_charged": 0})
    assert find.is_miss({"email": "guess@stripe.com", "valid_email": None, "email_status": "risky", "credits_charged": 0})
    assert not find.is_miss(hit)

    # the same route takes a LinkedIn URL on its own; only the matched variant's keys are sent
    _, body = find.to_upstream({"linkedin_url": "https://www.linkedin.com/in/satyanadella/"}, variant=("linkedin_url",))
    assert body == {"linkedin_url": "https://www.linkedin.com/in/satyanadella/"}

    verify = catalog.adapters["anymailfinder.verify-email"]
    assert verify.verified, verify.verify_note
    assert verify.from_upstream({"email_status": "invalid", "credits_charged": 0.2}) == {"valid": False, "status": "invalid"}
    assert verify.from_upstream({"email_status": "valid", "credits_charged": 0.2}) == {"valid": True, "status": "valid"}
