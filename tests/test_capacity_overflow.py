"""Step E of docs/PROVIDER-CAPACITY-PLAN.md — the overflow child cycle: the same vendor endpoint
served through a treg-owned aggregator account when treg's own account fails a tier-4 call.
Off by default; shadow mode never changes the caller's answer."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit, ratestore
from treg.application.call import overflow as O
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.routes_view import view as routes_view
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.models import Hold, LedgerEntry, OverflowRoute, OverflowSpend
from treg.timeutil import utcnow_naive

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


async def _route(aggregator="orthogonal", price_micro=3_000, enabled=True):
    async with session_maker() as db:
        db.add(OverflowRoute(endpoint_id=EP, aggregator=aggregator, provider="tikhub", method="GET", path=EP_PATH,
                             agg_slug="tikhub", agg_path=EP_PATH, agg_price_micro=price_micro, agg_unit="call",
                             ratio=3.0, enabled=enabled, last_verified_at=utcnow_naive()))
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
        state = LatestState.from_json(await ratestore.kv_get(db, STATE_NS, "overflow:orthogonal"))
    assert state.is_exhausted()
    # …and the next call skips the unhealthy aggregator and uses Monid
    capacity_view.invalidate()
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
