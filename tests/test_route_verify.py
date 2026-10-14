"""`X-Treg-Route-Verify`: an email find checked in the same call by `treg.people.email.verify`.

The check is an ordinary linked child call (`:v`) with its own hold, charge and verdict; the find's
answer is not altered, and a check that cannot run never fails the find."""

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
def verifiers_on(monkeypatch, platform_on):  # noqa: F811
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


async def test_a_hit_is_checked_by_bounceban_and_the_verdict_is_stored(clients: AsyncClient, verifiers_on, monkeypatch):
    """Even a hit the finder calls verified is checked: a finder marked addresses verified that two
    verifiers both called invalid. The invalid address is still returned, with its verdict."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay({"tomba": [TOMBA_HIT], "bounceban": [_bb("undeliverable")]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{FIND}", json=ASK, headers=ON)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"]["email"] == "patrick@stripe.com" and d["output"]["verified"] is True
    assert seen == ["tomba", "bounceban"], "BounceBan first for this check"
    v = d["_treg"]["verification"]
    assert v["verdict"] == "invalid" and v["checked"] is True and "reason" not in v
    assert v["served_by"] == "bounceban.people.email.verify" and v["cost_micro"] > 0
    total = int(r.headers["X-Treg-Cost-Micro"])
    assert total == d["_treg"]["charged_micro"] == TOMBA_FIND_PRICE + v["cost_micro"]
    assert before - await _balance(clients) == total
    assert sum(t["charged_micro"] for t in d["_treg"]["tried"]) == TOMBA_FIND_PRICE, "tried lists the find only"
    call_id = r.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        settled = {e.call_id for e in (await db.execute(select(LedgerEntry))).scalars() if e.kind == "settle"}
    assert settled == {f"{call_id}:r0", f"{call_id}:v:r0"}
    rows = {(x.endpoint_id, x.call_ref): x for x in await _rows()}
    assert rows[(FIND, call_id)].cost_charged_micro == total
    assert (VERIFY, f"{call_id}:v") in rows, "the check's own linked row"
    assert rows[("bounceban.people.email.verify", f"{call_id}:v:r0")].verdict == "invalid"


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
