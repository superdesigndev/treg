"""Public-contract checks for the Minerva listing; upstream payloads are synthetic."""

import httpx
import pytest

from treg.domain.capacity import collectors, sweep
from treg.domain.catalog import store as catalog_store


def test_minerva_shared_key_stays_disabled_until_prices_are_verified():
    catalog = catalog_store.load(refresh=True)
    endpoints = [endpoint for endpoint in catalog.endpoints if endpoint["provider"] == "minerva"]
    assert endpoints
    assert all(not catalog.platform_eligible(endpoint) for endpoint in endpoints)
    assert all(not endpoint.get("verified") for endpoint in endpoints)


@pytest.mark.parametrize("capacity", [0, 1295])
async def test_minerva_capacity_is_informational_including_when_empty(capacity):
    def probe(request):
        assert request.method == "GET"
        assert request.url == "https://api.minerva.io/billing/usage"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json={"current_term": {"full_record_capacity_available": capacity}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._minerva(client, "test-key")
    assert row["value"] == capacity and row["unit"] == "unique records"
    snapshot = sweep.snapshot_from("minerva", row)
    assert snapshot.confidence == "informational" and not snapshot.error


async def test_minerva_unlimited_enterprise_is_not_zero_capacity():
    payload = {"current_term": None, "access_status": "active", "product_tier": "enterprise"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload))) as client:
        row = await collectors._minerva(client, "test-key")
    assert row["value"] is None and row["informational"] is True


@pytest.mark.parametrize("payload", [
    {}, {"current_term": None},
    {"current_term": {"full_record_capacity_available": True}},
    {"current_term": {"full_record_capacity_available": -1}},
    {"current_term": {"full_record_capacity_available": "1295"}},
    {"current_term": {"full_record_capacity_available": 1.5}},
])
async def test_minerva_unknown_or_malformed_capacity_is_not_zero(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ValueError, match="no valid record capacity"):
            await collectors._minerva(client, "test-key")
