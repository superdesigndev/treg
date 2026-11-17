"""`X-Treg-Route-Domain-Match: strict`: an email find looks past a hit at another mail domain.

Off by default. With it, an off-domain hit is weak: the waterfall goes on, each attempt billed at
its own price, and the first off-domain answer comes back unaltered only when nothing on-domain
turns up."""

from __future__ import annotations

from httpx import AsyncClient

from treg.application.call import service as call_service

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_route_verify import ASK, FIND, TOMBA_FIND_PRICE, _relay, verifiers_on  # noqa: F401

STRICT = {"X-Treg-Route-Domain-Match": "strict"}
TOMBA_OFF = (200, {"data": {"email": "patrick@arcinstitute.org", "score": 99, "verification": {"status": "valid"}}})
TOMBA_ON = (200, {"data": {"email": "patrick@stripe.com", "score": 99, "verification": {"status": "valid"}}})


def _hunter(email: str) -> tuple[int, dict]:
    return 200, {"data": {"email": email, "score": 90, "verification": {"status": "valid"}}}


async def test_without_the_header_an_off_domain_hit_ends_the_call(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_OFF]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    assert seen == ["tomba"] and r.json()["_treg"]["outcome"] == "hit"
    assert r.json()["_treg"]["domain_match"] is False


async def test_an_on_domain_hit_after_an_off_domain_one_wins(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_OFF], "hunter": [_hunter("patrick@stripe.com")]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=STRICT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "hunter"], "the off-domain hit did not end the call"
    assert d["output"]["email"] == "patrick@stripe.com" and d["_treg"]["served_by"] == "hunter.people.email.find"
    assert d["_treg"]["outcome"] == "hit" and d["_treg"]["domain_match"] is True
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["weak", "hit"]
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] > TOMBA_FIND_PRICE, "both finds are billed"
    assert before - await _balance(clients) == total


async def test_nothing_on_domain_returns_the_first_off_domain_answer(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_OFF], "hunter": [_hunter("pc@othercorp.example")]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=STRICT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "hunter"]
    assert d["output"]["email"] == "patrick@arcinstitute.org", "the first paid answer, unaltered"
    assert d["_treg"]["served_by"] == "tomba.people.email.find"
    assert d["_treg"]["outcome"] == "weak" and r.headers["X-Treg-Route-Outcome"] == "weak"
    assert d["_treg"]["domain_match"] is False and "different domain" in d["_treg"]["advice"]
    assert before - await _balance(clients) == int(r.headers["X-Treg-Cost-Micro"]) == d["_treg"]["charged_micro"]


async def test_an_on_domain_first_hit_ends_the_call(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_ON]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=STRICT)
    assert r.status_code == 200, r.text
    assert seen == ["tomba"] and r.json()["_treg"]["outcome"] == "hit"
    assert int(r.headers["X-Treg-Cost-Micro"]) == TOMBA_FIND_PRICE


async def test_the_header_needs_the_waterfall(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_OFF]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**STRICT, "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_domain_match_unsupported"
    assert seen == [] and await _balance(clients) == before, "refused before anything is held"


async def test_a_contract_without_domain_matching_refuses_the_header(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    r = await clients.post("/call/treg.people.email.verify", json={"email": "p@stripe.com"}, headers=STRICT)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_domain_match_unsupported"
    assert FIND in r.json()["detail"]["message"] and seen == []


async def test_an_unknown_value_is_refused(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={"X-Treg-Route-Domain-Match": "true"})
    assert r.status_code == 400 and seen == []
