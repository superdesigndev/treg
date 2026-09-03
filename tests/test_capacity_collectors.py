"""Unit tests for balance collectors in treg.domain.capacity.collectors.

Each test mocks the upstream API response and verifies that the collector returns the expected
{"value", "unit", "note"} dict. This tests parsing logic without hitting real APIs.
"""

from __future__ import annotations

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
    expected = {"aviato", "coresignal", "devmatch", "exa", "finnhub", "justoneapi", "marketstack", "tiingo"}
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
