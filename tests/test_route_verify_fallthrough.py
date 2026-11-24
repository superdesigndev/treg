"""`X-Treg-Route-Verify-Fallthrough`: continue the find waterfall when a hit checks invalid.

Opt-in only. Without the header, `X-Treg-Route-Verify` leaves the first hit intact even when its
check returns `invalid`. With it, a definite invalid verdict (`invalid` on email finds, `dead` on
phone finds) treats that hit as a miss for routing purposes and keeps trying providers within
`X-Treg-Route-Max-Cost`, recording each attempt's check verdict in `_treg.tried`."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import contracts as C
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, Hold, LedgerEntry

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_route_verify import (  # noqa: F401
    ASK,
    FIND,
    TOMBA_FIND_PRICE,
    TOMBA_HIT,
    VERIFY,
    _bb,
    _relay,
    _rows,
    verifiers_on,
)
from test_route_verify_phone import HLR, _hlr, _relay as _phone_relay, _tomba  # noqa: F401

ON = {"X-Treg-Route-Verify": "true", "X-Treg-Route-Verify-Fallthrough": "true"}


def _hunter(email: str | None, *, verified: bool = True) -> tuple[int, dict]:
    return 200, {
        "data": {
            "email": email,
            "score": 90 if email else None,
            "verification": {"status": ("valid" if verified else "accept_all") if email else None},
        }
    }


def _tomba_email(email: str | None, *, status: str = "valid") -> tuple[int, dict]:
    return 200, {"data": {"email": email, "score": 95 if email else None, "verification": {"status": status if email else None}}}


async def test_without_the_header_an_invalid_hit_still_ends_the_call(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay(
        {"tomba": [TOMBA_HIT], "bounceban": [_bb("undeliverable")], "hunter": [_hunter("good@stripe.com")]}, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={"X-Treg-Route-Verify": "true"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban"]
    assert d["output"]["email"] == "patrick@stripe.com"
    assert d["_treg"]["served_by"] == "tomba.people.email.find"
    assert d["_treg"]["verification"]["verdict"] == "invalid"


async def test_an_invalid_first_hit_falls_through_to_a_valid_second_hit(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("bad@stripe.com")],
        "hunter": [_hunter("patrick@stripe.com")],
        "bounceban": [_bb("undeliverable"), _bb("deliverable")],
    }, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "hunter", "bounceban"]
    assert d["output"]["email"] == "patrick@stripe.com"
    assert d["_treg"]["served_by"] == "hunter.people.email.find"
    assert d["_treg"]["outcome"] == "hit"
    assert "advice" not in d["_treg"]
    v = d["_treg"]["verification"]
    assert v["verdict"] == "valid" and v["checked"] is True and v["served_by"] == "bounceban.people.email.verify"

    tried = d["_treg"]["tried"]
    assert len(tried) == 2
    assert tried[0]["endpoint_id"] == "tomba.people.email.find"
    assert tried[0]["outcome"] == "miss"
    assert tried[0]["detail"] == "verification verdict invalid"
    assert tried[0]["charged_micro"] == TOMBA_FIND_PRICE
    assert tried[0]["verification"]["verdict"] == "invalid"
    assert tried[0]["verification"]["checked"] is True
    assert tried[1]["endpoint_id"] == "hunter.people.email.find"
    assert tried[1]["outcome"] == "hit"
    assert tried[1]["verification"]["verdict"] == "valid"

    total = int(r.headers["X-Treg-Cost-Micro"])
    find_sum = sum(t["charged_micro"] for t in tried)
    check_sum = tried[0]["verification"]["cost_micro"] + tried[1]["verification"]["cost_micro"]
    assert total == d["_treg"]["charged_micro"] == find_sum + check_sum
    assert before - await _balance(clients) == total

    call_id = r.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        settled = {e.call_id for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"}
    assert settled == {f"{call_id}:r0", f"{call_id}:r0:v:r0", f"{call_id}:r1", f"{call_id}:r1:v:r0"}
    rows = {(x.endpoint_id, x.call_ref): x for x in await _rows()}
    assert rows[(FIND, call_id)].cost_charged_micro == total
    assert (VERIFY, f"{call_id}:r0:v") in rows and (VERIFY, f"{call_id}:r1:v") in rows
    assert rows[("bounceban.people.email.verify", f"{call_id}:r0:v:r0")].verdict == "invalid"
    assert rows[("bounceban.people.email.verify", f"{call_id}:r1:v:r0")].verdict == "valid"


async def test_fallthrough_header_alone_enables_verification_and_fallthrough(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("bad@stripe.com")],
        "hunter": [_hunter("patrick@stripe.com")],
        "bounceban": [_bb("undeliverable"), _bb("deliverable")],
    }, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={"X-Treg-Route-Verify-Fallthrough": "true"})
    assert r.status_code == 200, r.text
    assert seen == ["tomba", "bounceban", "hunter", "bounceban"]
    assert r.json()["output"]["email"] == "patrick@stripe.com"


@pytest.mark.parametrize(("bb_result", "flags", "expected_verdict"), [
    ("deliverable", {}, "valid"),
    ("risky", {"is_accept_all": True}, "catch_all"),
    ("risky", {}, "risky"),
    ("unknown", {}, "unknown"),
    (None, {}, "unknown"),
])
async def test_non_invalid_verdicts_do_not_trigger_fallthrough(
    clients: AsyncClient, verifiers_on, monkeypatch, bb_result, flags, expected_verdict,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("maybe@stripe.com", status="accept_all")],
        "hunter": [_hunter("never-called@stripe.com")],
        "bounceban": [_bb(bb_result, **flags)],
        "tomba-verify": [(200, {"data": {"email": {"status": None}}})],
        "hunter-verify": [(200, {"data": {"status": None}})],
    }, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "hunter" not in seen
    assert d["output"]["email"] == "maybe@stripe.com"
    assert d["_treg"]["served_by"] == "tomba.people.email.find"
    assert d["_treg"]["verification"]["verdict"] == expected_verdict


async def test_failed_or_over_budget_or_pending_check_does_not_fall_through(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    # 1. All verifiers fail -> checker_failed, stops at tomba
    seen = []
    down = (503, {"message": "down"})
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [TOMBA_HIT], "hunter": [_hunter("never@stripe.com")],
        "bounceban": [down], "tomba-verify": [down], "hunter-verify": [down],
    }, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200 and "hunter" not in seen
    assert r.json()["output"]["email"] == "patrick@stripe.com"
    assert r.json()["_treg"]["verification"]["reason"] == "checker_failed"

    # 2. Check over remaining max-cost -> over_cost_limit, stops at tomba
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [TOMBA_HIT], "hunter": [_hunter("never@stripe.com")],
    }, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Max-Cost": "0.0095"})
    assert r.status_code == 200 and seen == ["tomba"]
    assert r.json()["output"]["email"] == "patrick@stripe.com"
    assert r.json()["_treg"]["verification"]["reason"] == "over_cost_limit"

    # 3. Pending async check -> pending: true, stops at tomba
    seen.clear()
    verifying = (200, {"id": "bb-1", "status": "verifying", "try_again_at": 1})
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [TOMBA_HIT], "hunter": [_hunter("never@stripe.com")],
        "bounceban": [verifying] * 6,
    }, seen))
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.05)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200 and "hunter" not in seen
    assert r.json()["_treg"]["verification"]["pending"] is True
    async with session_maker() as db:
        assert len((await db.execute(select(AsyncTaskRecord))).scalars().all()) == 1


async def test_all_remaining_providers_missing_after_invalid_hit_returns_miss(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("avelasco@omilia.com")],
        "bounceban": [_bb("undeliverable")],
        "hunter": [_hunter(None)],
    }, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "hunter"]
    assert d["output"]["email"] is None and d["raw"] is None
    assert d["_treg"]["served_by"] is None and d["_treg"]["outcome"] == "miss"
    assert r.headers["X-Treg-Route-Outcome"] == "miss"
    assert d["_treg"]["verification"]["verdict"] == "invalid"
    assert d["_treg"]["verification"]["checked"] is True
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["miss", "miss"]
    assert d["_treg"]["tried"][0]["verification"]["verdict"] == "invalid"
    assert d["_treg"]["tried"][1]["charged_micro"] == 0  # hunter miss is free
    total = TOMBA_FIND_PRICE + d["_treg"]["tried"][0]["verification"]["cost_micro"]
    assert int(r.headers["X-Treg-Cost-Micro"]) == d["_treg"]["charged_micro"] == total
    assert before - await _balance(clients) == total
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


async def test_every_provider_invalid_returns_miss_and_bills_all_checks(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("bad1@stripe.com")],
        "hunter": [_hunter("bad2@stripe.com")],
        "bounceban": [_bb("undeliverable"), _bb("undeliverable")],
    }, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "hunter", "bounceban"]
    assert d["_treg"]["outcome"] == "miss" and d["output"]["email"] is None
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["miss", "miss"]
    assert [t["verification"]["verdict"] for t in d["_treg"]["tried"]] == ["invalid", "invalid"]
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == before - await _balance(clients)
    assert total > TOMBA_FIND_PRICE


async def test_error_after_invalid_hit_returns_incomplete_miss_and_keeps_first_bill(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("bad@stripe.com")],
        "bounceban": [_bb("undeliverable")],
        "hunter": [(503, {"error": "down"})],
    }, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["_treg"]["outcome"] == "miss" and d["_treg"]["incomplete"] is True
    assert r.headers["X-Treg-Route-Incomplete"] == "true"
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["miss", "error"]
    total = TOMBA_FIND_PRICE + d["_treg"]["tried"][0]["verification"]["cost_micro"]
    assert before - await _balance(clients) == total


async def test_max_cost_caps_fallthrough_before_unaffordable_next_finder(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("bad@stripe.com")],
        "bounceban": [_bb("undeliverable")],
        "hunter": [_hunter("never@stripe.com")],
    }, seen))
    before = await _balance(clients)
    # Fits tomba ($0.0089) + bounceban (~$0.003 with reserve margin), not hunter on top.
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Max-Cost": "0.014"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban"]
    assert d["_treg"]["outcome"] == "miss" and d["_treg"]["capped"] is True
    assert r.headers["X-Treg-Route-Capped"] == "true"
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["miss", "skipped"]
    assert "max cost" in d["_treg"]["tried"][1]["detail"]
    assert before - await _balance(clients) == d["_treg"]["charged_micro"]


async def test_domain_match_strict_and_verify_fallthrough_compose(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    # 1. Off-domain invalid hit is discarded (not kept in off_domain fallback).
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("bad@former.example")],
        "bounceban": [_bb("undeliverable")],
        "hunter": [_hunter(None)],
    }, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Domain-Match": "strict"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["outcome"] == "miss" and r.json()["output"]["email"] is None

    # 2. Off-domain valid hit is kept as weak and NOT checked a second time when hunter misses.
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("patrick@former.example")],
        "bounceban": [_bb("deliverable")],
        "hunter": [_hunter(None)],
    }, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Domain-Match": "strict"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "hunter"]
    assert d["_treg"]["outcome"] == "weak" and d["output"]["email"] == "patrick@former.example"
    assert d["_treg"]["verification"]["verdict"] == "valid"


async def test_confirm_and_verify_fallthrough_compose(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("bad@stripe.com")],
        "hunter": [_hunter("good@stripe.com")],
        "bounceban": [_bb("undeliverable"), _bb("deliverable")],
    }, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Confirm": "true"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban", "hunter", "bounceban"]
    assert d["output"]["email"] == "good@stripe.com"
    assert d["_treg"]["confirmation"] == {"field": "email", "confirmed": False, "confirmed_by": None}
    assert d["_treg"]["verification"]["verdict"] == "valid"


async def test_phone_find_falls_through_on_dead_line_and_stops_on_not_international(
    clients: AsyncClient, platform_on, monkeypatch,
):
    for p in ("QUICKENRICH", "TOMBA", "HLRLOOKUP"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HLRLOOKUP_SECRET", "PLATFORM-HLR-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "quickenrich,tomba,hlrlookup")
    get_settings.cache_clear()
    try:
        seen = []
        qe_hit = (200, {"success": True, "message": "Phone found", "code": 200,
                          "data": {"employee_phone": "4155550000", "employee_phone_type": "mobile", "country_code": "US"},
                          "meta": {"credits_used": 1}})
        monkeypatch.setattr(call_service, "relay", _phone_relay({
            "quickenrich": [qe_hit],
            "tomba": [_tomba("+14155550142")],
            "hlrlookup": [_hlr("DEAD", 1), _hlr("LIVE", 1)],
        }, seen))
        before = await _balance(clients)
        r = await clients.post(
            "/call/treg.people.phone.find",
            json={"linkedin_url": "https://www.linkedin.com/in/example"},
            headers={**ON, "X-Treg-Route-Prefer": "quickenrich,tomba"},
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert [p for p, _ in seen] == ["quickenrich", "hlrlookup", "tomba", "hlrlookup"]
        assert d["output"]["phone"] == "+14155550142"
        assert d["_treg"]["served_by"] == "tomba.people.phone.find"
        assert d["_treg"]["verification"]["verdict"] == "live"
        assert d["_treg"]["tried"][0]["outcome"] == "miss"
        assert d["_treg"]["tried"][0]["verification"]["verdict"] == "dead"
        call_id = r.headers["X-Treg-Call-Id"]
        async with session_maker() as db:
            assert (await db.execute(select(Hold))).scalars().all() == []
            settled = {e.call_id for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"}
        assert settled == {f"{call_id}:r0", f"{call_id}:r0:v", f"{call_id}:r1", f"{call_id}:r1:v"}
        assert before - await _balance(clients) == d["_treg"]["charged_micro"]

        # Non-international number -> skipped check (not dead), does not fall through
        seen.clear()
        qe_gb = (200, {"success": True, "message": "Phone found", "code": 200,
                         "data": {"employee_phone": "4155550142", "employee_phone_type": "mobile", "country_code": "GB"},
                         "meta": {"credits_used": 1}})
        monkeypatch.setattr(call_service, "relay", _phone_relay({"quickenrich": [qe_gb]}, seen))
        r = await clients.post(
            "/call/treg.people.phone.find",
            json={"linkedin_url": "https://www.linkedin.com/in/example"},
            headers=ON,
        )
        assert r.status_code == 200, r.text
        assert [p for p, _ in seen] == ["quickenrich"]
        assert r.json()["_treg"]["verification"]["reason"] == "not_international"
    finally:
        get_settings.cache_clear()


async def test_unsupported_or_contradictory_headers_are_refused_unbilled(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({}, seen))
    before = await _balance(clients)

    # 1. Endpoint without check fallthrough
    r = await clients.post(f"/call/{VERIFY}", json={"email": "p@stripe.com"}, headers=ON)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_verify_fallthrough_unsupported"

    # 2. Waterfall disabled
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={**ON, "X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_verify_fallthrough_unsupported"

    # 3. Verify explicitly disabled
    r = await clients.post(f"/call/{FIND}", json=ASK, headers={
        "X-Treg-Route-Verify": "false",
        "X-Treg-Route-Verify-Fallthrough": "true",
    })
    assert r.status_code == 422 and r.json()["detail"]["error"] == "route_verify_fallthrough_unsupported"

    assert seen == [] and await _balance(clients) == before


async def test_insufficient_balance_on_next_finder_keeps_already_settled_invalid_check(
    clients: AsyncClient, verifiers_on, monkeypatch,
):
    """If check 0 already settled during fallthrough, insufficient_balance on finder 1 must not 402
    with charged_micro=0; it stops cleanly as a miss accounting for what already ran."""
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    before = await _balance(clients)
    # Leave just enough for tomba ($0.0089) + bounceban (~$0.002 + margin), not enough for hunter.
    keep = 14_000
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "drain for test", before - keep)
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({
        "tomba": [_tomba_email("bad@stripe.com")],
        "bounceban": [_bb("undeliverable")],
        "hunter": [_hunter("never@stripe.com")],
    }, seen))
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert seen == ["tomba", "bounceban"]
    assert d["_treg"]["outcome"] == "miss"
    assert d["_treg"]["tried"][0]["outcome"] == "miss"
    assert d["_treg"]["tried"][1]["outcome"] == "skipped"
    assert "insufficient_balance" in d["_treg"]["tried"][1]["detail"]
    assert d["_treg"]["charged_micro"] == TOMBA_FIND_PRICE + d["_treg"]["tried"][0]["verification"]["cost_micro"]


async def test_catalog_get_and_child_stripping(clients: AsyncClient, verifiers_on):
    find = (await clients.get(f"/catalog/endpoints/{FIND}")).json()
    assert "X-Treg-Route-Verify-Fallthrough" in find["routing"]["headers"]
    assert "X-Treg-Route-Verify-Fallthrough" in find["endpoint"]["input"]["note"]
    verify = (await clients.get(f"/catalog/endpoints/{VERIFY}")).json()
    assert "X-Treg-Route-Verify-Fallthrough" not in verify["routing"]["headers"]

    parent = type("P", (), {})()
    parent.input = type("I", (), {
        "raw_headers": ((b"x-treg-route-verify-fallthrough", b"true"), (b"x-other", b"1")),
        "caller": None, "client_ip": "", "catalog_only": False,
    })()
    child = call_route._child_input(parent, {"method": "POST", "id": "x"}, {}, {"email": "a@b.c"})
    assert b"x-treg-route-verify-fallthrough" not in {k for k, _ in child.raw_headers}


def test_parse_contracts_validates_fallthrough_verdicts():
    base = {
        "identity": [{"email": "string"}],
        "output": {"email": {"type": "string"}, "verified": {"type": "boolean"}},
    }
    verify_contract = {
        "identity": [{"email": "string"}],
        "output": {"status": {"type": "string"}},
        "verdict": {"from": "status", "words": ["valid", "invalid", "unknown"], "map": {}},
    }
    with pytest.raises(ValueError, match="fallthrough_verdicts"):
        C.parse_contracts({"contracts": {
            "people.email.verify": verify_contract,
            "x.find": {**base, "check": {"endpoint": VERIFY, "field": "email", "fallthrough_verdicts": ["bogus"]}},
        }})
    with pytest.raises(ValueError, match="blank"):
        C.parse_contracts({"contracts": {
            "x.find": {**base, "check": {"endpoint": VERIFY, "field": "email", "fallthrough_verdicts": ["  "]}},
        }})
    assert "invalid" in catalog_store.load().contracts["people.email.find"].check.fallthrough_verdicts
    assert "dead" in catalog_store.load().contracts["people.phone.find"].check.fallthrough_verdicts
