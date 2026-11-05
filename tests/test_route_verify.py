"""`X-Treg-Route-Verify`: an email find checked in the same call by `treg.people.email.verify`.

The check is an ordinary linked child call (`:v`, then `:v2`...) with its own hold, charge and
verdict. A hit it calls invalid is never returned: the waterfall asks the next provider, and when
every hit is invalid the answer is null. A check that cannot run never fails the find."""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.application.call.types import CallFailure, UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing.contracts import Check
from treg.infra.db import session_maker
from treg.domain import money as ledger
from treg.models import AsyncTaskRecord, CallRecord, Hold, LedgerEntry

from test_marketplace_call import _balance, platform_on  # noqa: F401

FIND = "treg.people.email.find"
VERIFY = "treg.people.email.verify"
ASK = {"full_name": "Patrick Collison", "domain": "stripe.com"}
ON = {"X-Treg-Route-Verify": "true"}
TOMBA_HIT = (200, {"data": {"email": "patrick@stripe.com", "score": 99, "verification": {"status": "valid"}}})
TOMBA_FIND_PRICE = 8_900
NO_HIT = {"verdict": "unknown", "checked": False, "served_by": None, "cost_micro": 0, "reason": "no_hit"}


def _bb(result: str | None, **flags) -> tuple[int, dict]:
    return 200, {"id": "bb-1", "status": "success", "email": "patrick@stripe.com", "result": result,
                 "score": 99, "is_accept_all": False, "credits_consumed": 1, **flags}


@pytest.fixture
def verifiers_on(monkeypatch, platform_on):
    for p in ("TOMBA", "BOUNCEBAN", "HUNTER"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tomba,bounceban,hunter")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _relay(answers: dict[str, list[tuple[int, dict]]], seen: list, gate: dict | None = None):
    """A fake upstream keyed by vendor host and job: `tomba` finds, `tomba-verify` checks."""
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        if before_outbound := kwargs.get("before_outbound"):
            await before_outbound()
        provider = next(p for p in ("bounceban", "tomba", "hunter") if p in upstream_url)
        key = provider + ("-verify" if "verif" in upstream_url and provider != "bounceban" else "")
        seen.append(key)
        if gate and key in gate:
            gate[key][0].set()
            await gate[key][1].wait()
        status, doc = answers[key].pop(0)
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None
        return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)
    return relay


async def _rows() -> list[CallRecord]:
    await audit.drain()
    async with session_maker() as db:
        return list((await db.execute(select(CallRecord))).scalars().all())


async def test_without_the_header_nothing_is_checked(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert r.status_code == 200, r.text
    assert "verification" not in r.json()["_treg"] and seen == ["tomba"]
    assert int(r.headers["X-Treg-Cost-Micro"]) == TOMBA_FIND_PRICE


# Fictional person and domain for the tests below; the two finders return different addresses.
EX_ASK = {"full_name": "Ada Example", "domain": "example.com"}
EX_TOMBA = (200, {"data": {"email": "ada@example.com", "score": 99, "verification": {"status": "valid"}}})
HUNTER_HIT = (200, {"data": {"email": "a.example@example.com", "score": 95, "verification": {"status": "valid"}}})


async def test_an_invalid_hit_is_dropped_and_the_next_provider_wins(clients: AsyncClient, verifiers_on, monkeypatch):
    """Even a hit the finder calls verified is checked: a finder marked addresses verified that two
    verifiers both called invalid. That address is never returned: the waterfall asks the next
    provider, whose checked-valid address is the answer. Each find and each check is billed."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "hunter": [HUNTER_HIT],
         "bounceban": [_bb("undeliverable", email="ada@example.com"), _bb("deliverable", email="a.example@example.com")]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "hunter", "bounceban"], "BounceBan first for each check"
    assert d["output"]["email"] == "a.example@example.com" and d["_treg"]["served_by"] == "hunter.people.email.find"
    assert d["_treg"]["outcome"] == "hit"
    tried = d["_treg"]["tried"]
    assert [(t["provider"], t["outcome"]) for t in tried] == [("tomba", "miss"), ("hunter", "hit")]
    assert tried[0]["detail"] == "check verdict invalid" and tried[0]["charged_micro"] == TOMBA_FIND_PRICE
    v = d["_treg"]["verification"]
    assert v["verdict"] == "valid" and v["checked"] is True and v["served_by"] == "bounceban.people.email.verify"
    checks = d["_treg"]["checks"]
    assert [c["verdict"] for c in checks] == ["invalid", "valid"] and all(c["cost_micro"] > 0 for c in checks)
    assert "advice" not in d["_treg"], "the winner was checked valid"
    finds = sum(t["charged_micro"] for t in tried)
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == finds + sum(c["cost_micro"] for c in checks)
    assert before - await _balance(clients) == total, "every find and every check settles at its own price"
    call_id = r.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        settled = {e.call_id for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"}
    assert {f"{call_id}:v:r0", f"{call_id}:v2:r0", f"{call_id}:r0"} <= settled and len(settled) == 4
    rows = {(x.endpoint_id, x.call_ref): x for x in await _rows()}
    assert rows[(FIND, call_id)].cost_charged_micro == total
    assert (VERIFY, f"{call_id}:v") in rows and (VERIFY, f"{call_id}:v2") in rows, "each check its own linked row"
    assert rows[("bounceban.people.email.verify", f"{call_id}:v:r0")].verdict == "invalid"
    assert rows[("bounceban.people.email.verify", f"{call_id}:v2:r0")].verdict == "valid"


async def test_every_hit_invalid_returns_no_email(clients: AsyncClient, verifiers_on, monkeypatch):
    """When every provider's address is invalid there is nothing to hand back: every output field
    is null, `_treg.verification` says why (`withheld` counts the dropped hits) and `tried` names
    each one. The finds and checks were real work and stay billed, each at its own price."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "hunter": [HUNTER_HIT],
         "bounceban": [_bb("undeliverable", email="ada@example.com"), _bb("undeliverable", email="a.example@example.com")]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "hunter", "bounceban"]
    assert all(v is None for v in d["output"].values()) and d["raw"] is None
    assert d["_treg"]["outcome"] == "miss" and d["_treg"]["served_by"] is None
    assert r.headers["X-Treg-Route-Outcome"] == "miss"
    assert [(t["provider"], t["outcome"], t.get("detail")) for t in d["_treg"]["tried"]] == [
        ("tomba", "miss", "check verdict invalid"), ("hunter", "miss", "check verdict invalid")]
    v = d["_treg"]["verification"]
    assert v["verdict"] == "invalid" and v["checked"] is True and v["withheld"] == 2
    assert [c["verdict"] for c in d["_treg"]["checks"]] == ["invalid", "invalid"]
    total = int(r.headers["X-Treg-Cost-Micro"])
    finds = sum(t["charged_micro"] for t in d["_treg"]["tried"])
    assert finds > TOMBA_FIND_PRICE
    assert total == d["_treg"]["charged_micro"] == finds + sum(c["cost_micro"] for c in d["_treg"]["checks"])
    assert before - await _balance(clients) == total
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
    # With the waterfall off there is no next provider: the one invalid hit is still not returned.
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [EX_TOMBA], "bounceban": [_bb("undeliverable", email="ada@example.com")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers={**ON, "X-Treg-Route-Waterfall": "0"})
    d = r.json()
    assert r.status_code == 200 and seen == ["tomba", "bounceban"]
    assert d["output"]["email"] is None and d["_treg"]["outcome"] == "miss"
    assert d["_treg"]["verification"]["withheld"] == 1 and "checks" not in d["_treg"]


async def test_an_invalid_hit_stops_at_the_cost_ceiling(clients: AsyncClient, verifiers_on, monkeypatch):
    """The next provider is asked only while it fits `X-Treg-Route-Max-Cost`; a ceiling that leaves
    no room answers null rather than the invalid address."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [EX_TOMBA], "bounceban": [_bb("undeliverable", email="ada@example.com")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers={**ON, "X-Treg-Route-Max-Cost": "0.02"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban"] and d["output"]["email"] is None
    assert d["_treg"]["capped"] is True and d["_treg"]["verification"]["verdict"] == "invalid"
    assert {t["outcome"] for t in d["_treg"]["tried"][1:]} == {"skipped"}


async def test_a_find_that_fails_after_its_check_charges_the_check_nothing(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """The check settles with the routed find, not on its own: an invalid hit, then a provider
    error, ends in a 502 that charges nothing, the check included."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("undeliverable", email="ada@example.com")],
         "hunter": [(500, {"message": "down"})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    assert r.status_code == 502, r.text
    assert seen == ["tomba", "bounceban", "hunter"]
    assert r.json()["detail"]["charged_micro"] == 0
    assert await _balance(clients) == before
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        assert not [e for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"]


async def test_cancelling_after_a_check_releases_the_check_too(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("undeliverable", email="ada@example.com")],
         "hunter": [HUNTER_HIT]}, seen, gate={"hunter": (started, release)}))
    before = await _balance(clients)
    task = asyncio.create_task(clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == ["tomba", "bounceban", "hunter"]
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        assert not [e for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"]
    assert await _balance(clients) == before


@pytest.mark.parametrize("max_cost", [None, "0.0374"])
async def test_an_address_found_invalid_stays_invalid_for_the_whole_call(
    clients: AsyncClient, verifiers_on, monkeypatch, max_cost,
):
    """A second provider returning the address the check already called invalid is not checked
    again and not returned, whether or not a second check would fit the ceiling."""
    seen = []
    same = (200, {"data": {"email": "Ada@Example.com", "score": 95, "verification": {"status": "valid"}}})
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "hunter": [same], "bounceban": [_bb("undeliverable", email="ada@example.com")]}, seen))
    headers = {**ON, **({"X-Treg-Route-Max-Cost": max_cost} if max_cost else {})}
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "hunter"], "no second check of a known-invalid address"
    assert d["output"]["email"] is None and d["_treg"]["outcome"] == "miss"
    assert d["_treg"]["verification"]["verdict"] == "invalid" and d["_treg"]["verification"]["withheld"] == 2
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["miss", "miss"]


async def test_a_pending_finder_after_invalid_checks_still_lists_them(clients: AsyncClient, verifiers_on, monkeypatch):
    """A 202 after paid checks names them in `_treg.checks`, with who served each and its cost."""
    seen = []
    monkeypatch.setenv("TREG_PLATFORM_KEY_ENROW", "PLATFORM-ENROW-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tomba,bounceban,hunter,enrow")
    get_settings.cache_clear()
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.05)
    answers = {"tomba": [EX_TOMBA], "hunter": [HUNTER_HIT],
               "bounceban": [_bb("undeliverable", email="ada@example.com"),
                             _bb("undeliverable", email="a.example@example.com")],
               "enrow": [(200, {"id": "en-1", "qualification": "ongoing"})] * 8}
    relay = _relay(answers, seen)

    async def with_enrow(request, upstream_url, tool, secrets, client, **kwargs):
        if "enrow" in upstream_url:
            if before_outbound := kwargs.get("before_outbound"):
                await before_outbound()
            seen.append("enrow")
            payload = json.dumps(answers["enrow"].pop(0)[1]).encode()

            async def stream():
                yield payload

            async def close():
                return None
            return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)
        return await relay(request, upstream_url, tool, secrets, client, **kwargs)
    monkeypatch.setattr(call_service, "relay", with_enrow)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK,
                           headers={**ON, "X-Treg-Route-Prefer": "tomba,hunter,enrow"})
    assert r.status_code == 202, r.text
    t = r.json()["_treg"]
    assert t["outcome"] == "pending" and t["served_by"] == "enrow.people.email.find"
    assert [x["outcome"] for x in t["tried"]][:2] == ["miss", "miss"]
    assert [c["verdict"] for c in t["checks"]] == ["invalid", "invalid"]
    assert all(c["served_by"] == "bounceban.people.email.verify" and c["cost_micro"] > 0 for c in t["checks"])


def test_every_check_ref_names_its_parent():
    from treg.application import call_events
    assert call_events.ref_parts("root:v") == ("root", "v", None)
    assert call_events.ref_parts("root:v2") == ("root", "v2", None)
    assert call_events.ref_parts("root:v2:r0") == ("root:v2", "r0", 0)


async def test_a_checked_hit_drops_the_advice(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    unverified = (200, {"data": {"email": "a@stripe.com", "score": 90, "verification": {"status": "accept_all"}}})
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [unverified, unverified], "bounceban": [_bb("deliverable")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.json()["_treg"]["verification"]["verdict"] == "valid" and "advice" not in r.json()["_treg"]
    r = await clients.post(f"/call/{FIND}", json=ASK)
    assert "X-Treg-Route-Verify" in r.json()["_treg"]["advice"], "the advice names the header"


@pytest.mark.parametrize(("finder_status", "result", "flags", "disagrees"), [
    ("valid", "deliverable", {}, False),
    ("valid", "risky", {"is_accept_all": True}, True),
    ("valid", "unknown", {}, True),
    ("accept_all", "risky", {"is_accept_all": True}, False),
])
async def test_the_check_wins_over_a_verified_claim_it_does_not_confirm(
    clients: AsyncClient, verifiers_on, monkeypatch, finder_status, result, flags, disagrees,
):
    """Only a `verified: true` hit can be contradicted; the finder's answer stays as it said."""
    hit = (200, {"data": {"email": "a@stripe.com", "score": 90, "verification": {"status": finder_status}}})
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [hit], "bounceban": [_bb(result, **flags)]}, []))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    d = r.json()
    assert d["output"]["verified"] is (finder_status == "valid")
    assert d["_treg"]["verification"]["checked"] is True
    advice = catalog_store.load().contracts["people.email.find"].check.disagree_advice
    assert (d["_treg"].get("advice") == advice) is disagrees
    if not disagrees:
        assert "advice" not in d["_treg"]


def test_disagree_advice_needs_a_verified_output():
    from treg.domain.catalog.routing import contracts as C
    base = {"identity": [{"email": "str"}], "output": {"email": {"type": "str"}, "verified": {"type": "bool"}}}
    check = {"endpoint": VERIFY, "field": "email"}
    with pytest.raises(ValueError, match="verified"):
        C.parse_contracts({"contracts": {"x.find": {
            **base, "output": {"email": {"type": "str"}}, "check": {**check, "disagree_advice": "x"}}}})
    c = C.parse_contracts({"contracts": {"x.find": {**base, "check": {**check, "disagree_advice": "x"}}}})
    assert c["x.find"].check.disagree_advice == "x"


async def test_a_plain_verify_call_keeps_its_own_order(clients: AsyncClient, verifiers_on, monkeypatch):
    """BounceBan is preferred inside the check only: the contract itself prefers nobody."""
    assert catalog_store.load().contracts["people.email.verify"].prefer == ()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba-verify": [(200, {"data": {"email": {"status": "valid", "score": 99}}})]}, seen))
    r = await clients.post(f"/call/{VERIFY}", json={"email": "p@stripe.com"},
                           headers={"X-Treg-Route-Prefer": "tomba"})
    assert r.status_code == 200 and seen == ["tomba-verify"], "the caller's own prefer still decides"


async def test_the_teams_own_verifier_key_goes_before_bounceban(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    hunter_valid = (200, {"data": {"status": "valid", "score": 95}})
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "hunter-verify": [hunter_valid]}, seen))
    await clients.post("/secrets", json={"name": "hunter", "value": "MY-HUNTER-KEY"})
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Exclude": "hunter"})
    assert r.status_code == 200, r.text
    v = r.json()["_treg"]["verification"]
    assert seen == ["tomba", "hunter-verify"] and v["served_by"] == "hunter.people.email.verify"
    assert v["verdict"] == "valid" and v["cost_micro"] == 0, "a team's own key is never metered"


async def test_the_callers_route_headers_steer_the_find_not_the_check(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    hunter_hit = (200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})
    monkeypatch.setattr(call_service, "relay", _relay({"hunter": [hunter_hit], "bounceban": [_bb("deliverable")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK,
                           headers={**ON, "X-Treg-Route-Prefer": "hunter", "X-Treg-Route-Exclude": "bounceban"})
    assert r.status_code == 200, r.text
    assert seen == ["hunter", "bounceban"]
    assert r.json()["_treg"]["verification"]["served_by"] == "bounceban.people.email.verify"


async def test_a_miss_is_never_checked(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    miss = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [miss]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    assert r.json()["_treg"]["verification"] == NO_HIT and seen == ["tomba"]
    # every provider missed: the same answer, nothing charged
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [miss], "hunter": [(200, {"data": {"email": None, "score": None}})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    assert r.json()["_treg"]["verification"] == NO_HIT and seen == ["tomba", "hunter"]
    assert await _balance(clients) == before


async def test_a_check_over_the_cost_limit_does_not_run_and_the_find_stands(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Max-Cost": "0.0095"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["verification"] == {
        "verdict": "unknown", "checked": False, "served_by": None, "cost_micro": 0, "reason": "over_cost_limit"}
    assert seen == ["tomba"] and int(r.headers["X-Treg-Cost-Micro"]) == TOMBA_FIND_PRICE
    assert before - await _balance(clients) == TOMBA_FIND_PRICE
    assert "advice" not in r.json()["_treg"] or "X-Treg-Route-Verify" in r.json()["_treg"]["advice"]
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        assert not [e for e in (await db.execute(select(LedgerEntry))).scalars() if ":v" in (e.call_id or "")]


async def test_every_verifier_failing_costs_the_check_nothing(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    down = (503, {"message": "down"})
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [TOMBA_HIT], "bounceban": [down], "tomba-verify": [down], "hunter-verify": [down]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    v = r.json()["_treg"]["verification"]
    assert v == {"verdict": "unknown", "checked": False, "served_by": None, "cost_micro": 0, "reason": "checker_failed"}
    assert before - await _balance(clients) == int(r.headers["X-Treg-Cost-Micro"]) == TOMBA_FIND_PRICE


async def test_every_verifier_without_a_word_is_checked_and_unknown(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [TOMBA_HIT], "bounceban": [_bb(None)],
         "tomba-verify": [(200, {"data": {"email": {"status": None}}})],
         "hunter-verify": [(200, {"data": {"status": None}})]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    v = r.json()["_treg"]["verification"]
    assert v["verdict"] == "unknown" and v["checked"] is True and "reason" not in v
    assert int(r.headers["X-Treg-Cost-Micro"]) == TOMBA_FIND_PRICE + v["cost_micro"]


async def test_a_check_still_verifying_is_pending_and_left_to_the_worker(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    verifying = (200, {"id": "bb-1", "status": "verifying", "try_again_at": 1})
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "bounceban": [verifying] + [verifying] * 5}, seen))
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.05)
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    v = r.json()["_treg"]["verification"]
    call_id = r.headers["X-Treg-Call-Id"]
    assert v["verdict"] == "unknown" and v["checked"] is True and v["pending"] is True
    assert v["call_id"] == f"{call_id}:v:r0" and v["served_by"] == "bounceban.people.email.verify"
    assert v["async"]["task_id"] == "bb-1"
    assert v["async"]["poll"]["endpoint"] == "bounceban.people.email.verify.status"
    async with session_maker() as db:
        [task] = (await db.execute(select(AsyncTaskRecord))).scalars().all()
    assert task.status == "pending" and v["cost_micro"] == task.reserved_micro
    assert int(r.headers["X-Treg-Cost-Micro"]) == r.json()["_treg"]["charged_micro"] == TOMBA_FIND_PRICE
    assert before - await _balance(clients) == TOMBA_FIND_PRICE + task.reserved_micro, "held for the worker"


async def test_cancelling_during_the_check_releases_every_hold(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [TOMBA_HIT], "bounceban": [_bb("deliverable")]}, seen, gate={"bounceban": (started, release)}))
    before = await _balance(clients)
    task = asyncio.create_task(clients.post(f"/call/{FIND}", json=ASK, headers=ON))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        assert not [e for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"]
    assert await _balance(clients) == before, "an answer never received costs nothing"


async def test_idempotent_replay_returns_the_checked_answer_for_free(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "bounceban": [_bb("deliverable")]}, seen))
    h = {**ON, "Idempotency-Key": "verify-1"}
    r1 = await clients.post(f"/call/{FIND}", json=ASK, headers=h)
    mid = await _balance(clients)
    r2 = await clients.post(f"/call/{FIND}", json=ASK, headers=h)
    assert r2.headers.get("X-Treg-Idempotent-Replay") == "true" and r2.json() == r1.json()
    assert seen == ["tomba", "bounceban"] and await _balance(clients) == mid


async def test_the_header_on_a_tool_without_a_check_is_refused_unbilled(clients: AsyncClient, verifiers_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{VERIFY}", json={"email": "p@stripe.com"}, headers=ON)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_verify_unsupported"
    assert seen == [] and await _balance(clients) == before


async def test_catalog_get_names_the_header_only_where_a_check_exists(clients: AsyncClient, verifiers_on):
    find = (await clients.get(f"/catalog/endpoints/{FIND}")).json()
    assert "X-Treg-Route-Verify" in find["routing"]["headers"]
    assert "X-Treg-Route-Verify" in find["endpoint"]["input"]["note"]
    verify = (await clients.get(f"/catalog/endpoints/{VERIFY}")).json()
    assert "X-Treg-Route-Verify" not in verify["routing"]["headers"]


def test_the_header_never_reaches_a_child():
    parent = type("P", (), {})()
    parent.input = type("I", (), {"raw_headers": ((b"x-treg-route-verify", b"true"), (b"x-other", b"1")),
                                  "caller": None, "client_ip": "", "catalog_only": False})()
    child = call_route._child_input(parent, {"method": "POST", "id": "x"}, {}, {"email": "a@b.c"})
    assert b"x-treg-route-verify" not in {k for k, _ in child.raw_headers}


@pytest.mark.parametrize("kind,reason", [
    ("route_max_cost", "over_cost_limit"), ("insufficient_balance", "spend_limit"),
    ("daily_cap_reached", "spend_limit"), ("policy_denied", "not_allowed"),
    ("capability_pinned", "not_allowed"), ("route_no_candidate", "no_checker"), ("route_failed", "checker_failed"),
])
async def test_a_refused_check_says_why_and_costs_nothing(kind, reason):
    async def refuse(child, client):
        raise CallFailure(kind, status_code=402, detail={})
    parent = type("P", (), {})()
    parent.call_ref = "c1"
    parent.meta = None
    parent.quota_admissions = set()
    parent.quota_pending = ()
    parent.input = type("I", (), {"raw_headers": (), "caller": None, "client_ip": "", "catalog_only": False})()
    v, cost = await call_route._run_check(parent, Check(endpoint=VERIFY, field="email", prefer=("bounceban",)),
                                          {"email": "a@b.c"}, None, [], refuse, None)
    assert v == {"verdict": "unknown", "checked": False, "served_by": None, "cost_micro": 0, "reason": reason}
    assert cost == 0


async def test_a_direct_check_leaves_its_hold_with_the_find(clients: AsyncClient, verifiers_on, monkeypatch):
    """A check endpoint that is not routed runs as one plain child call through its adapter; its hold
    closes with the find's (the shape the phone check uses)."""
    seen = []
    contract = catalog_store.load().contracts["people.email.find"]
    original = contract.check
    object.__setattr__(contract, "check", Check(endpoint="tomba.people.email.verify", field="email"))  # frozen
    try:
        tomba_valid = (200, {"data": {"email": {"email": "patrick@stripe.com", "status": "valid", "score": 99}}})
        monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "tomba-verify": [tomba_valid]}, seen))
        r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
        assert r.status_code == 200, r.text
        v = r.json()["_treg"]["verification"]
        assert seen == ["tomba", "tomba-verify"]
        assert v["verdict"] == "valid" and v["served_by"] == "tomba.people.email.verify" and v["cost_micro"] > 0
        call_id = r.headers["X-Treg-Call-Id"]
        async with session_maker() as db:
            assert (await db.execute(select(Hold))).scalars().all() == []
            settled = {e.call_id for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"}
        assert settled == {f"{call_id}:r0", f"{call_id}:v"}
    finally:
        object.__setattr__(contract, "check", original)


async def test_both_mcp_servers_pass_the_header_through(monkeypatch):
    """`/mcp/` and `/mcp/v2/` differ on purpose; both already relay `X-Treg-Route-*` from `headers`."""
    from contextlib import asynccontextmanager

    import httpx

    from treg import mcp

    sent: list[dict] = []

    class _Client:
        headers: dict = {}

        async def request(self, method, path, **kwargs):
            sent.append(kwargs.get("headers") or {})
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, "http://t" + path))

    @asynccontextmanager
    async def _api(_token, **_kwargs):
        yield _Client()

    async def _org(_client):
        return 1, "team", None

    monkeypatch.setattr(mcp, "_api", _api)
    monkeypatch.setattr(mcp, "_resolve_org", _org)
    ctx = type("Ctx", (), {"headers": {"authorization": "Bearer test"}})()
    await mcp.call(FIND, params=ASK, headers=ON, ctx=ctx)
    await mcp.directory_catalog_call_write(FIND, params=ASK, headers=ON, ctx=ctx)
    assert [h.get("X-Treg-Route-Verify") for h in sent] == ["true", "true"]


@pytest.fixture
def upgradable(verifiers_on):
    """Let the email find take `X-Treg-Route-Upgrade: linkedin_url` (its answer carries no URL today)."""
    contract = catalog_store.load().contracts["people.email.find"]
    original = contract.output
    object.__setattr__(contract, "output", {**original, "linkedin_url": {"type": "url"}})  # frozen
    yield
    object.__setattr__(contract, "output", original)


def _fake_upgrade(seen_remaining: list, email: str | None, cost: int = 0):
    """`_run_upgrade` stand-in: records the budget it was given and replaces the email."""
    async def run(parent, ep, key, output, remaining, execute_child, upstream_client, pending=None):
        seen_remaining.append(remaining)
        if email is None:
            return {"upgraded": False, "via": key, "reason": "over_cost_limit"}, None, 0
        return ({"upgraded": True, "via": key, "served_by": "x.people.email.find", "cost_micro": cost},
                ({**output, "email": email}, {"x": 1}, "x.people.email.find"), cost)
    return run


async def test_an_upgrade_whose_address_is_invalid_keeps_the_checked_first_answer(
    clients: AsyncClient, upgradable, monkeypatch,
):
    """The first address checked valid; the upgrade's other address is invalid. The first answer
    comes back, `tried` keeps the find a hit and `_treg.upgrade` says why the upgrade did not stand."""
    seen, budgets = [], []
    monkeypatch.setattr(call_route, "_run_upgrade", _fake_upgrade(budgets, "ada.other@example.com"))
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("deliverable", email="ada@example.com"),
                                            _bb("undeliverable", email="ada.other@example.com")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers={**ON, "X-Treg-Route-Upgrade": "linkedin_url"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "bounceban"]
    assert d["output"]["email"] == "ada@example.com" and d["_treg"]["served_by"] == "tomba.people.email.find"
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["hit"]
    assert d["_treg"]["upgrade"]["upgraded"] is False and d["_treg"]["upgrade"]["reason"] == "invalid"
    assert d["_treg"]["verification"]["verdict"] == "valid"
    assert [c["verdict"] for c in d["_treg"]["checks"]] == ["valid", "invalid"]


async def test_a_pending_checks_hold_counts_against_the_ceiling(clients: AsyncClient, upgradable, monkeypatch):
    """A check still running may settle later, so its hold is spent from Max-Cost before any later call."""
    seen, budgets = [], []
    verifying = (200, {"id": "bb-1", "status": "verifying", "try_again_at": 1})
    monkeypatch.setattr(call_route, "_run_upgrade", _fake_upgrade(budgets, None))
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [EX_TOMBA], "bounceban": [verifying] * 6}, seen))
    r = await clients.post(f"/call/{FIND}", json=EX_ASK,
                           headers={**ON, "X-Treg-Route-Upgrade": "linkedin_url", "X-Treg-Route-Max-Cost": "0.20"})
    assert r.status_code == 200, r.text
    v = r.json()["_treg"]["verification"]
    assert v["pending"] is True and v["cost_micro"] > 0
    assert budgets == [200_000 - TOMBA_FIND_PRICE - v["cost_micro"]]


async def test_a_failed_find_reports_the_released_check(clients: AsyncClient, verifiers_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("undeliverable", email="ada@example.com")],
         "hunter": [(500, {"message": "down"})]}, []))
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert detail["released_micro"] == TOMBA_FIND_PRICE + 4_000, "the check's hold is part of the release"
    assert [c["cost_micro"] for c in detail["checks"]] == [0]


async def test_an_async_invalid_check_then_a_provider_error_charges_what_it_says(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """BounceBan answered `verifying`, then invalid on the poll: its task settled the check on its
    own, so no later failure can release it. A provider error after that ends in the null miss that
    charges what was paid (find and check), never a 502 claiming nothing was charged."""
    seen = []
    verifying = (200, {"id": "bb-1", "status": "verifying", "try_again_at": 1})
    descriptor = catalog_store.load().by_id["bounceban.people.email.verify"]["async"]
    monkeypatch.setitem(descriptor, "interval", 0.05)   # poll at once, not at BounceBan's cadence
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [verifying, _bb("undeliverable", email="ada@example.com")],
         "hunter": [(500, {"message": "down"})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "bounceban", "hunter"]
    assert d["output"]["email"] is None and d["_treg"]["outcome"] == "miss"
    assert d["_treg"]["verification"]["verdict"] == "invalid"
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == before - await _balance(clients)
    assert total == TOMBA_FIND_PRICE + d["_treg"]["verification"]["cost_micro"] and total > TOMBA_FIND_PRICE


def _poll_fast(monkeypatch):
    descriptor = catalog_store.load().by_id["bounceban.people.email.verify"]["async"]
    monkeypatch.setitem(descriptor, "interval", 0.05)   # poll at once, not at BounceBan's cadence


VERIFYING = (200, {"id": "bb-1", "status": "verifying", "try_again_at": 1})


async def test_a_balance_refusal_after_a_settled_check_charges_what_it_says_once(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """The next finder is refused for balance after an async check settled: a 200 null miss that
    charges the find and the check, stored under the idempotency key, so a retry charges nothing."""
    _poll_fast(monkeypatch)
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "leave room for one find and one check", await _balance(clients) - 25_000)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [VERIFYING, _bb("undeliverable", email="ada@example.com")],
         "hunter": [HUNTER_HIT]}, seen))
    before = await _balance(clients)
    h = {**ON, "Idempotency-Key": "settled-check-1"}
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "bounceban"], "hunter was refused before any request"
    assert d["output"]["email"] is None and d["_treg"]["outcome"] == "miss"
    assert d["_treg"]["tried"][-1]["detail"] == "insufficient_balance"
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == before - await _balance(clients) > TOMBA_FIND_PRICE
    mid = await _balance(clients)
    r2 = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert r2.headers.get("X-Treg-Idempotent-Replay") == "true" and await _balance(clients) == mid


async def test_a_verifier_that_settled_before_the_check_failed_spends_the_ceiling(
    clients: AsyncClient, upgradable, monkeypatch,
):
    """BounceBan settled through its task without a verdict and the other verifiers failed: that
    money is reported on the check and spent from Max-Cost before the upgrade is budgeted."""
    _poll_fast(monkeypatch)
    seen, budgets = [], []
    down = (503, {"message": "down"})
    monkeypatch.setattr(call_route, "_run_upgrade", _fake_upgrade(budgets, None))
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [VERIFYING, _bb(None, email="ada@example.com")],
         "tomba-verify": [down], "hunter-verify": [down]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK,
                           headers={**ON, "X-Treg-Route-Upgrade": "linkedin_url", "X-Treg-Route-Max-Cost": "0.20"})
    assert r.status_code == 200, r.text
    d = r.json()
    v = d["_treg"]["verification"]
    assert v["cost_micro"] > 0, "the settled verifier is on the check"
    assert budgets == [200_000 - TOMBA_FIND_PRICE - v["cost_micro"]]
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == before - await _balance(clients) == TOMBA_FIND_PRICE + v["cost_micro"]


async def test_a_pending_check_returns_what_its_earlier_verifiers_charged():
    """The routed check's first verifier charged without a verdict and the next is still running:
    `_run_check` returns that charge as spent now, and the hold as the pending view's cost."""
    async def pending_check(child, client):
        child.routed_spent_micro = 4_000       # what run_routed records for the verifiers before
        body = json.dumps({"output": {}, "raw": {}, "_treg": {
            "outcome": "pending", "served_by": "bounceban.people.email.verify", "reserved_micro": 4_000,
            "call_ref": f"{child.call_ref}:r1", "async": {"task_id": "bb-1"}}}).encode()

        async def stream():
            yield body

        async def close():
            return None
        return UpstreamResponse(202, ((b"x-treg-cost-micro", b"4000"),), stream(), close)
    parent = type("P", (), {})()
    parent.call_ref = "c1"
    parent.meta = None
    parent.quota_admissions = set()
    parent.quota_pending = ()
    parent.input = type("I", (), {"raw_headers": (), "caller": None, "client_ip": "", "catalog_only": False})()
    v, cost = await call_route._run_check(parent, Check(endpoint=VERIFY, field="email", prefer=("bounceban",)),
                                          {"email": "ada@example.com"}, None, [], pending_check, None)
    assert v["pending"] is True and v["cost_micro"] == 4_000 and v["charged_micro"] == 4_000
    assert cost == 4_000 and call_route._held(v) == 4_000, "spent now 4,000, still held 4,000"


async def test_an_overflow_charge_before_a_failure_is_charged_once_and_reported(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """Tomba served through an overflow aggregator settles at once (not in `pending`); its address
    is invalid and the next finder fails. The call ends in the null miss that charges that, stored
    under the idempotency key: a retry is a free replay, never a second charge."""
    import httpx

    from treg.application.call import overflow
    from treg.models import OverflowRoute

    monkeypatch.setenv("TREG_OVERFLOW_MODE", "on")
    monkeypatch.setenv("TREG_OVERFLOW_KEY_MONID", "test-key")
    monkeypatch.setenv("TREG_OVERFLOW_DAILY_BUDGET_USD", "10")
    get_settings.cache_clear()
    ep = catalog_store.load().by_id["tomba.people.email.find"]
    async with session_maker() as db:
        db.add(OverflowRoute(endpoint_id=ep["id"], aggregator="monid", provider="tomba", method=ep["method"],
                             path=ep["path"], agg_slug="tomba", agg_path=ep["path"], agg_price_micro=10_000,
                             agg_unit="call", ratio=1, enabled=True))
        await db.commit()
    call_route.overflow_routes_view.invalidate()
    monkeypatch.setattr(call_route.capacity_view, "is_exhausted",
                        lambda provider, endpoint_id=None, slot=None, **_: provider == "tomba")
    seen = []

    async def monid_send(client, req):
        seen.append("monid")
        return httpx.Response(200, json={
            "runId": "run-1", "status": "COMPLETED", "output": EX_TOMBA[1], "providerResponse": {"httpStatus": 200},
            "billing": {"reportedCost": {"value": 5_500, "unit": "MICRO_DOLLAR"}}})

    monkeypatch.setattr(overflow, "_send", monid_send)
    monkeypatch.setattr(call_service, "relay", _relay(
        {"bounceban": [_bb("undeliverable", email="ada@example.com")], "hunter": [(503, {"error": "busy"})]}, seen))
    h = {**ON, "X-Treg-Route-Prefer": "tomba,hunter", "Idempotency-Key": "overflow-invalid-1"}
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["monid", "bounceban", "hunter"]
    assert d["output"]["email"] is None and d["_treg"]["outcome"] == "miss"
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == before - await _balance(clients) > 0
    mid = await _balance(clients)
    r2 = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert r2.headers.get("X-Treg-Idempotent-Replay") == "true" and await _balance(clients) == mid
    assert seen == ["monid", "bounceban", "hunter"]


async def test_a_released_checks_audit_row_says_zero(clients: AsyncClient, verifiers_on, monkeypatch):
    """The check's own routed row is written once the find decides: released with a failed find,
    it reads zero, like the wallet."""
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("undeliverable", email="ada@example.com")],
         "hunter": [(500, {"message": "down"})]}, []))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    assert r.status_code == 502, r.text
    assert await _balance(clients) == before
    rows = {x.call_ref: x for x in await _rows()}
    [ref] = [k for k in rows if k.endswith(":v")]
    assert rows[ref].cost_charged_micro == 0
    view = (await clients.get(f"/calls/{ref}")).json()
    assert view["call"]["cost_charged_micro"] == 0 and view["charged_micro"] == 0


async def test_an_async_find_whose_usage_is_still_to_come_is_held_not_charged(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """Enrow returned the email before its credits_used: the task settles later, from zero to the
    hold. The answer names it in `_treg.unsettled` and never reports the hold as charged."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ENROW", "PLATFORM-ENROW-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tomba,bounceban,hunter,enrow")
    get_settings.cache_clear()
    descriptor = catalog_store.load().by_id["enrow.people.email.find"]["async"]
    monkeypatch.setitem(descriptor, "interval", 0.05)
    answers = [(200, {"id": "en-1", "qualification": "ongoing"}),
               (200, {"id": "en-1", "qualification": "valid", "email": "ada@example.com"})]
    seen = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        if before_outbound := kwargs.get("before_outbound"):
            await before_outbound()
        assert "enrow" in upstream_url, upstream_url
        seen.append("enrow")
        payload = json.dumps(answers.pop(0)[1]).encode()

        async def stream():
            yield payload

        async def close():
            return None
        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)
    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers={"X-Treg-Route-Prefer": "enrow"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"]["email"] == "ada@example.com" and seen == ["enrow", "enrow"]
    [u] = d["_treg"]["unsettled"]
    assert u["endpoint_id"] == "enrow.people.email.find" and u["reserved_micro"] > 0
    assert int(r.headers["X-Treg-Cost-Micro"]) == d["_treg"]["charged_micro"] == 0
    assert d["_treg"]["tried"][0]["charged_micro"] == 0
    assert before - await _balance(clients) == u["reserved_micro"], "still held for the worker"


def _overflow_on(monkeypatch, endpoint_id: str, provider: str, price: int) -> None:
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "on")
    monkeypatch.setenv("TREG_OVERFLOW_KEY_MONID", "test-key")
    monkeypatch.setenv("TREG_OVERFLOW_DAILY_BUDGET_USD", "10")
    get_settings.cache_clear()
    monkeypatch.setattr(call_route.capacity_view, "is_exhausted",
                        lambda p, endpoint_id=None, slot=None, **_: endpoint_id == _overflow_on.target)
    _overflow_on.target = endpoint_id
    _overflow_on.row = dict(endpoint_id=endpoint_id, aggregator="monid", provider=provider, agg_slug=provider,
                            agg_price_micro=price, agg_unit="call", ratio=1, enabled=True)


async def _add_overflow_route() -> None:
    from treg.models import OverflowRoute
    ep = catalog_store.load().by_id[_overflow_on.row["endpoint_id"]]
    async with session_maker() as db:
        db.add(OverflowRoute(method=ep["method"], path=ep["path"], agg_path=ep["path"], **_overflow_on.row))
        await db.commit()
    call_route.overflow_routes_view.invalidate()


async def test_an_overflow_check_without_a_reported_price_is_still_released_with_a_failed_find(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """Monid reports no price, so the check's overflow hold stays open (`:v:r0:overflow`) and is
    releasable: an invalid verdict then a provider error is the usual 502 that charges nothing."""
    import httpx

    from treg.application.call import overflow
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tomba,hunter")
    _overflow_on(monkeypatch, "tomba.people.email.verify", "tomba", 10_000)
    await _add_overflow_route()

    async def monid_send(client, req):
        return httpx.Response(200, json={"runId": "run-1", "status": "COMPLETED",
                                         "output": {"data": {"email": {"status": "invalid", "score": 0}}},
                                         "providerResponse": {"httpStatus": 200}})
    monkeypatch.setattr(overflow, "_send", monid_send)
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [EX_TOMBA], "hunter": [(500, {"error": "down"})]}, []))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers={**ON, "X-Treg-Route-Prefer": "tomba,hunter"})
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["charged_micro"] == 0 and await _balance(clients) == before
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


async def test_an_async_check_billed_on_submission_by_overflow_is_charged_not_held(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """Monid bills BounceBan's `verifying` submission at once: the 202 reports it as charged, not
    as a reservation, and a replay's original cost matches what the wallet paid."""
    import httpx

    from treg.application.call import overflow
    _overflow_on(monkeypatch, "bounceban.people.email.verify", "bounceban", 6_000)
    await _add_overflow_route()
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.07)

    async def monid_send(client, req):
        return httpx.Response(200, json={
            "runId": "run-1", "status": "COMPLETED", "providerResponse": {"httpStatus": 200},
            "output": {"status": "verifying", "id": "bb-1", "email": "ada@example.com", "try_again_at": 1},
            "billing": {"reportedCost": {"value": 5_500, "unit": "MICRO_DOLLAR"}}})
    monkeypatch.setattr(overflow, "_send", monid_send)
    before = await _balance(clients)
    h = {"X-Treg-Route-Prefer": "bounceban", "Idempotency-Key": "async-overflow-1"}
    r = await clients.post(f"/call/{VERIFY}", json={"email": "ada@example.com"}, headers=h)
    assert r.status_code == 202, r.text
    t = r.json()["_treg"]
    paid = before - await _balance(clients)
    assert paid > 0 and t["reserved_micro"] == 0 and t["charged_micro"] == paid
    replay = await clients.post(f"/call/{VERIFY}", json={"email": "ada@example.com"}, headers=h)
    assert replay.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert int(replay.headers.get("X-Treg-Original-Cost-Micro") or 0) == paid
    assert before - await _balance(clients) == paid


async def test_every_unsettled_hold_of_a_pending_check_counts_against_the_ceiling(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """BounceBan reached its terminal state without settling (its hold is left) and Enrow is still
    running: the check's 202 hands back both holds, and the upgrade is budgeted net of them."""
    from test_routing import _relay_by_provider
    monkeypatch.setenv("TREG_PLATFORM_KEY_LIMADATA", "PLATFORM-LIMADATA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_ENROW", "PLATFORM-ENROW-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "limadata,bounceban,enrow")
    get_settings.cache_clear()
    first, other = "initial@example.com", "different@example.com"
    seen = []
    answers = {"limadata": [(200, {"emails": [first]}), (200, {"emails": [other]})],
               "bounceban": [(200, {"id": "bb-mixed", "status": "verifying"}),
                             _bb(None, id="bb-mixed", email=first), _bb(None, id="bb-mixed", email=first)],
               "enrow": [(200, {"id": "en-mixed", "qualification": "ongoing", "credits_used": 0.25}),
                         (200, {"id": "en-mixed", "qualification": "valid", "credits_used": 0.25})]}
    monkeypatch.setitem(catalog_store.load().by_id["bounceban.people.email.verify"]["async"], "interval", 0.05)
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.4)
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(answers, seen))
    observe = call_service.async_task_app.observe_owned_poll
    failed_once: list = []

    async def settle_times_out_once(call_id, status, raw, **kwargs):
        if not failed_once:
            failed_once.append(call_id)
            raise TimeoutError("transient async settlement DB timeout")
        return await observe(call_id, status, raw, **kwargs)
    monkeypatch.setattr(call_service.async_task_app, "observe_owned_poll", settle_times_out_once)
    cap = 202_175
    r = await clients.post("/call/treg.people.email.find.personal",
                           json={"linkedin_url": "https://www.linkedin.com/in/ada-example"},
                           headers={**ON, "X-Treg-Route-Upgrade": "email", "X-Treg-Route-Max-Cost": "0.202175"})
    assert r.status_code == 200, r.text
    for ep, job in (("bounceban.people.email.verify.status", "bb-mixed"), ("enrow.people.email.verify.get", "en-mixed")):
        assert (await clients.get(f"/call/{ep}", params={"id": job})).status_code == 200
    async with session_maker() as db:
        entries = (await db.execute(select(LedgerEntry))).scalars().all()
    assert sum(-e.amount_micro for e in entries if e.kind == "settle") <= cap


async def test_a_nested_checks_records_follow_the_money(clients: AsyncClient, verifiers_on, monkeypatch):
    """The check's call event waits for the find like its audit row: released with a failed find it
    says zero, and a batch settle that fails leaves the row at zero, as the wallet."""
    from treg import call_events as sink
    from treg.domain import money as ledger_mod
    events = []
    monkeypatch.setattr(sink, "enabled", lambda: True)
    monkeypatch.setattr(sink, "emit", lambda row, *a, **k: events.append(row))
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("undeliverable", email="ada@example.com")],
         "hunter": [(503, {"error": "busy"})]}, []))
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    assert r.status_code == 502
    check_events = [e for e in events if e.get("call_ref", "").endswith(":v") and e.get("event_kind") == "call"]
    assert check_events and all(e.get("cost_charged_micro") == 0 for e in check_events), check_events

    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("deliverable", email="ada@example.com")]}, []))
    before = await _balance(clients)

    async def fail_batch(*args, **kwargs):
        raise RuntimeError("test fault: batch transaction failed")
    with monkeypatch.context() as patch:
        patch.setattr(ledger_mod, "close_holds_in_transaction", fail_batch)
        r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    assert r.status_code == 200, r.text
    call_ref = r.headers["X-Treg-Call-Id"]
    rows = {x.call_ref: x for x in await _rows()}
    assert rows[f"{call_ref}:v"].cost_charged_micro == 0, "the settle never committed"
    assert await _balance(clients) <= before


async def test_a_concurrent_poll_that_settles_at_zero_is_what_the_find_reports(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """The caller's own poll settles Enrow's task at zero while the router is still waiting: the
    find's answer and its replay report what the ledger settled (zero), not the hold."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_ENROW", "PLATFORM-ENROW-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tomba,bounceban,hunter,enrow")
    get_settings.cache_clear()
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.6)
    waiting = asyncio.Event()
    wait = call_route.async_bridge.await_terminal

    async def observed(*args, **kwargs):
        waiting.set()
        return await wait(*args, **kwargs)
    monkeypatch.setattr(call_route.async_bridge, "await_terminal", observed)

    async def enrow(request, upstream_url, tool, secrets, client, **kwargs):
        if before_outbound := kwargs.get("before_outbound"):
            await before_outbound()
        doc = ({"id": "en-1", "qualification": "ongoing"} if request.method == "POST"
               else {"id": "en-1", "qualification": "invalid", "email": None, "credits_used": 0})
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None
        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)
    monkeypatch.setattr(call_service, "relay", enrow)
    before = await _balance(clients)
    h = {"X-Treg-Route-Prefer": "enrow", "Idempotency-Key": "concurrent-zero-1"}
    req = asyncio.create_task(clients.post(f"/call/{FIND}", json=EX_ASK, headers=h))
    await asyncio.wait_for(waiting.wait(), 3)
    assert (await clients.get("/call/enrow.people.email.find.get", params={"id": "en-1"})).status_code == 200
    r = await req
    paid = before - await _balance(clients)
    t = r.json()["_treg"]
    assert paid == 0 and (t.get("charged_micro") or 0) == 0 and not t.get("held_micro")
    replay = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert int(replay.headers.get("X-Treg-Original-Cost-Micro") or 0) == paid


@pytest.mark.parametrize("reported", [False, True])
async def test_a_pending_overflow_check_reports_what_the_wallet_paid(
    clients: AsyncClient, verifiers_on, monkeypatch, reported,
):
    """BounceBan through Monid answers `verifying`; with or without a reported price, the find's
    `charged_micro`, the wallet and the replay's original cost agree."""
    import httpx

    from treg.application.call import overflow
    _overflow_on(monkeypatch, "bounceban.people.email.verify", "bounceban", 6_000)
    await _add_overflow_route()
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.07)

    async def monid_send(client, req):
        doc = {"runId": "run-1", "status": "COMPLETED", "providerResponse": {"httpStatus": 200},
               "output": {"status": "verifying", "id": "bb-1", "email": "ada@example.com", "try_again_at": 1}}
        if reported:
            doc["billing"] = {"reportedCost": {"value": 5_500, "unit": "MICRO_DOLLAR"}}
        return httpx.Response(200, json=doc)
    monkeypatch.setattr(overflow, "_send", monid_send)
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [EX_TOMBA]}, []))
    h = {**ON, "X-Treg-Route-Prefer": "tomba", "Idempotency-Key": f"pending-overflow-{reported}"}
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert r.status_code == 200, r.text
    t = r.json()["_treg"]
    paid = before - await _balance(clients)
    assert t["charged_micro"] + t.get("held_micro", 0) == paid
    assert int(r.headers["X-Treg-Cost-Micro"]) == t["charged_micro"]
    replay = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert int(replay.headers["X-Treg-Original-Cost-Micro"]) == t["charged_micro"]


async def test_a_cancel_after_the_settle_committed_records_what_was_charged(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """Cancelled while the settle's session closes, after its commit: the check's audit row and
    call event say what the ledger charged, not zero."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from treg import call_events as sink
    from treg.domain import money as ledger_mod
    events = []
    monkeypatch.setattr(sink, "enabled", lambda: True)
    monkeypatch.setattr(sink, "emit", lambda row, *a, **k: events.append(row))
    sessions = []
    close_holds = ledger_mod.close_holds_in_transaction

    async def closing(db, closings):
        sessions.append(db)
        return await close_holds(db, closings)
    monkeypatch.setattr(ledger_mod, "close_holds_in_transaction", closing)
    entered, proceed = asyncio.Event(), asyncio.Event()
    session_close = AsyncSession.close

    async def slow_close(db):
        if db in sessions:
            entered.set()
            await proceed.wait()
        await session_close(db)
    monkeypatch.setattr(AsyncSession, "close", slow_close)
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("deliverable", email="ada@example.com")]}, []))
    before = await _balance(clients)
    req = asyncio.create_task(clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON))
    await asyncio.wait_for(entered.wait(), 4)
    req.cancel()
    await asyncio.sleep(0)
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await req
    await asyncio.sleep(0.05)
    paid = before - await _balance(clients)
    assert paid > TOMBA_FIND_PRICE, "the settle committed: find and check were charged"
    check = next(x for x in await _rows() if x.endpoint_id == VERIFY)
    assert check.cost_charged_micro == paid - TOMBA_FIND_PRICE
    assert [e.get("cost_charged_micro") for e in events
            if e.get("call_ref") == check.call_ref and e.get("event_kind") == "call"] == [check.cost_charged_micro]


async def test_an_unreadable_ledger_never_makes_a_zero_receipt(clients: AsyncClient, verifiers_on, monkeypatch):
    """The closing ledger read fails every time: the answer, its header, its replay and its audit
    row keep what the call computed, which is what the wallet paid."""
    async def unreadable(ids):
        raise TimeoutError("injected read timeout")
    monkeypatch.setattr(call_route, "_ledger_view", unreadable)
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("deliverable", email="ada@example.com")]}, []))
    h = {**ON, "Idempotency-Key": "unreadable-1"}
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert r.status_code == 200, r.text
    paid = before - await _balance(clients)
    assert paid > TOMBA_FIND_PRICE
    assert r.json()["_treg"]["charged_micro"] == int(r.headers["X-Treg-Cost-Micro"]) == paid
    replay = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert int(replay.headers["X-Treg-Original-Cost-Micro"]) == paid
    row = {x.call_ref: x for x in await _rows()}[r.headers["X-Treg-Call-Id"]]
    assert row.cost_charged_micro == paid


async def test_a_cancel_while_reading_the_ledger_keeps_the_paid_result_for_the_key(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """Both calls were paid when the request is cancelled during the closing read: the read still
    finishes, the result is kept under the idempotency key, and a retry replays it for free."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA] * 2, "bounceban": [_bb("deliverable", email="ada@example.com")] * 2}, seen))
    entered, proceed = asyncio.Event(), asyncio.Event()
    read = call_route._ledger_view

    async def paused(ids):
        entered.set()
        await proceed.wait()
        return await read(ids)
    monkeypatch.setattr(call_route, "_ledger_view", paused)
    h = {**ON, "Idempotency-Key": "cancel-read-1"}
    before = await _balance(clients)
    task = asyncio.create_task(clients.post(f"/call/{FIND}", json=EX_ASK, headers=h))
    await asyncio.wait_for(entered.wait(), 3)
    paid = before - await _balance(clients)
    task.cancel()
    await asyncio.sleep(0)
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert r.headers.get("X-Treg-Idempotent-Replay") == "true", r.text
    assert seen == ["tomba", "bounceban"], "no provider asked twice"
    assert before - await _balance(clients) == paid
    assert int(r.headers["X-Treg-Original-Cost-Micro"]) == paid


async def test_the_closing_read_is_one_statement_over_exact_hold_ids(clients: AsyncClient, verifiers_on, monkeypatch):
    seen_ids = []
    read = call_route._ledger_view

    async def spy(ids):
        seen_ids.append(list(ids))
        return await read(ids)
    monkeypatch.setattr(call_route, "_ledger_view", spy)
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [EX_TOMBA], "bounceban": [_bb("deliverable", email="ada@example.com")]}, []))
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=ON)
    root = r.headers["X-Treg-Call-Id"]
    [ids] = seen_ids
    assert {root, f"{root}:r0", f"{root}:v", f"{root}:v:r0", f"{root}:r0:overflow"} <= set(ids)
    assert all(i == root or i.startswith(root + ":") for i in ids)


async def test_a_202_after_a_settle_reports_it_in_body_header_replay_and_event(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """Enrow's task settles (2,175) just before the closing read, while the find answers 202:
    the body, the cost header, the replay and the call event all say 2,175, the wallet's figure."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from treg import call_events as sink
    monkeypatch.setenv("TREG_PLATFORM_KEY_ENROW", "PLATFORM-ENROW-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "enrow")
    get_settings.cache_clear()
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.07)
    from test_routing import _relay_by_provider
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"enrow": [
        (200, {"id": "split-1", "qualification": "ongoing"}),
        (200, {"id": "split-1", "qualification": "invalid", "credits_used": 0.25})]}, []))
    events = []
    monkeypatch.setattr(sink, "enabled", lambda: True)
    monkeypatch.setattr(sink, "emit", lambda row, *a, **k: events.append(row))
    execute = AsyncSession.execute
    polled = []

    async def poll_first(db, statement, *args, **kwargs):
        if not polled and "UNION ALL" in str(statement) and "ledgerentry" in str(statement).lower():
            polled.append(True)
            assert (await clients.get("/call/enrow.people.email.find.get", params={"id": "split-1"})).status_code == 200
        return await execute(db, statement, *args, **kwargs)
    monkeypatch.setattr(AsyncSession, "execute", poll_first)
    h = {"X-Treg-Route-Prefer": "enrow", "Idempotency-Key": "split-1"}
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert polled and r.status_code == 202, r.text
    t = r.json()["_treg"]
    paid = before - await _balance(clients)
    assert paid == t["charged_micro"] == int(r.headers["X-Treg-Cost-Micro"]) > 0
    assert not t.get("held_micro") and t["reserved_micro"] == 0
    replay = await clients.post(f"/call/{FIND}", json=EX_ASK, headers=h)
    assert int(replay.headers["X-Treg-Original-Cost-Micro"]) == paid
    root = r.headers["X-Treg-Call-Id"]
    assert [e.get("cost_charged_micro") for e in events
            if e.get("call_ref") == root and e.get("event_kind") == "call"] == [paid]
