"""Unit tests for balance collectors in treg.domain.capacity.collectors.

Each test mocks the upstream API response and verifies that the collector returns the expected
{"value", "unit", "note"} dict. This tests parsing logic without hitting real APIs.
"""

from __future__ import annotations

import math
from datetime import timedelta

import httpx
import pytest

from treg.domain.capacity import collectors, policy, sweep
from treg.timeutil import utcnow_naive


async def test_fishaudio_balance_uses_workspace_wallet(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_FISHAUDIO", "private-test-key")
    monkeypatch.setenv("TREG_PLATFORM_FISHAUDIO_WORKSPACE_ID", "workspace-test-id")
    collectors.get_settings.cache_clear()
    try:
        def probe(request):
            assert request.method == "GET"
            assert request.url.path == "/wallet/self/api-credit"
            assert request.url.params.get("team_id") == "workspace-test-id"
            assert request.headers["authorization"] == "Bearer private-test-key"
            return httpx.Response(200, json={"credit": "99.957540"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
            row = await collectors.provider_balance("fishaudio", client)
    finally:
        collectors.get_settings.cache_clear()
    assert row["value"] == 99.95754
    assert row["unit"] == "USD"
    capacity = policy.default_policy("fishaudio", has_key=True)
    assert capacity.capacity_type == "cash"
    assert capacity.funding_mode == "manual"
    assert capacity.source == "api"
    assert capacity.rate_limit is None


async def test_fishaudio_balance_is_unknown_without_workspace_and_skips_request(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_FISHAUDIO", "private-test-key")
    monkeypatch.setenv("TREG_PLATFORM_FISHAUDIO_WORKSPACE_ID", "")
    collectors.get_settings.cache_clear()
    try:
        def probe(_request):
            pytest.fail("missing workspace ID must not call Fish's unscoped personal wallet")

        async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
            row = await collectors.provider_balance("fishaudio", client)
    finally:
        collectors.get_settings.cache_clear()
    assert row["value"] is None
    assert row["unit"] == "USD"
    assert "not configured" in row["note"]


@pytest.mark.parametrize("credit", [None, True, "not-a-number", "NaN", "Infinity", -1])
async def test_fishaudio_balance_rejects_invalid_credit(monkeypatch, credit):
    monkeypatch.setenv("TREG_PLATFORM_FISHAUDIO_WORKSPACE_ID", "workspace-test-id")
    collectors.get_settings.cache_clear()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"credit": credit}))) as client:
            row = await collectors._fishaudio(client, "test")
    finally:
        collectors.get_settings.cache_clear()
    assert row["value"] is None


async def test_openmart_balance_collector_and_policy():
    def probe(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v2/credit-balance"
        assert request.headers["authorization"] == "Bearer test"
        return httpx.Response(200, json={
            "balance": 4800,
            "period_start": "2026-09-01T00:00:00Z",
            "period_end": "2026-10-01T00:00:00Z",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._openmart(client, "test")
    assert row == {
        "value": 4800,
        "unit": "credits",
        "note": "Monthly subscription balance; current period ends 2026-10-01T00:00:00Z.",
    }
    capacity = policy.default_policy("openmart", has_key=True)
    assert capacity.capacity_type == "credits"
    assert capacity.funding_mode == "subscription"
    assert capacity.rate_limit == {"limit": 15, "window_s": 1, "source": "docs"}


async def test_tavily_capacity_uses_key_credit_remainder_and_conservative_rate():
    def probe(request):
        assert request.method == "GET"
        assert request.url == "https://api.tavily.com/usage"
        assert request.headers["authorization"] == "Bearer test"
        return httpx.Response(200, json={
            "key": {"usage": 125, "limit": 1000},
            "account": {"plan_usage": 125, "plan_limit": 1000,
                        "paygo_usage": 0, "paygo_limit": 5000},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._tavily(client, "test")
    assert row == {
        "value": 875,
        "unit": "API credits",
        "note": "key usage 125 of 1000; account pools are informational",
    }
    capacity = policy.default_policy("tavily", has_key=True)
    assert capacity.capacity_type == "credits"
    assert capacity.funding_mode == "manual"
    assert capacity.source == "api"
    assert capacity.rate_limit == {"limit": 100, "window_s": 60, "source": "docs"}


async def test_serper_capacity_uses_free_account_balance_and_live_rate():
    def probe(request):
        assert request.method == "GET"
        assert request.url == "https://google.serper.dev/account"
        assert request.headers["x-api-key"] == "test"
        return httpx.Response(200, json={"balance": 2476, "rateLimit": 50})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._serper(client, "test")
    assert row == {
        "value": 2476.0,
        "unit": "credits",
        "note": "account rate limit 50 queries/s",
    }
    capacity = policy.default_policy("serper", has_key=True)
    assert capacity.capacity_type == "credits"
    assert capacity.funding_mode == "auto_recharge"
    assert capacity.auto_funding_enabled is True
    assert capacity.source == "api"
    assert capacity.rate_limit == {"limit": 50, "window_s": 1, "source": "api"}


@pytest.mark.parametrize("balance", [None, True, "bad", "NaN", "Infinity", -1])
async def test_serper_capacity_rejects_invalid_balance(balance):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"balance": balance, "rateLimit": 5}))) as client:
        with pytest.raises(ValueError, match="invalid balance"):
            await collectors._serper(client, "test")


async def test_fetchin_capacity_uses_free_subscription_balance_and_safe_rate():
    def probe(request):
        assert request.method == "GET"
        assert request.url == "https://api.fetchin.io/api/v1/subscription"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json={
            "plan": "free", "status": "free", "creditsRemaining": 51_000,
            "paygCreditsRemaining": 50_000, "renewalDate": None, "rpsLimit": 5,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._fetchinio(client, "test-key")
    assert row["value"] == 51_000
    assert row["unit"] == "credits"
    assert "account limit 5 requests/s" in row["note"]
    capacity = policy.default_policy("fetchinio", has_key=True)
    assert capacity.capacity_type == "credits"
    assert capacity.funding_mode == "manual"
    assert capacity.source == "api"
    assert capacity.rate_limit == {"limit": 2, "window_s": 1, "source": "policy"}


@pytest.mark.parametrize("remaining", [None, True, -1, "51000", float("nan")])
async def test_fetchin_capacity_rejects_uncertain_balances(remaining):
    def probe(_request):
        if isinstance(remaining, float) and math.isnan(remaining):
            return httpx.Response(200, content=b'{"creditsRemaining": NaN}')
        return httpx.Response(200, json={"creditsRemaining": remaining})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        with pytest.raises(ValueError, match="remaining-credit"):
            await collectors._fetchinio(client, "test-key")


async def test_tavily_capacity_uses_account_pool_when_key_has_no_limit():
    payload = {
        "key": {"usage": 0, "limit": None},
        "account": {"plan_usage": 12, "plan_limit": 1000,
                    "paygo_usage": 3, "paygo_limit": 100},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )) as client:
        row = await collectors._tavily(client, "test")
    assert row == {
        "value": 1085,
        "unit": "API credits",
        "note": "key has no finite cap; remaining plan, PAYGO account pool(s)",
    }


@pytest.mark.parametrize("payload", [
    {}, {"key": {"usage": True, "limit": 1000}},
    {"key": {"usage": -1, "limit": 1000}},
    {"key": {"usage": 1, "limit": "1000"}},
])
async def test_tavily_capacity_rejects_uncertain_usage(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )) as client:
        row = await collectors._tavily(client, "test")
    assert row["value"] is None
    assert row["unit"] == "API credits"


@pytest.mark.parametrize("value,expected", [(0, 0), (71, 71), ("5000", 5000)])
async def test_zerobounce_balance_accepts_nonnegative_integer_values(monkeypatch, value, expected):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ZEROBOUNCE", "private-test-key")
    collectors.get_settings.cache_clear()

    def reply(request):
        assert request.url.path == "/v2/getcredits"
        assert request.url.params["api_key"] == "private-test-key"
        return httpx.Response(200, json={"Credits": value})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            row = await collectors.provider_balance("zerobounce", client)
        assert row["value"] == expected
        assert row["unit"] == "credits"
        assert "manual" in row["note"]
    finally:
        collectors.get_settings.cache_clear()


@pytest.mark.parametrize("status,value", [
    (200, -1), (200, "-1"), (200, True), (200, 12.5), (200, "12.5"), (200, "bad"),
    (403, None),
])
async def test_zerobounce_balance_rejects_uncertain_values_without_exposing_key(
    monkeypatch, status, value,
):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ZEROBOUNCE", "private-test-key")
    collectors.get_settings.cache_clear()

    def reply(request):
        return httpx.Response(status, json={"Credits": value}, request=request)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            row = await collectors.provider_balance("zerobounce", client)
        assert row["value"] is None
        assert row["note"]
        assert "private-test-key" not in str(row)
    finally:
        collectors.get_settings.cache_clear()


def test_zerobounce_capacity_policy_stays_manual_until_vendor_auto_pay_is_verified():
    row = policy.default_policy("zerobounce", has_key=True)
    assert row.capacity_type == "credits"
    assert row.funding_mode == "manual"
    assert row.source == "api"
    assert row.auto_funding_enabled is False
    assert row.rate_limit == {"limit": 25, "window_s": 1, "source": "policy"}


@pytest.mark.parametrize("balance", [0, 465])
async def test_millionverifier_balance_uses_query_key_without_double_counting(monkeypatch, balance):
    monkeypatch.setenv("TREG_PLATFORM_KEY_MILLIONVERIFIER", "private-test-key")
    collectors.get_settings.cache_clear()
    def reply(request):
        assert request.url.path == "/api/v3/credits"
        assert request.url.params["api"] == "private-test-key"
        return httpx.Response(200, json={"credits": balance, "bulk_credits": balance})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            row = await collectors.provider_balance("millionverifier", client)
        assert row == {"provider": "millionverifier", "value": balance, "unit": "credits", "note": ""}
    finally:
        collectors.get_settings.cache_clear()


@pytest.mark.parametrize("balance", [0, 9997, 12.5])
async def test_bounceban_balance_uses_raw_authorization_header(monkeypatch, balance):
    monkeypatch.setenv("TREG_PLATFORM_KEY_BOUNCEBAN", "private-test-key")
    collectors.get_settings.cache_clear()

    def reply(request):
        assert request.url == "https://api.bounceban.com/v1/account"
        assert request.headers["authorization"] == "private-test-key"
        return httpx.Response(200, json={
            "owner_email": "owner@example.com",
            "available_credits": balance,
            "rate_limit": [],
        })

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            row = await collectors.provider_balance("bounceban", client)
        assert row == {"provider": "bounceban", "value": balance,
                       "unit": "verification credits", "note": ""}
    finally:
        collectors.get_settings.cache_clear()


@pytest.mark.parametrize("balance", [None, -1, True, "9997"])
async def test_bounceban_balance_rejects_uncertain_values_without_exposing_key(monkeypatch, balance):
    monkeypatch.setenv("TREG_PLATFORM_KEY_BOUNCEBAN", "private-test-key")
    collectors.get_settings.cache_clear()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"available_credits": balance}))) as client:
            row = await collectors.provider_balance("bounceban", client)
        assert row["value"] is None
        assert "valid verification-credit balance" in row["note"]
        assert "private-test-key" not in str(row)
    finally:
        collectors.get_settings.cache_clear()


async def test_bounceban_balance_rejects_non_finite_value(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"available_credits": float("inf")}

    class Client:
        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setenv("TREG_PLATFORM_KEY_BOUNCEBAN", "private-test-key")
    collectors.get_settings.cache_clear()
    try:
        row = await collectors.provider_balance("bounceban", Client())
        assert row["value"] is None
        assert "valid verification-credit balance" in row["note"]
        assert "private-test-key" not in str(row)
    finally:
        collectors.get_settings.cache_clear()


def test_bounceban_capacity_policy_uses_manual_prepaid_credits():
    row = policy.default_policy("bounceban", has_key=True)
    assert row.capacity_type == "credits"
    assert row.funding_mode == "manual"
    assert row.source == "api"
    assert row.auto_funding_enabled is False
    assert row.rate_limit == {"limit": 25, "window_s": 1, "source": "docs"}


@pytest.mark.parametrize("status,body", [(200, {"error": "apikey_not_found"}), (401, {}), (200, {})])
async def test_millionverifier_balance_errors_do_not_expose_key(monkeypatch, status, body):
    monkeypatch.setenv("TREG_PLATFORM_KEY_MILLIONVERIFIER", "private-test-key")
    collectors.get_settings.cache_clear()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json=body))) as client:
            row = await collectors.provider_balance("millionverifier", client)
        assert row["value"] is None
        assert row["note"]
        assert "private-test-key" not in str(row)
    finally:
        collectors.get_settings.cache_clear()


class MockResponse:
    """A minimal httpx.Response stand-in for collector tests."""

    def __init__(self, json_data: dict, status_code: int = 200, headers: dict | None = None):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )


class MockClient:
    """A minimal httpx.AsyncClient stand-in that returns preset responses."""

    def __init__(self, get_response: MockResponse | None = None, post_response: MockResponse | None = None):
        self._get_response = get_response
        self._post_response = post_response

    async def get(self, url, **kwargs):
        return self._get_response

    async def post(self, url, **kwargs):
        return self._post_response


# ---- brightdata -------------------------------------------------------------------------

async def test_brightdata_collector_parses_balance_and_pending():
    resp = MockResponse({"balance": 456.78, "credit": 0, "prepayment": 0, "pending_costs": 12.34})
    client = MockClient(get_response=resp)
    result = await collectors._brightdata(client, "test-key")
    assert result["value"] == 456.78
    assert result["unit"] == "USD"
    assert "pending $12.34" in result["note"]


async def test_brightdata_collector_handles_missing_pending():
    resp = MockResponse({"balance": 100.0, "credit": 0, "prepayment": 0})
    client = MockClient(get_response=resp)
    result = await collectors._brightdata(client, "test-key")
    assert result["value"] == 100.0
    assert "pending $0.00" in result["note"]


# ---- crustdata --------------------------------------------------------------------------

async def test_crustdata_collector_parses_credits_and_recurring():
    resp = MockResponse({
        "account": {
            "credits": 5000.5,
            "recurring_credits": 2000,
            "recurring_credits_frequency": "monthly",
            "recurring_credits_refresh_date": "2026-09-01T00:00:00Z"
        }
    })
    client = MockClient(get_response=resp)
    result = await collectors._crustdata(client, "test-key")
    assert result["value"] == 5000.5
    assert result["unit"] == "credits"
    assert "recurring 2000 monthly" in result["note"]
    assert "2026-09-01" in result["note"]


async def test_crustdata_collector_handles_no_recurring_grant():
    resp = MockResponse({
        "account": {
            "credits": 1234.0,
            "recurring_credits": None,
            "recurring_credits_frequency": None,
            "recurring_credits_refresh_date": None
        }
    })
    client = MockClient(get_response=resp)
    result = await collectors._crustdata(client, "test-key")
    assert result["value"] == 1234.0
    assert "no recurring grant" in result["note"]


# ---- akta -------------------------------------------------------------------------------

async def test_akta_collector_parses_credits_and_tier():
    resp = MockResponse({
        "credit_balance": 9500.0,
        "balance_amount": 0.0,
        "currency": "USD",
        "package_type": "premium",
        "is_enterprise": False,
        "lifetime_consumed_credits": 100.0
    })
    client = MockClient(get_response=resp)
    result = await collectors._akta(client, "test-key")
    assert result["value"] == 9500.0
    assert result["unit"] == "credits"
    assert "tier premium" in result["note"]
    assert "(enterprise)" not in result["note"]
    assert "lifetime 100.0 used" in result["note"]


async def test_akta_collector_marks_enterprise_accounts():
    resp = MockResponse({
        "credit_balance": 50000.0,
        "package_type": "scale",
        "is_enterprise": True,
        "lifetime_consumed_credits": 0
    })
    client = MockClient(get_response=resp)
    result = await collectors._akta(client, "test-key")
    assert result["value"] == 50000.0
    assert "(enterprise)" in result["note"]


# ---- NO_BALANCE_API / BALANCE_ROUTES registration ---------------------------------------

def test_no_balance_api_includes_expected_providers():
    """Verify the vendors that have no free balance API are documented."""
    expected = {
        "adyntel", "aviato", "coresignal", "exa", "financialdatasets", "finnhub",
        "justoneapi", "keenable", "limadata", "marketstack", "scrubby", "tiingo", "trestleiq",
    }
    assert expected == set(collectors.NO_BALANCE_API.keys())


async def test_keenable_capacity_is_portal_only_with_documented_rate_limit(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_KEENABLE", "test")
    collectors.get_settings.cache_clear()
    try:
        row = await collectors.provider_balance("keenable")
        assert row["value"] is None and row["no_api"] is True
        capacity = policy.default_policy("keenable", has_key=True)
        assert capacity.capacity_type == "requests"
        assert capacity.funding_mode == "manual"
        assert capacity.source == "manual"
        assert capacity.rate_limit == {"limit": 10, "window_s": 1, "source": "docs"}
    finally:
        collectors.get_settings.cache_clear()


async def test_olostep_balance_and_conservative_shared_key_rate(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_OLOSTEP", "test-key")
    collectors.get_settings.cache_clear()
    try:
        def probe(request):
            assert request.method == "GET"
            assert request.url.path == "/user/credits/info"
            assert request.headers["authorization"] == "Bearer test-key"
            return httpx.Response(200, json={
                "credits": 4321,
                "active_subscription": {"display_name": "Free"},
                "allow_usage": True,
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
            row = await collectors.provider_balance("olostep", client)
        assert row == {
            "provider": "olostep",
            "value": 4321,
            "unit": "credits",
            "note": "plan Free; usage allowed",
        }
        capacity = policy.default_policy("olostep", has_key=True)
        assert capacity.capacity_type == "credits"
        assert capacity.funding_mode == "manual"
        assert capacity.source == "api"
        assert capacity.rate_limit == {"limit": 5, "window_s": 1, "source": "policy"}
    finally:
        collectors.get_settings.cache_clear()


async def test_scrapegraphai_balance_and_shared_key_policy(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_SCRAPEGRAPHAI", "test-key")
    collectors.get_settings.cache_clear()
    try:
        def probe(request):
            assert request.method == "GET"
            assert request.url == "https://v2-api.scrapegraphai.com/api/credits"
            assert request.headers["SGAI-APIKEY"] == "test-key"
            return httpx.Response(200, json={
                "remaining": 475,
                "used": 25,
                "plan": "Free Plan",
                "jobs": {
                    "crawl": {"used": 0, "limit": 1},
                    "monitor": {"used": 0, "limit": 1},
                },
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
            row = await collectors.provider_balance("scrapegraphai", client)
        assert row == {
            "provider": "scrapegraphai",
            "value": 475,
            "unit": "credits",
            "note": "plan Free Plan; used 25; crawl jobs 0/1; monitors 0/1",
        }
        capacity = policy.default_policy("scrapegraphai", has_key=True)
        assert capacity.capacity_type == "credits"
        assert capacity.funding_mode == "subscription"
        assert capacity.source == "api"
        assert capacity.rate_limit == {"limit": 500, "window_s": 60, "source": "policy"}
    finally:
        collectors.get_settings.cache_clear()


@pytest.mark.parametrize("remaining", [None, True, -1, "475", float("nan")])
async def test_scrapegraphai_balance_rejects_uncertain_values(remaining):
    def probe(_request):
        if isinstance(remaining, float) and math.isnan(remaining):
            return httpx.Response(200, content=b'{"remaining": NaN}')
        return httpx.Response(200, json={"remaining": remaining})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        with pytest.raises(ValueError, match="remaining-credit"):
            await collectors._scrapegraphai(client, "test")


async def test_adyntel_capacity_is_dashboard_only_and_rate_limited(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_ADYNTEL", "PLATFORM-ADYNTEL")
    collectors.get_settings.cache_clear()
    try:
        row = await collectors.provider_balance("adyntel")
        assert row["no_api"] is True and row["value"] is None
        assert "dashboard only" in row["note"]
        capacity = policy.default_policy("adyntel", has_key=True)
        assert capacity.capacity_type == "credits"
        assert capacity.funding_mode == "manual"
        assert capacity.auto_funding_enabled is False
        assert capacity.source == "manual"
        assert capacity.rate_limit == {"limit": 5, "window_s": 1, "source": "docs"}
    finally:
        collectors.get_settings.cache_clear()


def test_limadata_policy_uses_auto_recharge_and_the_documented_rate():
    row = policy.default_policy("limadata", has_key=True)
    assert row.capacity_type == "credits"
    assert row.funding_mode == "auto_recharge"
    assert row.auto_funding_enabled is True
    assert row.source == "manual"
    assert row.rate_limit == {"limit": 1, "window_s": 1, "source": "docs"}


async def test_trestleiq_capacity_is_portal_only_with_manually_verified_auto_recharge(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TRESTLEIQ", "PLATFORM-TRESTLEIQ")
    collectors.get_settings.cache_clear()
    try:
        row = await collectors.provider_balance("trestleiq")
        assert row["no_api"] is True and row["value"] is None
        capacity = policy.default_policy("trestleiq", has_key=True)
        assert capacity.capacity_type == "cash"
        assert capacity.funding_mode == "auto_recharge"
        assert capacity.auto_funding_enabled is True
        assert capacity.source == "manual"
        assert capacity.rate_limit == {"limit": 10, "window_s": 1, "source": "docs"}
    finally:
        collectors.get_settings.cache_clear()


def test_implemented_collectors_are_registered_and_do_not_overlap_absent_list():
    """A collector that parses a vendor must be on BALANCE_ROUTES, and a provider
    cannot be both 'we collect' and 'there is no balance API'."""
    for provider in ("aiark", "akta", "brightdata", "crustdata", "dropleads", "getleadsio", "prospeo", "wiza"):
        assert provider in collectors.BALANCE_ROUTES
        assert provider not in collectors.NO_BALANCE_API
    overlap = set(collectors.BALANCE_ROUTES.keys()) & set(collectors.NO_BALANCE_API.keys())
    assert not overlap, f"Providers in both maps: {overlap}"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"success": True, "credits": {"totalAvailable": 94.6, "subscription": 0,
                                         "payg": 94.6, "usePayg": True}}, 94.6),
        ({"success": True, "credits": {"totalAvailable": 0}}, 0),
        ({"success": True, "credits": {"totalAvailable": -1}}, None),
        ({"success": True, "credits": {"totalAvailable": True}}, None),
        ({"success": False, "credits": {"totalAvailable": 10}}, None),
    ],
)
async def test_dropleads_balance_collector_uses_total_available(payload, expected):
    def serve(request):
        assert request.url.path == "/api/v2/prime-db/credits/balance"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        row = await collectors._dropleads(upstream, "test-key")
    assert row["value"] == expected
    assert row["unit"] == "credits"


@pytest.mark.parametrize("remaining,expected", [(1987, 1987), (0, 0), (-1, None), (True, None)])
async def test_prospeo_balance_collector_uses_remaining_credits(remaining, expected):
    def serve(request):
        assert request.url.path == "/account-information"
        assert request.headers["x-key"] == "test-key"
        return httpx.Response(200, json={
            "error": False,
            "response": {
                "current_plan": "STARTER",
                "remaining_credits": remaining,
                "used_credits": 13,
                "next_quota_renewal_date": "2026-10-16T00:00:00Z",
            },
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        if expected is None:
            with pytest.raises(ValueError):
                await collectors._prospeo(upstream, "test-key")
        else:
            row = await collectors._prospeo(upstream, "test-key")
            assert row["value"] == expected
            assert row["unit"] == "credits"
            assert "plan STARTER" in row["note"]


@pytest.mark.parametrize(
    "remaining,expected",
    [(15000, 15000), (0, 0), (15000.5, 15000.5), (-1, None), (True, None)],
)
async def test_aiark_balance_collector_uses_total(remaining, expected):
    def serve(request):
        assert request.url.path == "/api/developer-portal/v1/payments/credits"
        assert request.headers["x-token"] == "test-key"
        return httpx.Response(200, json={"total": remaining})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        if expected is None:
            with pytest.raises(ValueError):
                await collectors._aiark(upstream, "test-key")
        else:
            row = await collectors._aiark(upstream, "test-key")
            assert row["value"] == expected
            assert row["unit"] == "credits"
            assert "roll over" in row["note"]


def test_aiark_policy_uses_subscription_and_documented_rate():
    row = policy.default_policy("aiark", has_key=True)
    assert row.capacity_type == "monthly_quota"
    assert row.funding_mode == "quota_reset"
    assert row.auto_funding_enabled is False
    assert row.rate_limit == {"limit": 5, "window_s": 1, "source": "docs"}


@pytest.mark.parametrize("remaining,expected", [(997, 997), (0, 0), (-1, None), (True, None)])
async def test_getleadsio_balance_collector_uses_fair_use_credits(remaining, expected):
    def serve(request):
        assert request.url.path == "/api/v1/usage/fair-use"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"ok": True, "credits_remaining": remaining})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        if expected is None:
            with pytest.raises(ValueError):
                await collectors._getleadsio(upstream, "test-key")
        else:
            row = await collectors._getleadsio(upstream, "test-key")
            assert row["value"] == expected
            assert row["unit"] == "credits"
            assert "Live Leads wallet is not included" in row["note"]


def test_getleadsio_policy_uses_the_documented_default_rate():
    row = policy.default_policy("getleadsio", has_key=True)
    assert row.rate_limit == {"limit": 100, "window_s": 60, "source": "docs"}


def test_prospeo_policy_smooths_at_the_stricter_shared_key_rate():
    row = policy.default_policy("prospeo", has_key=True)
    assert row.rate_limit == {"limit": 1, "window_s": 1, "source": "docs"}


@pytest.mark.parametrize('remaining,expected', [(300, 300), (0, 0), (None, None), (-1, None), ('unlimited', None), (True, None)])
async def test_quickenrich_subscription_allowance_from_free_discovery(remaining, expected):
    def reply(request):
        import json
        assert request.method == 'POST' and request.url.path == '/api/employees/contact-finder'
        assert request.headers['authorization'] == 'Bearer private-test-key'
        assert json.loads(request.content)['per_page'] == 1
        return httpx.Response(200, json={'success': True, 'data': [], 'meta': {'credits_used': 0, 'remaining_credits': remaining}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        row = await collectors._quickenrich(client, 'private-test-key')
    assert row['value'] == expected
    assert 'private-test-key' not in str(row)


@pytest.mark.parametrize('balance',[0,9.992])
async def test_trykitt_balance_is_usd(monkeypatch,kitt_on,balance):
    def reply(request):
        assert request.headers['x-api-key']=='TEST-KITT-KEY'
        assert request.url.path=='/credit'
        return httpx.Response(200,json={'credits':balance})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        row=await collectors.provider_balance('trykitt',client)
    assert row['value']==balance and row['unit']=='USD'


# ---- ContactOut ----

async def test_contactout_pool_stats_are_informational_even_when_empty(contactout_platform):
    for extra in ({}, {"remaining": -5, "phone_remaining": 0, "search_remaining": 20}):
        usage = {
            "count": 10,
            "quota": 0,
            "phone_count": 20,
            "phone_quota": 0,
            "search_count": 30,
            "search_quota": 20,
        } | extra
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"status_code": 200, "usage": usage})
            )
        ) as c:
            row = await collectors.provider_balance("contactout", c)
        snap = sweep.snapshot_from("contactout", row)
        assert not snap.error and snap.remaining is None
        assert "email: used=10, quota=0" in snap.note
        state = policy.latest_state(
            policy.default_policy("contactout", has_key=True), snap
        )
        assert state.confidence == "informational" and state.exhausted_until is None
        old = policy.latest_state(
            policy.default_policy("contactout", has_key=True),
            snap,
            now=utcnow_naive() + timedelta(hours=7),
        )
        assert old.health == "stale" and old.exhausted_until is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status_code": 401},
        {"status_code": 200, "usage": {}},
        {"status_code": 200, "usage": {"count": True, "quota": 1}},
    ],
)
async def test_contactout_bad_stats_are_not_valid_observations(contactout_platform, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as c:
        row = await collectors.provider_balance("contactout", c)
    assert row["value"] is None and not row.get("informational")
    assert "PLATFORM-TEST" not in str(row)
    assert sweep.snapshot_from("contactout", row).error
