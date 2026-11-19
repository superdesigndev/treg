"""The per-call outcome: what a v2 row's own declaration judged, stored on `CallRecord`."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import CallRecord

from test_routing import _balance, _relay_by_provider

SCRAPE = "branddev.web.scrape"


@pytest.fixture
def branddev_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_BRANDDEV", "PLATFORM-BRANDDEV-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "branddev")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _answer(credits: int, **markdown) -> dict:
    empty = {"requested": False, "success": None, "data": None}
    return {"url": "https://x.test/", "markdown": {"requested": True, **markdown},
            "html": empty, "images": empty, "screenshot": empty,
            "key_metadata": {"credits_consumed": credits}}


async def test_a_call_stores_what_its_v2_row_judged(clients: AsyncClient, branddev_on, monkeypatch):
    """A page, a blocked page, a page that does not exist and an undecided status each land on
    their own row: `hit`, `miss`, `not_found`, and NULL for the status rule. Each costs what
    Context.dev consumed for it: the 404 page its credit although it is no hit, the blocked page
    nothing."""
    await audit.drain()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [
        (200, _answer(1, success=True, data="# A page")),
        (200, _answer(0, success=False, data=None, error_code="WEBSITE_BLOCKED")),
        (200, _answer(1, success=False, data=None, error_code="NOT_FOUND")),
        (500, {"message": "upstream exploded"}),
    ]}, []))
    for page in ("a", "b", "c", "d"):
        r = await clients.post(f"/call/{SCRAPE}", json={"url": f"https://x.test/{page}",
                                                        "formats": {"markdown": True}})
        assert r.status_code in (200, 500), r.text
    await audit.drain()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.endpoint_id == SCRAPE)
                                 .order_by(CallRecord.id))).scalars().all()
    assert [(r.status_code, r.outcome) for r in rows] == [
        (200, "hit"), (200, "miss"), (200, "not_found"), (500, None)]
    credit = rows[0].cost_charged_micro
    assert credit and credit > 0
    assert [r.cost_charged_micro or 0 for r in rows] == [credit, 0, credit, 0]




async def test_the_routed_job_still_charges_nothing_for_a_page_that_does_not_exist(
        clients: AsyncClient, branddev_on, monkeypatch):
    """Through `treg.web.extract` the same 404 page ends the job free: a routed call that finds
    nothing charges nothing, whatever the provider consumed."""
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"*": [
        (200, _answer(1, success=False, data=None, error_code="NOT_FOUND"))]}, seen))
    before = await _balance(clients)
    r = await clients.post("/call/treg.web.extract", json={"url": "https://x.test/gone"})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "route_not_found", r.text
    assert [t["endpoint_id"] for t in r.json()["detail"]["tried"]] == [SCRAPE]
    assert await _balance(clients) == before
