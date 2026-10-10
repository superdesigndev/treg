"""VidGuy ads on demand: one fixed-price catalog row per ad type over one upstream route.

The rows share `POST /snacks/on-demand`; what separates their prices is the pinned `types`, `count`
and `render` body fields, so the shared key must refuse any other shape before reserve.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

from treg.api import app
from treg.application.call import resolve as call_resolution
from treg.application.call.types import ResolutionFailed
from treg.domain.capacity import collectors
from treg.domain.capacity import signatures as S
from treg.domain.catalog import store as catalog_store

AD_ROWS = {
    "vidguy.ads.static.from_url": ("static-ad", 8),
    "vidguy.ads.ugc.from_url": ("ugc-ad", 13),
    "vidguy.ads.greenscreen.from_url": ("greenscreen-ad", 13),
    "vidguy.ads.animated.from_url": ("animated-ad", 39),
    "vidguy.ads.music.from_url": ("music-ad", 52),
}


def test_every_vidguy_ad_row_is_platform_priced():
    """Tier 4 needs a computable USD price on every row it may serve: credits x the fx rate."""
    cat = catalog_store.load()
    for endpoint_id, (_, credits) in AD_ROWS.items():
        ep = cat.by_id[endpoint_id]
        assert cat.platform_eligible(ep), endpoint_id
        assert cat.cost_view(ep["cost"], "vidguy")["usd"] == pytest.approx(credits * 0.1)
    assert cat.platform_eligible(cat.by_id["vidguy.jobs.get"])
    # The balance read is the caller's own account, never served on treg's key.
    assert not cat.platform_eligible(cat.by_id["vidguy.account.credits"])


@pytest.mark.parametrize("endpoint_id", sorted(AD_ROWS))
def test_the_shared_key_serves_only_the_pinned_ad_shape(endpoint_id):
    ep = catalog_store.load().by_id[endpoint_id]
    ad_type = AD_ROWS[endpoint_id][0]
    call_resolution._enforce_platform_request(
        ep, b'{"shopUrl":"https://drinkolipop.com","types":["%s"],"count":1,"render":true}'
        % ad_type.encode())
    for body in (
        # a dearer ad type on a cheaper row
        b'{"shopUrl":"https://drinkolipop.com","types":["music-ad"],"count":1,"render":true}'
        if ad_type != "music-ad" else
        b'{"shopUrl":"https://drinkolipop.com","types":["static-ad"],"count":1,"render":true}',
        # several slots for the price of one
        b'{"shopUrl":"https://drinkolipop.com","types":["%s"],"count":5,"render":true}'
        % ad_type.encode(),
        # two ad types in one call
        b'{"shopUrl":"https://drinkolipop.com","types":["%s","animated-ad"],"count":1,"render":true}'
        % ad_type.encode(),
        # omitted pins fall back to VidGuy's multi-ad default pack
        b'{"shopUrl":"https://drinkolipop.com"}',
    ):
        with pytest.raises(ResolutionFailed):
            call_resolution._enforce_platform_request(ep, body)


def test_ad_rows_poll_the_job_route_until_a_terminal_status():
    cat = catalog_store.load()
    for endpoint_id in AD_ROWS:
        desc = cat.by_id[endpoint_id]["async"]
        assert desc["id_from"] == "jobId"
        assert desc["poll"] == {"endpoint": "vidguy.jobs.get", "param": {"in": "pathParams", "name": "id"}}
        assert desc["status"]["success"] == ["completed"]
        assert set(desc["status"]["failure"]) == {"failed", "cancelled"}
        assert desc["result"]["path"] == "result.renderedAssets"


async def test_vidguy_key_uses_free_balance_probe_and_rejects_a_bad_key(clients: AsyncClient, monkeypatch):
    def probe(request):
        assert request.method == "GET"
        assert request.url.host == "www.vidguy.ai"
        assert request.url.path == "/api/v1/credits"
        if request.headers["authorization"] == "Bearer bad":
            return httpx.Response(401, json={"error": "Invalid API key"})
        assert request.headers["authorization"] == "Bearer own-key"
        return httpx.Response(200, json={"balance": 120, "plan": None, "recentTransactions": []})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post("/connections/token", json={"provider": "vidguy", "token": "bad"})
        assert bad.status_code == 422
        good = await clients.post("/connections/token", json={"provider": "vidguy", "token": "own-key"})
        assert good.status_code == 200, good.text


async def test_vidguy_balance_collector_reads_the_credit_balance():
    def serve(request):
        assert request.url.path == "/api/v1/credits"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={
            "balance": 3120,
            "plan": {"type": "enterprise", "creditsIncluded": 4000, "creditsUsedThisPeriod": 880,
                     "periodStart": "2026-10-01T00:00:00Z", "periodEnd": "2026-11-01T00:00:00Z"},
            "recentTransactions": [],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        row = await collectors._vidguy(upstream, "test-key")
    assert row["value"] == 3120
    assert row["unit"] == "credits"
    assert "880 of 4000" in row["note"]


def test_vidguy_out_of_credits_reads_as_balance():
    assert S.classify("vidguy", 402, {}, b'{"error":"Insufficient credits","required":13,"balance":4}').kind == "balance"
    # A caller's bad request is not a capacity signal.
    assert S.classify("vidguy", 400, {}, b'{"error":"Invalid request","code":"shop_no_products"}') is None
