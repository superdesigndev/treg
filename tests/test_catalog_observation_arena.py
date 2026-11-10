"""Arena quotes remain available while the shared observation reader has partial evidence."""

import asyncio
import re
from unittest.mock import AsyncMock

from sqlalchemy import event

from treg.api import app
from treg.application.call import route, service
from treg.config import get_settings
from treg.domain.catalog import stats
from treg.infra.catalog_observations import (
    CachedEndpointObservationReader, PostgresEndpointObservationReader,
)
from treg.infra.db import background_session_maker, session_maker
from treg.models import EndpointDayStat, EndpointStatCursor
from treg.timeutil import utcnow_naive

from test_marketplace_call import platform_on  # noqa: F401 - enrichment fixture dependency
from test_routing import enrichment_on  # noqa: F401 - isolated synthetic provider credentials

HUNTER = "hunter.people.email.find"
WIZA = "wiza.people.email.find"


async def test_quote_keeps_trusted_statistics_and_unmeasured_async_candidates_without_live_queries(
    clients, enrichment_on, monkeypatch,
):
    monkeypatch.setenv("TREG_PLATFORM_KEY_WIZA", "SYNTHETIC-WIZA")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,wiza")
    get_settings.cache_clear()
    at = utcnow_naive()
    async with session_maker() as db:
        db.add(EndpointStatCursor(id="callrecord", caught_up_at=at, updated_at=at))
        for endpoint_id in (HUNTER, WIZA):
            db.add(EndpointDayStat(
                endpoint_id=endpoint_id, day=at.strftime("%Y-%m-%d"), n=24, ok=24,
                hits=18, hit_decided=24, last_ok_at=at,
                latency_seen=24, latency_sample=[100] * 24,
                terminal_reconciled=endpoint_id == HUNTER))
        await db.commit()

    reader = CachedEndpointObservationReader(
        PostgresEndpointObservationReader(background_session_maker))
    monkeypatch.setattr(app.state, "endpoint_observation_reader", reader)
    monkeypatch.setattr(route, "_endpoint_observation_reader", reader)
    no_live = AsyncMock(side_effect=AssertionError("an Arena quote must not aggregate raw audit rows"))
    no_dispatch = AsyncMock(side_effect=AssertionError("a quote must not call a provider"))
    monkeypatch.setattr(stats, "observed", no_live)
    monkeypatch.setattr(service, "execute_call", no_dispatch)
    monkeypatch.setattr(service, "relay", no_dispatch)
    plans = []
    original = route.build_plan

    async def capture_plan(*args, **kwargs):
        plan = await original(*args, **kwargs)
        plans.append(plan)
        return plan

    monkeypatch.setattr(route, "build_plan", capture_plan)
    statements = []
    engines = {factory.kw["bind"].sync_engine
               for factory in (session_maker, background_session_maker)}

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    for engine in engines:
        event.listen(engine, "before_cursor_execute", record)
    request = {
        "capability": "people.email.find", "mode": "compare", "providers": ["hunter", "wiza"],
        "identity": {"full_name": "Test Person", "domain": "example.com"},
        "max_cost_micro": 1_000_000,
    }
    try:
        cold = await asyncio.wait_for(clients.post("/arena/plans", json=request), timeout=5)
        assert cold.status_code == 200, cold.text
        assert all(candidate.confidence == "unmeasured" for candidate in plans[-1].candidates)
        await asyncio.wait_for(reader.wait_for_idle(), timeout=5)

        warm = await asyncio.wait_for(clients.post("/arena/plans", json=request), timeout=5)
        assert warm.status_code == 200, warm.text
        def offered(response):
            return {row["provider"]: row for row in response.json()["providers"]}
        assert set(offered(warm)) == {"hunter", "wiza"}
        assert offered(warm) == offered(cold), "missing observations must not prevent a provider's quote"
        assert warm.json()["balance_micro"] == cold.json()["balance_micro"]
        candidates = {candidate.endpoint["id"]: candidate for candidate in plans[-1].candidates}
        assert candidates[HUNTER].hit_rate == 0.75 and candidates[HUNTER].confidence == "measured"
        assert candidates[WIZA].hit_rate is None and candidates[WIZA].confidence == "unmeasured"
        assert set(await reader.get_many([HUNTER, WIZA])) == {HUNTER}
        assert not any(re.search(r"\bcallrecord\b", sql, re.IGNORECASE) for sql in statements)
        no_live.assert_not_awaited()
        no_dispatch.assert_not_awaited()
    finally:
        await reader.aclose()
        for engine in engines:
            event.remove(engine, "before_cursor_execute", record)
        get_settings.cache_clear()
