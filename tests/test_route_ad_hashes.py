"""`X-Treg-Route-Ad-Hashes`: an email find's hit hashed for ad platforms in `_treg.ad_hashes`.

The hashes are computed by treg at no charge; the find's answer is not altered."""

from __future__ import annotations

from httpx import AsyncClient

from treg.application.call import service as call_service
from treg.domain.catalog.routing import ad_hashes

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_route_verify import ASK, FIND, TOMBA_FIND_PRICE, TOMBA_HIT, VERIFY, _relay, verifiers_on  # noqa: F401

ON = {"X-Treg-Route-Ad-Hashes": "true"}


async def test_without_the_header_there_are_no_hashes(clients: AsyncClient, verifiers_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    assert "ad_hashes" not in r.json()["_treg"]


async def test_a_hit_carries_its_hashes_at_the_find_price(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"]["email"] == "patrick@stripe.com" and d["raw"] == TOMBA_HIT[1]
    assert d["_treg"]["ad_hashes"] == {"hashed": True, **ad_hashes.hashes("patrick@stripe.com")}
    assert seen == ["tomba"], "no extra provider call"
    assert int(r.headers["X-Treg-Cost-Micro"]) == TOMBA_FIND_PRICE == before - await _balance(clients)


async def test_a_miss_says_why_there_are_no_hashes(clients: AsyncClient, verifiers_on, monkeypatch):
    miss = (200, {"data": {"email": None}})
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [miss]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["ad_hashes"] == {"hashed": False, "reason": "no_hit"}


async def test_the_header_on_a_tool_without_hashes_is_refused_unbilled(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{VERIFY}", json={"email": "p@stripe.com"}, headers=ON)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_ad_hashes_unsupported"
    assert FIND in r.json()["detail"]["message"]
    assert seen == [] and await _balance(clients) == before
