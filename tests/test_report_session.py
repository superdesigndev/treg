"""Dashboard reports run behind their own gate, never on an unbounded share of the API pool.

The 30-day usage rollup, the observed-agents roster and spend by tag aggregate whole windows of the
audit and ledger tables. These tests pin which routes take the report session, that a full gate or a
statement timeout answers a typed 429 at once, and that a slot is always given back.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

from treg import crypto
from treg.api import app
from treg.bootstrap_handlers import _report_busy
from treg.infra import db as infra_db
from treg.infra.db import (ReportBusy, get_admin_read_session, get_report_session, reset_db,
                           session_maker)
from treg.models import Membership, Org, User

REPORT_ROUTES = {"/orgs/{org_id}/usage", "/orgs/{org_id}/usage/by-tag",
                 "/orgs/{org_id}/agents/observed"}


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


@pytest.fixture
async def env():
    await reset_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        token = crypto.new_token()
        async with session_maker() as s:
            org = Org(name="Team", slug="team")
            user = User(email="owner@x.dev")
            s.add_all([org, user])
            await s.flush()
            s.add(Membership(user_id=user.id, org_id=org.id, role="owner",
                             token_hash=crypto.hash_token(token)))
            await s.commit()
            org_id = org.id
        yield SimpleNamespace(c=c, org_id=org_id, owner=token)


def _route_dependencies(route: APIRoute) -> set:
    return {dep.call for dep in route.dependant.dependencies}


def test_the_heavy_dashboard_reports_take_the_report_session():
    routes = {r.path: r for r in app.routes if isinstance(r, APIRoute) and r.path in REPORT_ROUTES}
    assert set(routes) == REPORT_ROUTES
    for route in routes.values():
        assert get_report_session in _route_dependencies(route), route.path


@pytest.mark.parametrize("path", ["/orgs/{org}/usage", "/orgs/{org}/usage/by-tag",
                                  "/orgs/{org}/agents/observed"])
async def test_a_report_answers_and_gives_its_slot_back(env, path):
    resp = await env.c.get(path.format(org=env.org_id), headers=_h(env.owner))
    assert resp.status_code == 200, resp.text
    assert infra_db._report_slots()._value == infra_db._REPORT_SLOTS


async def test_a_full_gate_refuses_at_once_with_a_typed_429(env, monkeypatch):
    monkeypatch.setattr(infra_db, "_REPORT_WAIT_S", 0.01)
    slots = infra_db._report_slots()
    for _ in range(infra_db._REPORT_SLOTS):
        await slots.acquire()
    try:
        resp = await env.c.get(f"/orgs/{env.org_id}/usage", headers=_h(env.owner))
        other = await env.c.get("/usage/me", headers=_h(env.owner))
    finally:
        for _ in range(infra_db._REPORT_SLOTS):
            slots.release()
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "2"
    assert resp.json()["report_busy"] == "busy"
    assert other.status_code == 200, "a full report gate must not refuse other routes"


async def test_the_whole_report_is_bounded_not_just_each_statement(monkeypatch):
    """Many statements, each under the statement timeout, still end at the report deadline."""
    monkeypatch.setattr(infra_db, "_REPORT_DEADLINE_S", 0.05)
    probe = FastAPI()
    probe.add_exception_handler(ReportBusy, _report_busy)

    @probe.get("/slow")
    async def slow(db=Depends(get_report_session)):
        for _ in range(20):
            await asyncio.sleep(0.01)
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=probe), base_url="http://probe") as c:
        resp = await c.get("/slow")
    assert resp.status_code == 429
    assert resp.json()["report_busy"] == "timeout"
    assert infra_db._report_slots()._value == infra_db._REPORT_SLOTS


class _Canceled(Exception):
    sqlstate = "57014"


async def test_a_statement_timeout_becomes_report_busy_and_releases_the_slot():
    gen = get_report_session()
    await gen.__anext__()
    with pytest.raises(ReportBusy) as caught:
        await gen.athrow(DBAPIError("SELECT 1", {}, _Canceled()))
    assert caught.value.reason == "timeout"
    assert infra_db._report_slots()._value == infra_db._REPORT_SLOTS


async def test_other_database_errors_pass_through_unchanged():
    gen = get_report_session()
    await gen.__anext__()
    other = DBAPIError("SELECT 1", {}, Exception("boom"))
    with pytest.raises(DBAPIError) as caught:
        await gen.athrow(other)
    assert caught.value is other
    assert infra_db._report_slots()._value == infra_db._REPORT_SLOTS


async def test_an_admin_read_cut_off_by_its_timeout_becomes_report_busy():
    """A polled admin report must not hold the read pool the dashboard reports share."""
    gen = get_admin_read_session()
    await gen.__anext__()
    with pytest.raises(ReportBusy) as caught:
        await gen.athrow(DBAPIError("SELECT 1", {}, _Canceled()))
    assert caught.value.reason == "timeout"


async def test_other_admin_read_errors_pass_through_unchanged():
    gen = get_admin_read_session()
    await gen.__anext__()
    other = DBAPIError("SELECT 1", {}, Exception("boom"))
    with pytest.raises(DBAPIError) as caught:
        await gen.athrow(other)
    assert caught.value is other
