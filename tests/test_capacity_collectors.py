"""Unit tests for balance collectors in treg.domain.capacity.collectors.

Each test mocks the upstream API response and verifies that the collector returns the expected
{"value", "unit", "note"} dict. This tests parsing logic without hitting real APIs.
"""

from __future__ import annotations

import httpx
import pytest

from treg.domain.capacity import collectors


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


class SequentialMockClient:
    """A mock client that returns responses in sequence, for retry testing."""

    def __init__(self, responses: list[MockResponse]):
        self._responses = responses
        self._call_count = 0

    async def get(self, url, **kwargs):
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp

    @property
    def call_count(self):
        return self._call_count


# ---- _get retry behavior ----------------------------------------------------------------

async def test_get_retries_on_500_then_succeeds(monkeypatch):
    """_get should retry on transient 500, then succeed on 200."""
    monkeypatch.setattr("treg.domain.capacity.collectors._RETRY_DELAYS", (0, 0, 0))
    responses = [
        MockResponse({}, status_code=500),
        MockResponse({"ok": True}, status_code=200),
    ]
    client = SequentialMockClient(responses)
    result = await collectors._get(client, "https://example.com/test")
    assert result == {"ok": True}
    assert client.call_count == 2


async def test_get_retries_exhausted_raises(monkeypatch):
    """_get should raise after all retries exhausted."""
    monkeypatch.setattr("treg.domain.capacity.collectors._RETRY_DELAYS", (0, 0, 0))
    responses = [
        MockResponse({}, status_code=500),
        MockResponse({}, status_code=502),
        MockResponse({}, status_code=503),
    ]
    client = SequentialMockClient(responses)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await collectors._get(client, "https://example.com/test")
    assert exc_info.value.response.status_code == 503
    assert client.call_count == 3


async def test_get_does_not_retry_non_transient_status(monkeypatch):
    """_get should raise immediately on non-transient status like 401."""
    monkeypatch.setattr("treg.domain.capacity.collectors._RETRY_DELAYS", (0, 0, 0))
    responses = [
        MockResponse({}, status_code=401),
        MockResponse({"ok": True}, status_code=200),
    ]
    client = SequentialMockClient(responses)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await collectors._get(client, "https://example.com/test")
    assert exc_info.value.response.status_code == 401
    assert client.call_count == 1


# ---- _oceanio 404 retry -----------------------------------------------------------------

async def test_oceanio_retries_on_404_then_succeeds(monkeypatch):
    """Ocean.io collector should retry once on 404 (their observed flake), then succeed.

    404 is NOT in _TRANSIENT_STATUSES (those are server errors like 500), so _get raises
    immediately. _oceanio then catches the 404, sleeps 1.5s, and calls _get one more time.
    """
    async def mock_sleep(_):
        pass

    monkeypatch.setattr("treg.domain.capacity.collectors.asyncio.sleep", mock_sleep)

    call_count = 0
    responses = [
        MockResponse({}, status_code=404),  # First _get call raises immediately
        MockResponse({"credits": {"oneTime": 100, "recurrent": 215}, "dailyLimitRateLeft": 50}),
    ]

    class OceanioMockClient:
        async def get(self, url, **kwargs):
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

    client = OceanioMockClient()
    result = await collectors._oceanio(client, "test-key")

    assert result["value"] == 315
    assert result["unit"] == "credits"
    assert "100 one-time + 215 recurring" in result["note"]
    assert call_count == 2  # One 404, then one success


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


# ---- NO_BALANCE_API entries -------------------------------------------------------------

def test_no_balance_api_entries_have_meaningful_notes():
    """Each NO_BALANCE_API entry should have a meaningful note explaining why."""
    for provider, note in collectors.NO_BALANCE_API.items():
        assert len(note) > 20, f"{provider} has too short a note"
        assert "dashboard" in note.lower() or "no" in note.lower(), (
            f"{provider} note should mention dashboard or explain absence"
        )


def test_no_balance_api_includes_expected_providers():
    """Verify the vendors that have no free balance API are documented."""
    expected = {"aviato", "coresignal", "exa", "finnhub", "justoneapi", "marketstack", "tiingo"}
    assert expected == set(collectors.NO_BALANCE_API.keys())


def test_balance_routes_includes_new_collectors():
    """Verify the newly implemented collectors are registered."""
    assert "akta" in collectors.BALANCE_ROUTES
    assert "brightdata" in collectors.BALANCE_ROUTES
    assert "crustdata" in collectors.BALANCE_ROUTES


def test_no_overlap_between_balance_routes_and_no_balance_api():
    """A provider should be in exactly one of BALANCE_ROUTES or NO_BALANCE_API, not both."""
    overlap = set(collectors.BALANCE_ROUTES.keys()) & set(collectors.NO_BALANCE_API.keys())
    assert not overlap, f"Providers in both maps: {overlap}"
