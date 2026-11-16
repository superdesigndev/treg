"""A row whose provider-reported figure settles the call holds its declared worst case, so calls
in flight together never settle above what the team holds."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg.api import app
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import LedgerEntry

EP = "crawl4ai.web.extract.batch"
WORST_CREDITS = 92.5
CALLS = 40
BODY = {"urls": [f"https://a.test/{i}" for i in range(25)], "format": "md"}


class _Stream(httpx.AsyncByteStream):
    def __init__(self, gate: asyncio.Event, summary: bool) -> None:
        self.gate, self.summary = gate, summary

    async def __aiter__(self):
        await self.gate.wait()
        yield b'{"url":"https://a.test/0","cost":3.7}\n'
        if self.summary:
            yield json.dumps({"summary": {"cost": WORST_CREDITS}}).encode() + b"\n"


class _Upstream(httpx.AsyncBaseTransport):
    def __init__(self, *, summary: bool = True) -> None:
        self.gate, self.summary, self.requests = asyncio.Event(), summary, 0

    async def handle_async_request(self, request):
        self.requests += 1
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"},
                              stream=_Stream(self.gate, self.summary), request=request)


@pytest.fixture
def worst_case_row(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "fake-local-key")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai")
    get_settings.cache_clear()
    cat = catalog_store.load()
    ep = copy.deepcopy(cat.by_id[EP])
    ep["cost"]["worst_case"] = WORST_CREDITS
    monkeypatch.setitem(cat.by_id, EP, ep)
    yield
    get_settings.cache_clear()


async def _settles(org_id: int) -> list[dict]:
    async with session_maker() as db:
        return [m or {} for m in (await db.execute(select(LedgerEntry.meta).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle"))).scalars().all()]


async def _parallel(clients: AsyncClient, upstream: _Upstream) -> list[httpx.Response]:
    app.state.http = AsyncClient(transport=upstream, base_url="https://upstream.test")
    tasks = [asyncio.create_task(clients.post(f"/call/{EP}", json=BODY)) for _ in range(CALLS)]
    for _ in range(400):
        await asyncio.sleep(0.02)
        if sum(t.done() for t in tasks) + upstream.requests >= CALLS:
            break
    upstream.gate.set()
    return await asyncio.gather(*tasks)


async def test_parallel_usage_calls_never_settle_above_what_the_team_holds(clients, worst_case_row):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    before = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
    upstream = _Upstream()
    responses = await _parallel(clients, upstream)
    settles = await _settles(org_id)
    admitted = sum(r.status_code == 200 for r in responses)
    assert admitted == len(settles) == upstream.requests
    assert sum(r.status_code == 402 for r in responses) == CALLS - admitted
    assert all(m["reserved_micro"] >= m["settled_micro"] for m in settles)
    assert sum(int(m["block_shortfall_micro"]) for m in settles) == 0
    assert sum(int(m["consumed_micro"]) for m in settles) <= before


async def test_a_missing_figure_still_settles_at_the_estimate_not_the_worst_case(clients, worst_case_row):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    upstream = _Upstream(summary=False)
    upstream.gate.set()
    app.state.http = AsyncClient(transport=upstream, base_url="https://upstream.test")
    assert (await clients.post(f"/call/{EP}", json=BODY)).status_code == 200
    [settle] = await _settles(org_id)
    assert settle["reserved_micro"] > settle["settled_micro"] > 0
