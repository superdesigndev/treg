"""A routed people search neither re-bills page one for a page it cannot send, nor lets Exa's
per-result rider pass X-Treg-Route-Max-Cost on a base-price hold."""

import pytest

from treg.application.call import service
from treg.config import get_settings

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _relay_by_provider


@pytest.fixture
def exa_on(monkeypatch, platform_on):
    monkeypatch.setenv("TREG_PLATFORM_KEY_EXA", "K-exa")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "exa")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("paging", [{"page": 2}, {"offset": 100}, {"cursor": "abc"}])
async def test_paging_key_is_refused_before_any_call(clients, exa_on, monkeypatch, paging):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"*": [(200, {})]}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json={"title": "VP Sales", **paging})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "route_paging_unsupported"
    assert seen == [] and await _balance(clients) == before


def _exa(n):
    return {"results": [{"url": f"https://linkedin.com/in/p{i}", "title": f"P {i}"} for i in range(n)],
            "costDollars": {"total": round(0.007 + 0.001 * max(0, n - 10), 6)}}


@pytest.mark.parametrize("limit,cap,status,charge", [(100, "0.05", 402, 0), (10, "0.05", 200, 7_000), (100, "1", 200, 97_000)])
async def test_exa_rider_is_held_against_the_ceiling(clients, exa_on, monkeypatch, limit, cap, status, charge):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"exa": [(200, _exa(limit))]}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.people.search", json={"q": "VP Sales at fintechs", "limit": limit},
                           headers={"X-Treg-Route-Max-Cost": cap, "X-Treg-Route-Exclude": "parallel,crawl4ai"})
    assert r.status_code == status, r.text
    charged = before - await _balance(clients)
    assert charged == charge <= float(cap) * 1_000_000
    assert (seen == []) == (status == 402)
