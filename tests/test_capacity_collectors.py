"""Unit tests for balance collectors in treg.domain.capacity.collectors.

Each test mocks the upstream API response and verifies that the collector returns the expected
{"value", "unit", "note"} dict. This tests parsing logic without hitting real APIs.
"""

from __future__ import annotations

from datetime import timedelta
from treg.domain.capacity import policy, sweep
from treg.timeutil import utcnow_naive
from treg.domain.capacity import collectors
import httpx
import pytest


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
    expected = {"aviato", "coresignal", "exa", "finnhub", "justoneapi", "marketstack", "tiingo"}
    assert expected == set(collectors.NO_BALANCE_API.keys())


def test_implemented_collectors_are_registered_and_do_not_overlap_absent_list():
    """A collector that parses a vendor must be on BALANCE_ROUTES, and a provider
    cannot be both 'we collect' and 'there is no balance API'."""
    for provider in ("akta", "brightdata", "crustdata"):
        assert provider in collectors.BALANCE_ROUTES
        assert provider not in collectors.NO_BALANCE_API
    overlap = set(collectors.BALANCE_ROUTES.keys()) & set(collectors.NO_BALANCE_API.keys())
    assert not overlap, f"Providers in both maps: {overlap}"


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
