"""`X-Treg-Route-Confirm`: an email find whose hit counts only once a second provider agrees.

The waterfall goes on after an unconfirmed hit, each attempt billed at its own price; the first
provider's answer is returned either way, with `_treg.confirmation` saying whether another agreed."""

from __future__ import annotations

from httpx import AsyncClient

from treg.application.call import service as call_service

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_route_verify import ASK, FIND, TOMBA_FIND_PRICE, TOMBA_HIT, _relay, verifiers_on  # noqa: F401

ON = {"X-Treg-Route-Confirm": "true"}


def _hunter(email: str | None) -> tuple[int, dict]:
    return 200, {"data": {"email": email, "score": 90 if email else None, "verification": {"status": "valid" if email else None}}}


async def test_without_the_header_the_first_hit_ends_the_call(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    assert seen == ["tomba"] and "confirmation" not in r.json()["_treg"]
    assert "X-Treg-Route-Confirmed" not in r.headers


async def test_a_second_provider_with_the_same_address_confirms_the_first(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "hunter": [_hunter("Patrick@Stripe.com ")]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "hunter"], "the hit waited for another provider"
    assert d["output"]["email"] == "patrick@stripe.com" and d["_treg"]["served_by"] == "tomba.people.email.find"
    assert d["_treg"]["confirmation"] == {"field": "email", "confirmed": True, "confirmed_by": "hunter"}
    assert r.headers["X-Treg-Route-Confirmed"] == "true"
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["hit", "hit"]
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] > TOMBA_FIND_PRICE, "both finds are billed"
    assert before - await _balance(clients) == total


async def test_nobody_agreeing_returns_the_first_hit_unconfirmed(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "hunter": [_hunter("pcollison@stripe.com")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "hunter"]
    assert d["_treg"]["outcome"] == "hit" and d["output"]["email"] == "patrick@stripe.com", "never fewer answers"
    assert d["_treg"]["served_by"] == "tomba.people.email.find"
    assert d["_treg"]["confirmation"] == {"field": "email", "confirmed": False, "confirmed_by": None}
    assert r.headers["X-Treg-Route-Confirmed"] == "false"


async def test_a_miss_after_a_hit_leaves_it_unconfirmed(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "hunter": [_hunter(None)]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["outcome"] == "hit" and r.json()["_treg"]["confirmation"]["confirmed"] is False


async def test_a_miss_everywhere_carries_no_confirmation(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    miss = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [miss], "hunter": [_hunter(None)]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    assert "confirmation" not in r.json()["_treg"] and "X-Treg-Route-Confirmed" not in r.headers


async def test_the_header_needs_the_waterfall(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_confirm_unsupported"
    assert seen == [] and await _balance(clients) == before, "refused before anything is held"


async def test_a_contract_without_a_confirm_field_refuses_the_header(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    r = await clients.post("/call/treg.people.email.verify", json={"email": "p@stripe.com"}, headers=ON)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_confirm_unsupported"
    assert "treg.people.email.find" in r.json()["detail"]["message"] and seen == []
