"""Super Carl bills completed searches, including empty results, but not clarifications."""

from __future__ import annotations

import json

import pytest

from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store


SEARCH_ENDPOINTS = (
    ("supercarl.people.search", "users"),
    ("supercarl.people.search.preview", "users"),
    ("supercarl.companies.search", "companies"),
    ("supercarl.companies.jobs.search", "results"),
    ("supercarl.linkedin.search.posts", "results"),
    ("supercarl.people.search.from_posts", "people"),
)
SEARCH_CREDIT_MICRO = 99_000


@pytest.fixture
def supercarl_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_SUPERCARL", "TEST-SUPERCARL-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "supercarl")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("endpoint_id,row_field", SEARCH_ENDPOINTS)
@pytest.mark.parametrize("outcome,expected_charge", [
    ("clarification", 0),
    ("hit", SEARCH_CREDIT_MICRO),
    ("empty", SEARCH_CREDIT_MICRO),
])
async def test_search_outcome_settles_the_real_catalog_price(
    clients, monkeypatch, supercarl_platform, endpoint_id, row_field, outcome, expected_charge,
):
    # Synthetic envelopes preserve the observed billing distinction without storing private rows.
    if outcome == "clarification":
        payload = {
            "success": False,
            "entity_resolution": {"status": "needs_clarification"},
            "next_actions": [{"type": "ASK_USER"}],
        }
    else:
        rows = [{"id": "fixture-row-1"}, {"id": "fixture-row-2"}] if outcome == "hit" else []
        payload = {"success": True, row_field: rows, "total": len(rows)}
        if endpoint_id.endswith("from_posts"):
            payload["results"] = rows
    upstream_body = json.dumps(payload).encode()

    async def relay(*args, **kwargs):
        async def stream():
            yield upstream_body

        async def close():
            return None

        return UpstreamResponse(200, (), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    endpoint = catalog_store.load().by_id[endpoint_id]
    request_body = dict(endpoint["test_request"]["body"])
    # Multiple requested/returned rows must still cost a single completed search credit.
    for field in ("limit", "preview_limit", "people_limit"):
        if field in request_body:
            request_body[field] = 2

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    balance_path = f"/orgs/{org_id}/balance"
    before = (await clients.get(balance_path)).json()["balance_micro"]

    response = await clients.post(f"/call/{endpoint_id}", json=request_body)

    assert response.status_code == 200, response.text
    assert response.json() == payload
    assert response.headers["X-Treg-Cost-Micro"] == str(expected_charge)
    after = (await clients.get(balance_path)).json()
    assert after["balance_micro"] == before - expected_charge
    # All three outcomes must close the reserved hold, including a zero-cost clarification.
    assert [entry["kind"] for entry in after["entries"]["items"]][:2] == ["settle", "reserve"]
