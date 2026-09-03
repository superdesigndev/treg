"""Connection discipline on `/call/`: a call in flight holds NO pooled DB connection.

Found live 2026-08-24 (bootoshi's feedback #9/#10, reproduced from our own token): 15 concurrent
metered calls each took ~31 s and one died as a bare 500, while the provider answered every one in
under a second. The request session had auto-begun a transaction after `ledger.reserve` committed
(the org refresh, the secret loads) and held that connection for the whole upstream round trip;
`_platform_settle` then opened its own session for a SECOND connection. Two per in-flight call
against a 15-slot pool deadlocked at 15: every settle waited on a slot only another waiting call
could free, until `pool_timeout` killed one and the rest cascaded.

The fix is one commit before `relay()`. These tests pin (a) the invariant itself — zero checked-out
connections while the upstream is being called — on both the metered and the own-key paths, (b) a
burst larger than the pool completing at provider speed with every charge settled, and (c) the
typed 503 a genuinely saturated pool now answers instead of an anonymous 500.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from treg import api as A
from treg.domain.catalog import stats as catalog_stats
from treg.application.call import service as call_service
from treg.routers import call as call_routes
from treg import audit
from treg.domain import money as ledger
from treg.config import get_settings
from treg.infra.db import _background_engine, _engine, session_maker
from treg.infra.upstream.relay import relay as upstream_relay
from treg.models import CallRecord, Hold

from test_marketplace_call import EP, EP_MICRO, platform_on  # noqa: F401 — fixture reuse
from test_mcp import _call_tool, mcp_session

from sqlalchemy.exc import TimeoutError as PoolTimeoutError


@pytest.fixture
async def dispose_exhausted_pool_on_its_own_loop():
    """A pool wait binds its asyncio queue to this test's loop; do not leak it to the next test."""
    yield
    await audit.drain()
    await _engine.dispose()


def _relay_that_checks_the_pool(seen: list[int]):
    """The real relay, with the pool's checked-out count sampled at the moment the upstream is
    called. Recorded rather than asserted here: an assertion inside the relay would surface as a
    502, not a test failure."""
    original = upstream_relay

    async def _relay(*args, **kwargs):
        seen.append(_engine.pool.checkedout())
        return await original(*args, **kwargs)

    return _relay


async def test_a_metered_call_holds_no_db_connection_while_upstream_is_called(
    clients: AsyncClient, platform_on, monkeypatch,
):
    await audit.drain()  # an earlier test's fire-and-forget audit row must not be counted
    seen: list[int] = []
    monkeypatch.setattr(call_service, "relay", _relay_that_checks_the_pool(seen))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert seen == [0], f"pooled connections held during the upstream round trip: {seen}"


async def test_an_own_key_call_holds_no_db_connection_while_upstream_is_called(
    clients: AsyncClient, monkeypatch,
):
    """The unmetered path pins one too (the secret loads auto-begin a transaction), and it has no
    second session to deadlock against — it just eats a slot per in-flight call for nothing."""
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    await audit.drain()
    seen: list[int] = []
    monkeypatch.setattr(call_service, "relay", _relay_that_checks_the_pool(seen))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.json()["auth"] == "Bearer MKKEY"
    assert seen == [0], f"pooled connections held during the upstream round trip: {seen}"


async def test_a_burst_larger_than_the_pool_settles_every_call_at_provider_speed(
    clients: AsyncClient, platform_on, monkeypatch, dispose_exhausted_pool_on_its_own_loop,
):
    """20 metered calls, all forced INTO the upstream window at once (each waits at the relay until
    every one has arrived). The pool has 15 slots (5 + 10 overflow, the same defaults the test engine
    uses). Before the fix this deadlocked: 20 requests × a held connection, 20 settles waiting for a
    slot nobody could free, until the 30 s pool_timeout killed some and the rest cascaded."""
    N = 20
    original = upstream_relay
    arrived = 0
    everyone_in = asyncio.Event()

    async def _relay_after_everyone_arrives(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == N:
            everyone_in.set()
        await asyncio.wait_for(everyone_in.wait(), timeout=10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(call_service, "relay", _relay_after_everyone_arrives)
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    before = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]

    t0 = time.monotonic()
    responses = await asyncio.gather(*(
        clients.get(f"/call/{EP}?aweme_id=burst-{i}") for i in range(N)))
    wall = time.monotonic() - t0

    assert [r.status_code for r in responses] == [200] * N, [r.status_code for r in responses]
    assert wall < 10, f"a {N}-call burst took {wall:.1f}s — that is the pool timeout, not the provider"
    after = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
    assert after == before - N * EP_MICRO, "every call settled at its price"
    async with session_maker() as db:
        open_holds = (await db.execute(select(Hold).where(Hold.org_id == org_id))).scalars().all()
    assert open_holds == [], "no settle was lost to the pool"


async def test_a_saturated_pool_answers_a_typed_503_not_an_anonymous_500(
    clients: AsyncClient, monkeypatch,
):
    async def _no_slot(*args, **kwargs):
        raise PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached, connection timed out")

    monkeypatch.setattr(call_service, "_resolve_call", _no_slot)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503, r.text
    assert r.json()["treg_saturated"] is True
    assert r.headers.get("Retry-After") == "2"


@pytest.mark.skipif(
    not os.environ.get("TREG_TEST_DB_URL"), reason="requires the Postgres test database"
)
async def test_a_catalog_search_storm_cannot_starve_calls_of_the_pool(
    clients: AsyncClient, platform_on, monkeypatch,
    dispose_exhausted_pool_on_its_own_loop,
):
    """The public search path must not multiply one slow observation query by request concurrency.

    This recreates the production failure at the HTTP boundary: 100 identical searches arrive while
    the observation query is slow, then ordinary Marketplace calls need the same 15-slot pool. The
    old request-owned query filled every slot and the calls timed out with pool 503s. Search now
    returns from an empty/stale process cache while one task owns the only refresh connection.
    """
    original = catalog_stats.observed
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    refresh_calls = 0

    async def _slow_observed(db, endpoint_ids, **kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        # Force a real checkout before holding the refresh open. One connection is expected; one
        # per search request is the production defect this test reproduces.
        await db.execute(select(CallRecord.id).limit(1))
        refresh_started.set()
        await asyncio.wait_for(release_refresh.wait(), timeout=15)
        return await original(db, endpoint_ids, **kwargs)

    monkeypatch.setattr(catalog_stats, "observed", _slow_observed)
    searches = [asyncio.create_task(clients.get("/catalog/search?q=tiktok&limit=25"))
                for _ in range(100)]
    await asyncio.wait_for(refresh_started.wait(), timeout=5)
    await asyncio.sleep(0.25)  # let the old path fill all 15 pool slots before calls arrive

    try:
        calls = await asyncio.gather(*(
            clients.get(f"/call/{EP}?aweme_id=catalog-mix-{i}") for i in range(20)
        ))
    finally:
        release_refresh.set()

    search_responses = await asyncio.gather(*searches)
    await A.app.state.endpoint_observation_reader.wait_for_idle()
    assert [r.status_code for r in search_responses] == [200] * 100
    assert [r.status_code for r in calls] == [200] * 20, [
        (r.status_code, r.text) for r in calls
    ]
    assert refresh_calls == 1, "100 identical searches must share one refresh task"


@pytest.mark.skipif(
    not os.environ.get("TREG_TEST_DB_URL"), reason="requires the Postgres test database"
)
async def test_http_and_mcp_catalog_search_share_one_nonblocking_refresh(
    clients: AsyncClient, monkeypatch, dispose_exhausted_pool_on_its_own_loop,
):
    """The agent entry point must share HTTP's cache, task, and single refresh connection."""
    original = catalog_stats.observed
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    refresh_calls = 0

    async def _slow_observed(db, endpoint_ids, **kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        await db.execute(select(CallRecord.id).limit(1))
        refresh_started.set()
        await asyncio.wait_for(release_refresh.wait(), timeout=15)
        return await original(db, endpoint_ids, **kwargs)

    monkeypatch.setattr(catalog_stats, "observed", _slow_observed)
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as mcp_client:
        http_search = asyncio.create_task(clients.get("/catalog/search?q=backlinks&limit=8"))
        mcp_search = asyncio.create_task(_call_tool(
            mcp_client, "catalog_search", {"query": "backlinks", "limit": 8}, token=token,
        ))
        await asyncio.wait_for(refresh_started.wait(), timeout=5)
        try:
            http_response, mcp_response = await asyncio.wait_for(
                asyncio.gather(http_search, mcp_search), timeout=5,
            )
            assert http_response.status_code == 200, http_response.text
            assert mcp_response["results"]
            # The refresh holds exactly one connection and it is NOT one of the API's. Before the
            # pool split this read `_engine.pool.checkedout() == 1`, because the refresh ran on the
            # request pool; now the same invariant is the stronger pair below. SQLite aliases all
            # three engines, which is why this whole test is Postgres-only.
            assert _engine.pool.checkedout() == 0, (
                "a catalog search must hold no request-pool connection while the refresh runs"
            )
            assert _background_engine.pool.checkedout() == 1, (
                "only the independent refresh session may hold a pooled connection"
            )
            assert refresh_calls == 1, "HTTP and MCP must join the same process refresh task"
        finally:
            release_refresh.set()

    await A.app.state.endpoint_observation_reader.wait_for_idle()


async def test_a_settle_that_loses_the_pool_once_retries_and_still_charges(
    clients: AsyncClient, platform_on, monkeypatch,
):
    """A settle that gives up forfeits real revenue (the hold is reaped in the org's favour), so a
    transient pool wait gets exactly one retry — and nothing else does."""
    original = ledger.settle_in_transaction
    calls = 0

    async def _flaky_settle(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PoolTimeoutError("QueuePool limit reached")
        return await original(*args, **kwargs)

    monkeypatch.setattr(ledger, "settle_in_transaction", _flaky_settle)
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    before = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and calls == 2
    assert r.headers.get("X-Treg-Cost-Micro") == str(EP_MICRO)
    assert (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"] == before - EP_MICRO


@pytest.mark.skipif(
    not os.environ.get("TREG_TEST_DB_URL"), reason="requires the Postgres test database"
)
async def test_auth_releases_a_single_slot_pool_before_an_application_session(
    clients: AsyncClient,
):
    """An auth dependency and its application use case must use one pool slot in sequence."""
    await audit.drain()
    original_bind = session_maker.kw["bind"]
    limited_engine = create_async_engine(
        get_settings().database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
    )
    session_maker.kw["bind"] = limited_engine
    try:
        responses = await asyncio.gather(*(clients.get("/connections") for _ in range(6)))
        assert [r.status_code for r in responses] == [200] * 6, [
            (r.status_code, r.text) for r in responses
        ]
    finally:
        session_maker.kw["bind"] = original_bind
        await limited_engine.dispose()
