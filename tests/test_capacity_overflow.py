"""Step E of docs/PROVIDER-CAPACITY-PLAN.md — the overflow child cycle: the same vendor endpoint
served through a treg-owned aggregator account when treg's own account fails a tier-4 call.
Off by default; shadow mode never changes the caller's answer."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient
from sqlmodel import select

from dataclasses import replace
from datetime import datetime, timedelta
from treg.domain.capacity import marks, policy, signatures
from treg.domain.capacity import routes as R
from treg.domain.catalog import store
from treg import audit, ratestore
from treg.application.call import overflow as O
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.routes_view import view as routes_view
from treg.domain.capacity.marks import LOCK_NS, Lock
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.models import Hold, LedgerEntry, OverflowRoute, OverflowSpend
from treg.timeutil import utcnow_naive

from test_capacity_overflow_routes import APOLLO_OUT_OF_CREDITS, APOLLO_VALIDATION
from test_marketplace_call import EP, EP_MICRO, EP_PATH, _balance, _fake_relay, platform_on  # noqa: F401

VENDOR_BODY = {"data": {"comments": [{"id": "1", "text": "hashed"}], "cursor": 20}}


@pytest.fixture
def overflow_on(monkeypatch, platform_on):
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "on")
    monkeypatch.setenv("TREG_OVERFLOW_KEY_ORTHOGONAL", "ORTH-KEY")
    monkeypatch.setenv("TREG_OVERFLOW_KEY_MONID", "MONID-KEY")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _route(aggregator="orthogonal", price_micro=3_000, enabled=True, *, endpoint_id=EP, provider="tikhub",
                 method="GET", path=EP_PATH, ratio=3.0):
    async with session_maker() as db:
        db.add(OverflowRoute(endpoint_id=endpoint_id, aggregator=aggregator, provider=provider, method=method, path=path,
                             agg_slug=provider, agg_path=path, agg_price_micro=price_micro, agg_unit="call",
                             ratio=ratio, enabled=enabled, last_verified_at=utcnow_naive()))
        await db.commit()
    routes_view.invalidate()
    capacity_view.invalidate()


def _orthogonal(answers: list[tuple[int, dict]], seen: list):
    async def _send(client, req):
        seen.append(req)
        status, body = answers.pop(0)
        return httpx.Response(status, json=body, request=httpx.Request(req.method, req.url))
    return _send


async def _holds():
    async with session_maker() as db:
        return (await db.execute(select(Hold))).scalars().all()


async def _rows(model):
    async with session_maker() as db:
        return (await db.execute(select(model))).scalars().all()


async def test_off_by_default_the_vendor_402_is_relayed_unchanged(clients: AsyncClient, platform_on, monkeypatch):
    await _route()
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {})], seen))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and seen == [] and "X-Treg-Served-Via" not in r.headers


async def test_findymail_shaped_402_on_tier4_runs_one_child_cycle(clients: AsyncClient, overflow_on, monkeypatch):
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen = []
    envelope = {"success": True, "data": VENDOR_BODY, "priceCents": 0.3, "requestId": "run_1",
                "billing": {"chargedPriceCents": 0.3}}
    monkeypatch.setattr(O, "_send", _orthogonal([(200, envelope)], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r.status_code == 200, r.text
    assert r.json() == VENDOR_BODY, "the vendor's body, byte-for-byte the fixture's data"
    assert r.headers["X-Treg-Served-Via"] == "overflow:orthogonal"
    assert r.headers["X-Treg-Cost-Micro"] == "3000", "the aggregator's real price, 0% markup"
    assert before - await _balance(clients) == 3_000
    # the aggregator saw the SAME vendor request, wrapped
    assert len(seen) == 1 and seen[0].json == {"api": "tikhub", "path": EP_PATH, "query": {"aweme_id": "7", "count": "5"}}
    assert seen[0].headers["Authorization"] == "Bearer ORTH-KEY"
    assert await _holds() == [], "both holds closed"
    entries = await _rows(LedgerEntry)
    kinds = sorted((e.kind, e.call_id or "") for e in entries if e.kind != "grant")
    parent = r.headers["X-Treg-Call-Id"]
    assert kinds == sorted([("reserve", parent), ("release", parent), ("reserve", f"{parent}:overflow"), ("settle", f"{parent}:overflow")])
    settle = next(e for e in entries if e.kind == "settle")
    assert settle.meta["cost_source"] == "aggregator" and settle.meta["served_via"] == "overflow:orthogonal"
    spend = await _rows(OverflowSpend)
    assert len(spend) == 1 and spend[0].aggregator == "orthogonal" and spend[0].calls == 1 and spend[0].cost_micro == 3_000
    assert spend[0].delta_micro == 3_000 - EP_MICRO
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    mine = [x for x in rows if x["tool_name"] == EP]
    assert len(mine) == 2, "primary attempt + child, sharing call_ref"
    tiers = {x.get("credential_tier") for x in mine}
    assert tiers == {"platform", "platform-overflow"}


async def test_overflow_budget_reconciles_the_estimate_to_the_actual_cost(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"out"}'))
    seen = []
    envelope = {"success": True, "data": VENDOR_BODY, "priceCents": 0.25}
    monkeypatch.setattr(O, "_send", _orthogonal([(200, envelope)], seen))

    response = await clients.get(f"/call/{EP}?aweme_id=7")

    assert response.status_code == 200
    assert response.headers["X-Treg-Cost-Micro"] == "2500"
    spend = await _rows(OverflowSpend)
    assert len(spend) == 1
    assert (spend[0].calls, spend[0].cost_micro) == (1, 2_500)


async def test_overflow_substitutes_consumed_path_params_before_calling_aggregator(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    endpoint = "predictleads.companies.enrich"
    monkeypatch.setenv("TREG_PLATFORM_KEY_PREDICTLEADS", "TEST-PREDICTLEADS-KEY")
    monkeypatch.setenv(
        "TREG_PLATFORM_PROVIDERS", "tikhub,scrapecreators,dataforseo,brightdata,predictleads",
    )
    get_settings.cache_clear()
    async with session_maker() as db:
        db.add(OverflowRoute(
            endpoint_id=endpoint, aggregator="orthogonal", provider="predictleads", method="GET",
            path="/companies/{id_or_domain}", agg_slug="predictleads",
            agg_path="/v3/companies/{id_or_domain}", agg_price_micro=40_000, agg_unit="call",
            ratio=1.0, enabled=True, last_verified_at=utcnow_naive(),
        ))
        await db.commit()
    routes_view.invalidate()
    capacity_view.invalidate()
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen = []
    envelope = {"success": True, "data": VENDOR_BODY, "priceCents": 4.0}
    monkeypatch.setattr(O, "_send", _orthogonal([(200, envelope)], seen))

    r = await clients.get(f"/call/{endpoint}?id_or_domain=stripe.com")

    assert r.status_code == 200, r.text
    assert len(seen) == 1
    assert seen[0].json["path"] == "/v3/companies/stripe.com"
    assert "{" not in seen[0].json["path"] and "}" not in seen[0].json["path"]
    assert "query" not in seen[0].json


async def test_aggregator_402_releases_the_child_marks_it_unhealthy_and_answers_a_typed_503(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    await _route()
    async with session_maker() as db:  # a second route exists; it must NOT be tried
        db.add(OverflowRoute(endpoint_id=EP, aggregator="monid", provider="tikhub", method="GET", path=EP_PATH,
                             agg_slug="tikhub", agg_path=EP_PATH, agg_price_micro=2_000, agg_unit="call",
                             ratio=2.0, enabled=True, last_verified_at=utcnow_naive()))
        await db.commit()
    routes_view.invalidate()
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b"out"))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(402, {"success": False, "error": "insufficient balance"})], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_capacity_unavailable"
    assert r.headers["X-Treg-Error"] == "1" and r.headers.get("X-Treg-Cost-Micro") in (None, "0")
    assert await _balance(clients) == before and await _holds() == []
    assert len(seen) == 1, "one hop: the second aggregator is never contacted"
    async with session_maker() as db:
        lock = Lock.from_json(await ratestore.kv_get(db, LOCK_NS, "overflow:orthogonal"))
    assert lock.is_active()
    # …and the next call skips the unhealthy aggregator and uses Monid (no manual reload: the
    # strike invalidated this process's view)
    monid_ok = {"runId": "r", "status": "COMPLETED", "output": VENDOR_BODY, "providerResponse": {"httpStatus": 200},
                "billing": {"reportedCost": {"value": 2000, "unit": "MICRO_DOLLAR"}}}
    monkeypatch.setattr(O, "_send", _orthogonal([(200, monid_ok)], seen))
    r2 = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r2.status_code == 200 and r2.headers["X-Treg-Served-Via"] == "overflow:monid"
    assert seen[-1].json["provider"] == "tikhub" and r2.headers["X-Treg-Cost-Micro"] == "2000"


async def test_contract_refusal_falls_back_to_the_vendor_answer(clients: AsyncClient, overflow_on, monkeypatch):
    await _route()
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"nope"}'))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(400, {"success": False, "error": "x",
                                                        "_orthogonal": {"error": "orthogonal_endpoint_contract", "message": "company required"}})], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and r.text == '{"detail":"nope"}'
    assert await _balance(clients) == before and await _holds() == []


async def test_adapter_parse_crash_falls_back_to_vendor_answer_and_releases_child_hold(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    await _route()
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"vendor out"}'))
    seen = []
    monkeypatch.setattr(
        O, "_send",
        _orthogonal([(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], seen),
    )

    def crash_parse(status, body):
        raise RuntimeError("adapter parse crashed")

    monkeypatch.setattr(O.by_name("orthogonal"), "parse", crash_parse)

    response = await clients.get(f"/call/{EP}?aweme_id=7")

    assert response.status_code == 402
    assert response.content == b'{"detail":"vendor out"}'
    assert len(seen) == 1
    assert not any(hold.call_id.endswith(":overflow") for hold in await _holds())
    spend = await _rows(OverflowSpend)
    assert len(spend) == 1
    assert (spend[0].calls, spend[0].cost_micro) == (1, 3_000)


async def test_request_error_after_send_preserves_unknown_budget_estimate(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"vendor out"}'))
    seen = []

    async def timeout_after_send(client, request):
        seen.append(request)
        raise httpx.ReadTimeout(
            "aggregator response timed out",
            request=httpx.Request(request.method, request.url),
        )

    monkeypatch.setattr(O, "_send", timeout_after_send)

    response = await clients.get(f"/call/{EP}?aweme_id=7")

    assert response.status_code == 503
    assert len(seen) == 1
    assert await _holds() == []
    spend = await _rows(OverflowSpend)
    assert len(spend) == 1
    assert (spend[0].calls, spend[0].cost_micro) == (1, 3_000)


async def test_vendor_500_cost_reported_by_aggregator_is_counted_in_daily_spend(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"out"}'))
    seen = []
    envelope = {
        "success": False,
        "error": "upstream returned status 500",
        "data": {"error": "vendor failed"},
        "priceCents": 0.3,
    }
    monkeypatch.setattr(O, "_send", _orthogonal([(500, envelope)], seen))

    response = await clients.get(f"/call/{EP}?aweme_id=7")

    assert response.status_code == 500
    spend = await _rows(OverflowSpend)
    assert len(spend) == 1
    assert (spend[0].calls, spend[0].cost_micro) == (1, 3_000)


async def test_budget_crossing_skips_overflow(clients: AsyncClient, overflow_on, monkeypatch):
    await _route(price_micro=3_000)
    monkeypatch.setenv("TREG_OVERFLOW_DAILY_BUDGET_USD", "0.004")
    get_settings.cache_clear()
    async with session_maker() as db:
        from treg.domain.capacity.overflow_spend import add_in_transaction
        await add_in_transaction(db, "orthogonal", 2_000, 0)
        await db.commit()
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b"out"))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {})], seen))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and seen == [] and await _holds() == []


async def test_caller_400_and_own_key_never_overflow(clients: AsyncClient, overflow_on, monkeypatch):
    await _route()
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {})], seen))
    monkeypatch.setattr(call_service, "relay", _fake_relay(400, b'{"detail":"bad id"}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 400
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b"out"))
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier 2
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 402
    assert seen == []


async def test_shadow_mode_probes_records_spend_and_returns_the_vendor_error(clients: AsyncClient, overflow_on, monkeypatch):
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "shadow")
    get_settings.cache_clear()
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and "X-Treg-Served-Via" not in r.headers and r.headers["X-Treg-Cost-Micro"] == "0"
    assert len(seen) == 1 and await _balance(clients) == before and await _holds() == []
    spend = await _rows(OverflowSpend)
    assert len(spend) == 1 and spend[0].cost_micro == 3_000 and spend[0].calls == 1
    kinds = {e.kind for e in await _rows(LedgerEntry)}
    assert "settle" not in kinds, "shadow never charges"


async def test_cancellation_cleanup_releases_both_holds_exactly_once(clients: AsyncClient, overflow_on):
    from treg.domain import money as ledger
    from treg.application.call.settle import _finish_cancelled_call
    from treg.application.call.resolve import MarketplaceCall
    from treg.models import Tool
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.reserve_in_transaction(db, org_id, EP, 1_000, call_id="REF")
        await ledger.reserve_in_transaction(db, org_id, EP, 3_000, call_id="REF:overflow")
        await db.commit()
    assert len(await _holds()) == 2
    mk = MarketplaceCall(tool=Tool(org_id=org_id, name=EP, owner="x", base_url="https://x", host="x"),
                         upstream="https://x", consumed=set(), endpoint_id=EP, provider="tikhub",
                         tier="platform", estimate_micro=1_000, call_id="REF")
    await _finish_cancelled_call(None, mk, "REF")
    assert await _holds() == []
    releases = [e for e in await _rows(LedgerEntry) if e.kind == "release"]
    assert sorted(e.call_id for e in releases) == ["REF", "REF:overflow"]
    await _finish_cancelled_call(None, mk, "REF")  # again: nothing to release, nothing breaks
    assert len([e for e in await _rows(LedgerEntry) if e.kind == "release"]) == 2


async def test_an_exhausted_account_with_a_route_skips_the_direct_attempt(clients: AsyncClient, overflow_on, monkeypatch):
    """The ladder (plan §4): view says exhausted → no direct attempt, no parent hold → child cycle."""
    await _route(price_micro=3_000)
    now = utcnow_naive()
    from datetime import timedelta
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, "tikhub", LatestState(
            "tikhub", 0.0, "USD", now, "exact", exhausted_until=now + timedelta(hours=1), health="exhausted").to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()
    direct = []
    async def never(*a, **k):
        direct.append(1)
        raise AssertionError("the direct relay must not run")
    monkeypatch.setattr(call_service, "relay", never)
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "k1"})
    assert r.status_code == 200 and r.headers["X-Treg-Served-Via"] == "overflow:orthogonal", r.text
    assert direct == [] and len(seen) == 1
    assert before - await _balance(clients) == 3_000 and await _holds() == []
    parent = r.headers["X-Treg-Call-Id"]
    kinds = sorted((e.kind, e.call_id or "") for e in await _rows(LedgerEntry) if e.kind != "grant")
    assert kinds == [("reserve", f"{parent}:overflow"), ("settle", f"{parent}:overflow")], "no parent hold at all"
    replay = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "k1"})
    assert replay.status_code == 200 and replay.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert len(seen) == 1, "the replay never touched the aggregator"


async def test_skip_direct_budget_reservation_crash_falls_back_to_typed_capacity_503(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    from datetime import timedelta

    await _route(price_micro=3_000)
    now = utcnow_naive()
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, "tikhub", LatestState(
            "tikhub", 0.0, "USD", now, "exact", exhausted_until=now + timedelta(hours=1),
            health="exhausted",
        ).to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()

    async def crash_reservation(db, aggregator, estimate_micro, cap_micro, *, day=None):
        raise RuntimeError("budget reservation crashed")

    monkeypatch.setattr(O.overflow_spend_ledger, "reserve_in_transaction", crash_reservation)

    response = await clients.get(f"/call/{EP}?aweme_id=7")

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "provider_capacity_unavailable"
    assert await _holds() == []


async def test_an_exhausted_account_without_a_route_is_still_the_typed_503(clients: AsyncClient, overflow_on):
    from datetime import timedelta
    now = utcnow_naive()
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, "tikhub", LatestState(
            "tikhub", 0.0, "USD", now, "exact", exhausted_until=now + timedelta(hours=1), health="exhausted").to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()
    routes_view.invalidate()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_capacity_unavailable"
    assert await _holds() == []


async def test_org_opt_out_is_honoured_before_any_aggregator_is_contacted(clients: AsyncClient, overflow_on, monkeypatch):
    await _route()
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    assert (await clients.get(f"/orgs/{org_id}/settings")).json()["platform_overflow"] is True
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"platform_overflow": False})
    assert r.status_code == 200, r.text
    assert (await clients.get(f"/orgs/{org_id}/settings")).json()["platform_overflow"] is False
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], seen))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and seen == [] and await _holds() == []
    # exhausted view + route: the opted-out team gets the typed 503, not a relay
    from datetime import timedelta
    now = utcnow_naive()
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, "tikhub", LatestState(
            "tikhub", 0.0, "USD", now, "exact", exhausted_until=now + timedelta(hours=1), health="exhausted").to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503 and seen == []
    # …and back on: served
    await clients.patch(f"/orgs/{org_id}/settings", json={"platform_overflow": True})
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.headers["X-Treg-Served-Via"] == "overflow:orthogonal" and len(seen) == 1


def test_cli_org_overflow_parses(monkeypatch):
    from treg import cli
    seen = {}
    monkeypatch.setattr(cli, "cmd_org_overflow", lambda args, cfg: seen.update(vars(args)))
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        pytest.skip("no exposed parser builder")
    args = parser.parse_args(["org", "overflow", "off"])
    assert args.state == "off" and args.fn is not None


async def test_child_with_no_reported_cost_settles_at_the_aggregator_reserve(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    """The child carries its own settlement basis. An aggregator answer without a price settles at
    the aggregator reserve, never at the parent's direct price (a regression the basis refactor
    introduced and the review caught)."""
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"out"}'))
    envelope = {"success": True, "data": VENDOR_BODY}  # no priceCents, no billing block
    monkeypatch.setattr(O, "_send", _orthogonal([(200, envelope)], []))
    before = await _balance(clients)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200
    assert response.headers["X-Treg-Cost-Micro"] == "3000"
    assert before - await _balance(clients) == 3_000
    assert 3_000 != EP_MICRO, "the fixture must price the aggregator differently from direct"


# --- Apollo: "out of credits" is a 422, not a 402 (ops/capacity.md, 2026-09-01) --------------

APOLLO_EP = "apollo.people.enrich"           # POST /people/match, 1 credit = $0.026 per hit
APOLLO_PATH = "/people/match"
APOLLO_PERSON = {"person": {"id": "p1", "name": "hashed", "email": "hashed"}, "request_id": 1}


async def test_apollo_out_of_credits_422_on_tier4_overflows_through_orthogonal(clients: AsyncClient, overflow_on, monkeypatch):
    await _route(endpoint_id=APOLLO_EP, provider="apollo", method="POST", path=APOLLO_PATH, price_micro=10_000, ratio=0.38)
    monkeypatch.setattr(call_service, "relay", _fake_relay(422, APOLLO_OUT_OF_CREDITS))
    seen = []
    envelope = {"success": True, "data": APOLLO_PERSON, "priceCents": 1.0, "requestId": "run_a",
                "billing": {"chargedPriceCents": 1.0}}
    monkeypatch.setattr(O, "_send", _orthogonal([(200, envelope)], seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{APOLLO_EP}", json={"email": "hashed@example.com"})
    assert r.status_code == 200, r.text
    assert r.json() == APOLLO_PERSON and r.headers["X-Treg-Served-Via"] == "overflow:orthogonal"
    assert r.headers["X-Treg-Cost-Micro"] == "10000" and before - await _balance(clients) == 10_000
    assert len(seen) == 1 and seen[0].json["api"] == "apollo" and seen[0].json["path"] == APOLLO_PATH
    assert seen[0].json["body"] == {"email": "hashed@example.com"}, "the caller's own request, relayed"
    assert await _holds() == []
    await audit.drain()
    mine = [x for x in (await clients.get("/calls")).json() if x["tool_name"] == APOLLO_EP]
    assert {x.get("credential_tier") for x in mine} == {"platform", "platform-overflow"}
    async with session_maker() as db:
        lock = Lock.from_json(await ratestore.kv_get(db, LOCK_NS, "apollo"))
    assert lock.strikes == 1 and not lock.is_active(), "the 422 was read as OUR account running dry: a strike, not the caller's mistake"


APOLLO_SEARCH = "apollo.companies.search"      # POST /mixed_companies/search, per_call: a 4xx is normally billable
APOLLO_SEARCH_PATH = "/mixed_companies/search"


async def test_apollo_out_of_credits_on_a_per_call_endpoint_is_never_billed_to_the_caller(clients: AsyncClient, platform_on, monkeypatch):
    """`per_call` bills a 422 as the caller's input error. Not this one: the table says it is OUR
    account, so the hold is released, and the caller pays nothing for treg's empty pool."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(422, APOLLO_OUT_OF_CREDITS))
    before = await _balance(clients)
    r = await clients.post(f"/call/{APOLLO_SEARCH}", json={"q_organization_name": "x"})
    assert r.status_code == 422 and r.content == APOLLO_OUT_OF_CREDITS
    assert await _balance(clients) == before and await _holds() == []
    entries = [e for e in await _rows(LedgerEntry) if e.kind != "grant"]
    assert sorted(e.kind for e in entries) == ["release", "reserve"]
    assert next(e for e in entries if e.kind == "release").meta.get("reason") == "capacity_balance"


async def test_apollo_out_of_credits_on_a_per_call_endpoint_pays_the_aggregator_once(clients: AsyncClient, overflow_on, monkeypatch):
    await _route(endpoint_id=APOLLO_SEARCH, provider="apollo", method="POST", path=APOLLO_SEARCH_PATH, price_micro=10_000, ratio=0.38)
    monkeypatch.setattr(call_service, "relay", _fake_relay(422, APOLLO_OUT_OF_CREDITS))
    seen = []
    envelope = {"success": True, "data": {"organizations": [{"id": "o1"}]}, "priceCents": 1.0, "requestId": "run_b",
                "billing": {"chargedPriceCents": 1.0}}
    monkeypatch.setattr(O, "_send", _orthogonal([(200, envelope)], seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{APOLLO_SEARCH}", json={"q_organization_name": "x"})
    assert r.status_code == 200 and r.headers["X-Treg-Served-Via"] == "overflow:orthogonal"
    assert before - await _balance(clients) == 10_000, "the aggregator's price once; the parent 422 was released, not settled"
    parent = r.headers["X-Treg-Call-Id"]
    kinds = sorted((e.kind, e.call_id or "") for e in await _rows(LedgerEntry) if e.kind != "grant")
    assert kinds == sorted([("reserve", parent), ("release", parent), ("reserve", f"{parent}:overflow"), ("settle", f"{parent}:overflow")])


async def test_aggregator_relaying_the_vendors_own_out_of_credits_dialect_is_the_aggregators_dry_account(
        clients: AsyncClient, overflow_on, monkeypatch):
    """Orthogonal's Apollo account can be empty too, and Apollo says so with a 422, not a 402. The
    child is released (never billed at the aggregator's price for a request no vendor served), the
    aggregator is marked unhealthy, and the caller gets the typed 503 - not a 422 served 'via'."""
    await _route(endpoint_id=APOLLO_SEARCH, provider="apollo", method="POST", path=APOLLO_SEARCH_PATH, price_micro=10_000, ratio=0.38)
    monkeypatch.setattr(call_service, "relay", _fake_relay(422, APOLLO_OUT_OF_CREDITS))
    seen = []
    relayed = {"success": False, "error": "Upstream returned status 422", "data": json.loads(APOLLO_OUT_OF_CREDITS),
               "priceCents": 1.0, "requestId": "run_c"}
    monkeypatch.setattr(O, "_send", _orthogonal([(422, relayed)], seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{APOLLO_SEARCH}", json={"q_organization_name": "x"})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_capacity_unavailable", r.text
    assert await _balance(clients) == before and await _holds() == []
    assert {e.kind for e in await _rows(LedgerEntry) if e.kind != "grant"} == {"reserve", "release"}
    async with session_maker() as db:
        lock = Lock.from_json(await ratestore.kv_get(db, LOCK_NS, "overflow:orthogonal:apollo"))
        whole = await ratestore.kv_get(db, LOCK_NS, "overflow:orthogonal")
    assert lock.is_active(), "the aggregator's own Apollo account is dry: skip it for a while"
    assert whole is None, "for Apollo only - hunter and lusha still overflow through Orthogonal"
    # ...and the next Apollo call honours the mark: Orthogonal is not contacted, the only route
    # is out, so the vendor's own (unbilled) answer stands
    again = []
    monkeypatch.setattr(O, "_send", _orthogonal([], again))
    r2 = await clients.post(f"/call/{APOLLO_SEARCH}", json={"q_organization_name": "x"})
    assert r2.status_code == 422 and again == [] and "X-Treg-Served-Via" not in r2.headers


async def test_a_vendors_period_quota_through_the_aggregator_is_that_vendors_answer_not_an_aggregator_outage(
        clients: AsyncClient, overflow_on, monkeypatch):
    """Apollo's daily cap on Orthogonal's Apollo account: the aggregator is dry for APOLLO. The
    child is released and Apollo skips Orthogonal for a while; hunter, lusha and everyone else
    keep overflowing through it."""
    await _route(endpoint_id=APOLLO_SEARCH, provider="apollo", method="POST", path=APOLLO_SEARCH_PATH, price_micro=10_000, ratio=0.38)
    monkeypatch.setattr(call_service, "relay", _fake_relay(422, APOLLO_OUT_OF_CREDITS))
    relayed = {"success": False, "error": "Upstream returned status 429",
               "data": {"error": "You have exceeded the rate limit per day"}, "priceCents": 0}
    monkeypatch.setattr(O, "_send", _orthogonal([(429, relayed)], []))
    before = await _balance(clients)
    r = await clients.post(f"/call/{APOLLO_SEARCH}", json={"q_organization_name": "x"})
    assert r.status_code == 503 and await _balance(clients) == before
    async with session_maker() as db:
        whole = await ratestore.kv_get(db, LOCK_NS, "overflow:orthogonal")
        mine = Lock.from_json(await ratestore.kv_get(db, LOCK_NS, "overflow:orthogonal:apollo"))
    assert whole is None and mine.is_active(), "one vendor's quota is not the aggregator's outage"


async def test_apollo_validation_422_is_the_callers_and_never_overflows(clients: AsyncClient, overflow_on, monkeypatch):
    await _route(endpoint_id=APOLLO_EP, provider="apollo", method="POST", path=APOLLO_PATH, price_micro=10_000, ratio=0.38)
    monkeypatch.setattr(call_service, "relay", _fake_relay(422, APOLLO_VALIDATION))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([], seen))
    r = await clients.post(f"/call/{APOLLO_EP}", json={})
    assert r.status_code == 422 and r.content == APOLLO_VALIDATION and seen == [], "relayed verbatim, no aggregator contacted"
    assert "X-Treg-Served-Via" not in r.headers
# --- the dashboard sees what the caller got -----------------------------------------------------

async def _exhausted(provider: str = "tikhub") -> None:
    from datetime import timedelta
    now = utcnow_naive()
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, provider, LatestState(
            provider, 0.0, "USD", now, "exact", exhausted_until=now + timedelta(hours=1), health="exhausted").to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()


async def test_a_call_rescued_by_overflow_is_one_ok_event_not_a_refusal(clients: AsyncClient, overflow_on, monkeypatch, posthog_events):
    """Skip-direct: the resolver knew the account was dry and went straight to the aggregator. The
    caller got a 200; the dashboard used to see a 503 refused_by=capacity and nothing else."""
    await _route(price_micro=3_000)
    await _exhausted()
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], []))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.headers["X-Treg-Served-Via"] == "overflow:orthogonal"
    (e,) = await posthog_events()
    p = e["properties"]
    assert p["status_code"] == 200 and p["outcome"] == "ok" and p["refused_by"] is None
    assert p["tier"] == "platform-overflow" and p["served_via"] == "overflow:orthogonal"
    assert p["charged_micro"] == 3_000 and p["call_ref"] == r.headers["X-Treg-Call-Id"]
    await audit.drain()
    rows = [x for x in (await clients.get("/calls")).json() if x["tool_name"] == EP]
    assert {x.get("credential_tier") for x in rows} == {"platform", "platform-overflow"}, "both DB rows stay"


async def test_a_call_overflow_could_not_rescue_is_still_one_refusal_event(clients: AsyncClient, overflow_on, monkeypatch, posthog_events):
    await _route(price_micro=3_000)
    await _exhausted()
    monkeypatch.setattr(O, "_send", _orthogonal([(402, {"success": False, "error": "insufficient balance"})], []))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503
    (e,) = await posthog_events()
    p = e["properties"]
    assert p["status_code"] == 503 and p["outcome"] == "treg_refused" and p["refused_by"] == "capacity"
    assert "served_via" not in p


async def test_a_vendor_402_rescued_by_overflow_is_one_ok_event(clients: AsyncClient, overflow_on, monkeypatch, posthog_events):
    """The post-failure path: the vendor answered 402, the child served. One event, the child's."""
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], []))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200
    (e,) = await posthog_events()
    p = e["properties"]
    assert p["status_code"] == 200 and p["outcome"] == "ok" and p["tier"] == "platform-overflow"
    assert p["served_via"] == "overflow:orthogonal" and p["capacity_signal"] == "balance", "the strike is still on the event"


async def test_a_vendor_402_overflow_did_not_rescue_keeps_the_vendor_error_event(clients: AsyncClient, overflow_on, monkeypatch, posthog_events):
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"nope"}'))
    monkeypatch.setattr(O, "_send", _orthogonal([(400, {"success": False, "error": "x", "_orthogonal": {"error": "orthogonal_endpoint_contract", "message": "missing"}})], []))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402
    (e,) = await posthog_events()
    p = e["properties"]
    assert p["status_code"] == 402 and p["outcome"] == "vendor_error" and p["tier"] == "platform" and "served_via" not in p


# ---- the weekly verify: renewals are held to their own cap and the run to a budget -------------
# On 2026-09-07 the cron (`verify --all`, 2¢ cap) skipped every stamped route priced above 2¢ - 46
# routes mapped and verified on 2026-08-26 decayed off and no run could ever bring them back. A
# stamped route renews under `--renew-max-usd`; the 2¢ `--max-usd` is only for discovery.

def test_verify_plan_renews_stamped_routes_first_and_discovers_only_with_all():
    from datetime import timedelta
    from types import SimpleNamespace
    from treg import worker
    now = utcnow_naive()
    old = SimpleNamespace(endpoint_id="a.old", provider="a", enabled=True, last_verified_at=now - timedelta(days=6))
    fresh = SimpleNamespace(endpoint_id="a.fresh", provider="a", enabled=True, last_verified_at=now - timedelta(days=1))
    lapsed = SimpleNamespace(endpoint_id="b.lapsed", provider="b", enabled=False, last_verified_at=now - timedelta(days=9))
    never = SimpleNamespace(endpoint_id="b.never", provider="b", enabled=False, last_verified_at=None)
    plan = worker._verify_plan([fresh, never, old, lapsed], all_rows=True, only=None, max_usd=0.02, renew_max_usd=1.0)
    assert [(r.endpoint_id, cap) for r, cap in plan] == [
        ("b.lapsed", 1.0), ("a.old", 1.0), ("a.fresh", 1.0), ("b.never", 0.02)]
    without_all = worker._verify_plan([fresh, never, old, lapsed], all_rows=False, only=None, max_usd=0.02, renew_max_usd=1.0)
    assert [r.endpoint_id for r, _ in without_all] == ["b.lapsed", "a.old", "a.fresh"]
    only_b = worker._verify_plan([fresh, never, old, lapsed], all_rows=True, only={"b"}, max_usd=0.02, renew_max_usd=1.0)
    assert [r.endpoint_id for r, _ in only_b] == ["b.lapsed", "b.never"]


async def test_verify_run_visits_pricey_renewals_within_budget_and_skips_pricey_discovery(
    clients, overflow_on, monkeypatch,
):
    from datetime import timedelta
    from types import SimpleNamespace
    from treg import worker
    from treg.domain.capacity import verify as V
    from treg.domain.catalog import store as catalog_store
    monkeypatch.setattr(get_settings(), "secret_key", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    eps = [e for e in catalog_store.load().endpoints if e.get("test_request")][:3]
    assert len(eps) == 3
    now = utcnow_naive()
    specs = [  # (endpoint, price, enabled, stamped)
        (eps[0], 500_000, True, now - timedelta(days=6)),   # renewal, 50¢: above the 2¢ discovery cap
        (eps[1], 10_000, True, now - timedelta(days=1)),    # renewal, 1¢
        (eps[2], 500_000, False, None),                     # discovery, 50¢: held to the 2¢ cap
    ]
    async with session_maker() as db:
        for ep, price, enabled, stamped in specs:
            db.add(OverflowRoute(endpoint_id=ep["id"], aggregator="orthogonal", provider=ep["provider"],
                                 method=ep["method"], path=ep["path"], agg_slug=ep["provider"], agg_path=ep["path"],
                                 agg_price_micro=price, agg_unit="call", ratio=1.0, enabled=enabled,
                                 last_verified_at=stamped))
        await db.commit()
    from treg import oauth_providers
    monkeypatch.setattr(oauth_providers, "get", lambda *_: None)  # no direct key: spend = relay fee
    seen = []
    async def verify(client, route, **kwargs):
        seen.append(route.endpoint_id)
        return V.Verification(route.endpoint_id, route.aggregator, None, 200, True,
                              route.agg_price_micro, utcnow_naive())
    monkeypatch.setattr(V, "verify_route", verify)
    args = SimpleNamespace(all=True, only=None, max_usd=0.02, renew_max_usd=1.0, budget_usd=15.0)
    assert await worker._overflow_verify(args) == 0
    assert seen == [eps[0]["id"], eps[1]["id"]]   # oldest renewal first; the 50¢ discovery pair is skipped
    seen.clear()
    args.budget_usd = 0.30                          # the 50¢ renewal no longer fits; the 1¢ one still does
    assert await worker._overflow_verify(args) == 0
    assert seen == [eps[1]["id"]]


# ---- ContactOut ----

CONTACTOUT_EP = 'contactout.people.contact.work'
CONTACTOUT_PATH = '/v1/people/linkedin'
CONTACTOUT_QUERY = {'profile': 'https://linkedin.com/in/synthetic-test', 'email_type': 'work'}
CONTACTOUT_QUOTA = b'{"status_code":403,"message":"You\'re out of credits, please email your sales manager"}'
CONTACTOUT_BODY = {'status_code': 200, 'profile': {'work_email': ['test@example.test']}}

@pytest.fixture
def contactout_on(monkeypatch, overflow_on):
    monkeypatch.setenv('TREG_PLATFORM_PROVIDERS', 'contactout')
    monkeypatch.setenv('TREG_PLATFORM_KEY_CONTACTOUT', 'DIRECT-TEST')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_contactout_credit_exhaustion_is_endpoint_scoped_and_access_refusal_is_not_capacity():
    signal = signatures.classify('contactout', 403, {}, CONTACTOUT_QUOTA)
    assert signal.kind == 'quota' and signatures.is_exhausting(signal)
    assert marks.lock_key('contactout', CONTACTOUT_EP, signal.kind) == CONTACTOUT_EP
    assert signatures.classify('contactout', 403, {}, b'{"message":"No access to endpoint"}') is None
    assert signatures.classify('contactout', 429, httpx.Headers({'Retry-After':'2'}), b'').kind == 'burst'
    assert policy.default_policy('contactout', has_key=True).overflow_allowed


@pytest.mark.parametrize('status,body', [(403,CONTACTOUT_QUOTA),(429,b'{"message":"Rate limit reached"}')])
async def test_contactout_capacity_failure_uses_existing_child_billing(clients, contactout_on, monkeypatch, status, body):
    await _route(endpoint_id=CONTACTOUT_EP, provider='contactout', path=CONTACTOUT_PATH, price_micro=550000)
    fake = _fake_relay(status, body)
    async def relay(*args, **kwargs):
        response = await fake(*args, **kwargs)
        return replace(response, raw_headers=((b'retry-after', b'0'),)) if status == 429 else response
    monkeypatch.setattr(call_service, 'relay', relay)
    seen=[]
    monkeypatch.setattr(O, '_send', _orthogonal([(200,{'success':True,'data':CONTACTOUT_BODY,'priceCents':55})],seen))
    before=await _balance(clients)
    r=await clients.get('/call/'+CONTACTOUT_EP,params=CONTACTOUT_QUERY)
    assert r.status_code==200
    assert r.json()==CONTACTOUT_BODY
    assert r.headers['X-Treg-Served-Via']=='overflow:orthogonal'
    assert before-await _balance(clients)==550000
    assert not await _holds()
    assert seen[0].json['query']['email_type']=='work'


@pytest.mark.parametrize('own,optout,status,body', [
    (True,False,403,CONTACTOUT_QUOTA), (False,True,403,CONTACTOUT_QUOTA),
    (False,False,404,b'{}'), (False,False,403,b'{"message":"No access to endpoint"}')])
async def test_contactout_byok_optout_and_noncapacity_errors_never_overflow(clients,contactout_on,monkeypatch,own,optout,status,body):
    await _route(endpoint_id=CONTACTOUT_EP,provider='contactout',path=CONTACTOUT_PATH,price_micro=550000)
    if own:
        await clients.post('/secrets',json={'name':'contactout','value':'OWN-TEST'})
    if optout:
        org=(await clients.get('/orgs')).json()[0]['org_id']
        await clients.patch(f'/orgs/{org}/settings',json={'platform_overflow':False})
    monkeypatch.setattr(call_service,'relay',_fake_relay(status,body))
    seen=[]
    monkeypatch.setattr(O,'_send',_orthogonal([],seen))
    r=await clients.get('/call/'+CONTACTOUT_EP,params=CONTACTOUT_QUERY)
    assert r.status_code==status and seen==[]
    assert not await _holds()


def test_contactout_only_verified_compatible_candidates_enable():
    enabled=[]
    cat=store.load()
    for row in R.load_seed():
        if row['provider']!='contactout': continue
        ep=cat.by_id[row['endpoint_id']];cv=cat.cost_view(ep['cost'],'contactout')
        route=OverflowRoute(**{k:row[k] for k in ('endpoint_id','aggregator','provider','method','path','agg_slug','agg_path','agg_unit')},agg_price_micro=round(row['agg_price_usd']*1e6),ratio=R.price_ratio(row['agg_price_usd'],R.our_event_usd(cv)),last_verified_at=datetime.fromisoformat(row['verified_at']) if row['verified_at'] else None)
        verdict=R.eligible(route,our_cost=ep['cost'],platform_eligible=True,policy=None,our_usd=cv['usd'])
        if verdict.enabled:
            enabled.append((row['endpoint_id'],row['aggregator']))
            assert row['verified_at']
            expired=R.eligible(route,our_cost=ep['cost'],platform_eligible=True,policy=None,our_usd=cv['usd'],now=route.last_verified_at+timedelta(days=8))
            assert not expired.enabled
    assert len(enabled)==13
    assert (CONTACTOUT_EP,'orthogonal') in enabled
    assert (CONTACTOUT_EP,'monid') not in enabled
    assert ('contactout.companies.enrich','orthogonal') not in enabled



@pytest.mark.parametrize('budget,expected_calls', [(0.5,0),(1.0,1)])
async def test_contactout_renewal_budget_includes_direct_cost_and_uses_ephemeral_profile(monkeypatch,budget,expected_calls):
    import runpy
    from types import SimpleNamespace
    from treg.domain.capacity import verify as V
    namespace=runpy.run_path('scripts/contactout_overflow_verify.py')
    main=namespace['main']
    globals_=main.__globals__
    candidate=next(dict(r) for r in R.load_seed() if r['endpoint_id']==CONTACTOUT_EP and r['aggregator']=='orthogonal')
    monkeypatch.setattr(R,'load_seed',lambda:[candidate])
    globals_['get_settings']=lambda:SimpleNamespace(platform_key_contactout='DIRECT-TEST',overflow_key_for=lambda p:'AGG-TEST')
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):
            return httpx.Response(200,json={'profiles':{'one':{'li_vanity':'synthetic-renewal'}}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:Client())
    seen=[]
    async def verify(client,route,**kwargs):
        seen.append(kwargs)
        assert kwargs['test_request']['queryParams']['profile'].endswith('/synthetic-renewal')
        assert kwargs['test_request']['queryParams']['include_phone']=='true'
        return V.Verification(CONTACTOUT_EP,'orthogonal',200,200,True,550000,utcnow_naive())
    monkeypatch.setattr(V,'verify_route',verify)
    result=await main(SimpleNamespace(budget_usd=budget,apply=False))
    assert len(seen)==expected_calls
    assert result==(0 if expected_calls else 1)


# ---- an aggregator's OWN per-request 4xx is request-scoped, never a strike --------------------
# 2026-09-08: one Orthogonal 400 (its request validation, no vendor data) parsed as `malformed`,
# which is AGGREGATOR_SIDE - overflow:orthogonal was struck for 15 minutes for every org, and every
# Apollo call for that quarter hour became provider_capacity_unavailable with no route left.

APOLLO_SEARCH_EP = "apollo.people.search"       # POST /mixed_people/api_search, catalog cost FREE
APOLLO_SEARCH_PATH = "/mixed_people/api_search"
ORTHOGONAL_OWN_400 = {"success": False, "error": "query.page must be a string"}
ORTHOGONAL_OWN_422 = {"success": False, "error": "Unprocessable: body.per_page exceeds 100",
                      "_orthogonal": {"message": "body.per_page exceeds 100"}}


def test_orthogonal_parse_reserves_malformed_for_what_is_not_an_envelope():
    from treg.infra.upstream.aggregators import AGGREGATOR_SIDE, orthogonal
    own_400 = orthogonal.parse(400, json.dumps(ORTHOGONAL_OWN_400).encode())
    own_422 = orthogonal.parse(422, json.dumps(ORTHOGONAL_OWN_422).encode())
    own_404 = orthogonal.parse(404, b'{"success":false,"error":"Unknown api slug"}')
    for res in (own_400, own_422, own_404):
        assert res.failure == "contract" and res.failure not in AGGREGATOR_SIDE
        assert res.cost_micro == 0 and res.upstream_status is None and res.upstream_body == b""
    assert own_422.detail == "body.per_page exceeds 100", "the aggregator's own message, for the caller"
    # what still blames the aggregator as a whole
    assert orthogonal.parse(402, b'{"success":false,"error":"insufficient balance"}').failure == "aggregator_balance"
    assert orthogonal.parse(401, b'{"error":"invalid key"}').failure == "aggregator_auth"
    assert orthogonal.parse(500, b'{"success":false,"error":"internal"}').failure == "malformed"
    assert orthogonal.parse(502, b"<html>bad gateway</html>").failure == "malformed"
    assert orthogonal.parse(200, b"<html>").failure == "malformed"
    # a 4xx that DOES carry the vendor's body is the vendor's answer, relayed as before
    relayed = orthogonal.parse(422, json.dumps({"success": False, "error": "API request failed with status 422",
                                                "data": {"error": "Please provide at least one of: first_name"}}).encode())
    assert relayed.failure is None and relayed.upstream_status == 422


async def _orthogonal_lock():
    async with session_maker() as db:
        raw = await ratestore.kv_get(db, LOCK_NS, "overflow:orthogonal")
    return Lock.from_json(raw) if raw else None


async def test_orthogonals_own_400_is_request_scoped_and_never_strikes_the_aggregator(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    """The old wrong outcome: the vendor 402 became a typed 503, overflow:orthogonal went unhealthy
    for 15 minutes for every org. Now: the vendor's own answer stands, nothing charged, no mark, and
    the very next call reaches Orthogonal again."""
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"nope"}'))
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(400, ORTHOGONAL_OWN_400),
                                                 (200, {"success": True, "data": VENDOR_BODY, "priceCents": 0.3})], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and r.text == '{"detail":"nope"}', "the vendor's answer, not the relay's envelope"
    assert "X-Treg-Served-Via" not in r.headers
    assert await _balance(clients) == before and await _holds() == []
    lock = await _orthogonal_lock()
    assert lock is None or not lock.is_active(), "a request-scoped refusal is not an aggregator outage"
    r2 = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r2.status_code == 200 and r2.headers["X-Treg-Served-Via"] == "overflow:orthogonal", r2.text
    assert len(seen) == 2 and before - await _balance(clients) == 3_000
    await audit.drain()
    from treg.models import CallRecord
    async with session_maker() as db:
        children = (await db.execute(select(CallRecord).where(CallRecord.credential_tier == "platform-overflow"))).scalars().all()
    refused = [c for c in children if (c.error_response or "").endswith("contract")]
    assert len(children) == 2 and len(refused) == 1, "both child attempts are on the record"
    assert refused[0].cost_charged_micro == 0 and refused[0].cost_observed_micro == 0


async def test_orthogonals_own_422_on_the_skip_direct_ladder_is_a_typed_503_naming_the_refusal(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    """No vendor answer exists on this ladder, so the caller gets treg's typed 503 that says the
    relay refused THIS request - never Orthogonal's envelope as if Apollo had answered - and the
    aggregator stays healthy for the next caller."""
    await _route(endpoint_id=APOLLO_SEARCH_EP, provider="apollo", method="POST", path=APOLLO_SEARCH_PATH,
                 price_micro=2_000, ratio=None)
    now = utcnow_naive()
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, "apollo", LatestState(
            "apollo", 0.0, "USD", now, "exact", exhausted_until=now + timedelta(hours=1), health="exhausted").to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()

    async def never(*a, **k):
        raise AssertionError("the direct relay must not run")
    monkeypatch.setattr(call_service, "relay", never)
    seen = []
    monkeypatch.setattr(O, "_send", _orthogonal([(422, ORTHOGONAL_OWN_422)], seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{APOLLO_SEARCH_EP}", json={"person_titles": ["cto"], "per_page": 500})
    assert r.status_code == 503 and r.headers["X-Treg-Error"] == "1", r.text
    detail = r.json()["detail"]
    assert detail["error"] == "provider_capacity_unavailable" and detail["provider"] == "apollo"
    assert "contract" in detail["message"] and "body.per_page exceeds 100" in detail["message"]
    assert "nothing was charged" in detail["message"]
    assert "success" not in r.json() and r.headers.get("X-Treg-Cost-Micro") in (None, "0")
    assert await _balance(clients) == before and await _holds() == []
    lock = await _orthogonal_lock()
    assert lock is None or not lock.is_active(), "one refused request must not take the relay offline"
    assert len(seen) == 1


async def test_orthogonal_5xx_still_marks_the_aggregator_unhealthy(clients: AsyncClient, overflow_on, monkeypatch):
    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"nope"}'))
    monkeypatch.setattr(O, "_send", _orthogonal([(503, {"success": False, "error": "upstream gateway timeout"})], []))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_capacity_unavailable"
    lock = await _orthogonal_lock()
    assert lock is not None and lock.is_active()
    assert await _holds() == []


# ---- the catalog discloses what a relayed call bills -------------------------------------------

async def test_catalog_get_discloses_the_overflow_price_of_a_free_endpoint(clients: AsyncClient, overflow_on):
    """apollo.people.search is catalog-free and billed $0.002 through Orthogonal 8,810 times on
    2026-09-08; nothing on the read surface said a free endpoint could bill. The route's price now
    rides on the endpoint row whenever the deployment can relay it."""
    await _route(endpoint_id=APOLLO_SEARCH_EP, provider="apollo", method="POST", path=APOLLO_SEARCH_PATH,
                 price_micro=2_000, ratio=None)
    r = await clients.get(f"/catalog/endpoints/{APOLLO_SEARCH_EP}")
    assert r.status_code == 200, r.text
    ep = r.json()["endpoint"]
    assert ep["cost"]["usd"] == 0 and ep["platform_eligible"] is True
    assert ep["overflow_price_usd"] == 0.002 and ep["overflow_via"] == "orthogonal"
    assert ep["overflow_price_unit"] == "call"
    assert any("overflow relay (orthogonal)" in h and "$0.002 per call" in h for h in r.json()["hints"])


async def test_catalog_get_says_nothing_about_overflow_when_the_deployment_cannot_relay(
    clients: AsyncClient, overflow_on, monkeypatch,
):
    await _route(endpoint_id=APOLLO_SEARCH_EP, provider="apollo", method="POST", path=APOLLO_SEARCH_PATH,
                 price_micro=2_000, ratio=None)
    # a disabled route is no route
    await _route(endpoint_id=EP, price_micro=3_000, enabled=False)
    r = await clients.get(f"/catalog/endpoints/{EP}")
    assert "overflow_price_usd" not in r.json()["endpoint"]
    # mode off: the price is not a price this deployment can bill
    monkeypatch.setenv("TREG_OVERFLOW_MODE", "off")
    get_settings.cache_clear()
    r = await clients.get(f"/catalog/endpoints/{APOLLO_SEARCH_EP}")
    assert "overflow_price_usd" not in r.json()["endpoint"]
    assert not any("overflow relay" in h for h in r.json()["hints"])


@pytest.mark.parametrize('skip_direct', [False, True])
@pytest.mark.parametrize('cap,status', [('0.0015', 402), ('0.003', 200)])
async def test_caller_ceiling_checks_actual_overflow_reserve(
    clients, overflow_on, monkeypatch, skip_direct, cap, status,
):
    await _route(price_micro=3000)
    if skip_direct:
        await _exhausted()
    monkeypatch.setattr(call_service, 'relay', _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen = []
    envelope = {'success': True, 'data': VENDOR_BODY, 'priceCents': 0.3,
                'billing': {'chargedPriceCents': 0.3}}
    monkeypatch.setattr(O, '_send', _orthogonal([(200, envelope)], seen))
    before = await _balance(clients)
    response = await clients.get(f'/call/{EP}?aweme_id=7',
                                headers={'X-Treg-Route-Max-Cost': cap})
    assert response.status_code == status, response.text
    assert before - await _balance(clients) == (3000 if status == 200 else 0)
    assert len(seen) == (1 if status == 200 else 0)
    assert await _holds() == []
    if status == 402:
        assert response.json()['detail']['error'] == 'route_max_cost'
        async with session_maker() as db:
            rows = (await db.execute(select(OverflowSpend))).scalars().all()
            assert all(row.cost_micro == 0 for row in rows)
