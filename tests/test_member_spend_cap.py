"""A daily dollar limit per member on treg's keys, with one team default."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.db import session_maker
from treg.models import MemberSpend

EP = "tikhub.tiktok.video.comments"   # $0.001 per successful call on treg's key
EP_MICRO = 1_000


@pytest.fixture
def platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATFORM-TIKHUB-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _org_id(clients: AsyncClient) -> int:
    return (await clients.get("/auth/me")).json()["org_id"]


async def _owner_id(clients: AsyncClient, org_id: int) -> int:
    members = (await clients.get(f"/orgs/{org_id}/members")).json()
    return next(m["user_id"] for m in members if m["role"] == "owner")


async def _agent(clients: AsyncClient, org_id: int, name: str, **body) -> dict:
    r = await clients.post(f"/orgs/{org_id}/agents", json={"name": name, **body})
    assert r.status_code == 200, r.text
    return r.json()


async def _call(clients: AsyncClient, token: str, n: int):
    return await clients.get(f"/call/{EP}?aweme_id={n}", headers={"X-Treg-Token": token})


async def _member(clients: AsyncClient, org_id: int, user_id: int) -> dict:
    members = (await clients.get(f"/orgs/{org_id}/members")).json()
    return next(m for m in members if m["user_id"] == user_id)


async def test_team_default_limits_members_and_agents_but_not_the_owner(clients, platform_on):
    org_id = await _org_id(clients)
    r = await clients.patch(f"/orgs/{org_id}/settings",
                            json={"member_daily_spend_cap_micro": EP_MICRO})   # one call a day
    assert r.status_code == 200, r.text
    agent = await _agent(clients, org_id, "rep-1")

    assert (await _call(clients, agent["token"], 1)).status_code == 200
    refused = await _call(clients, agent["token"], 2)
    assert refused.status_code == 429, refused.text
    detail = refused.json()["detail"]
    assert detail["error"] == "member_spend_cap_reached"
    assert detail["spent_today_micro"] == EP_MICRO and detail["daily_spend_cap_micro"] == EP_MICRO
    # An agent learns when to try again instead of retrying until midnight.
    assert detail["resets_at"].endswith("T00:00:00Z")
    assert 0 < int(refused.headers["Retry-After"]) == detail["retry_after_s"] <= 86400
    assert "Ask a team admin to raise your limit" in detail["message"]
    assert "connections connect" not in detail["message"]

    owner_token = clients.headers["X-Treg-Token"]
    for n in (3, 4):
        assert (await _call(clients, owner_token, n)).status_code == 200, "an owner never inherits it"

    row = await _member(clients, org_id, agent["user_id"])
    assert row["daily_spend_cap_micro"] is None
    assert row["effective_daily_spend_cap_micro"] == EP_MICRO
    assert row["spent_today_micro"] == EP_MICRO
    me = (await clients.get("/usage/me", headers={"X-Treg-Token": agent["token"]})).json()
    assert me["daily_spend_cap_micro"] == EP_MICRO and me["spent_today_micro"] == EP_MICRO


async def test_a_members_own_limit_overrides_the_team_default(clients, platform_on):
    org_id = await _org_id(clients)
    await clients.patch(f"/orgs/{org_id}/settings", json={"member_daily_spend_cap_micro": 0})
    tight = await _agent(clients, org_id, "rep-tight")
    loose = await _agent(clients, org_id, "rep-loose")
    assert (await _call(clients, tight["token"], 1)).status_code == 429

    r = await clients.patch(f"/orgs/{org_id}/members/{loose['user_id']}/spend-cap",
                            json={"daily_spend_cap_micro": -1})
    assert r.status_code == 200, r.text
    assert r.json()["effective_daily_spend_cap_micro"] is None
    assert (await _call(clients, loose["token"], 2)).status_code == 200

    r = await clients.patch(f"/orgs/{org_id}/members/{loose['user_id']}/spend-cap",
                            json={"daily_spend_cap_micro": None})
    assert r.json()["effective_daily_spend_cap_micro"] == 0, "null follows the team again"


async def test_an_owner_can_be_limited_by_their_own_figure(clients, platform_on):
    org_id = await _org_id(clients)
    owner_id = await _owner_id(clients, org_id)
    await clients.patch(f"/orgs/{org_id}/members/{owner_id}/spend-cap",
                        json={"daily_spend_cap_micro": 0})
    r = await _call(clients, clients.headers["X-Treg-Token"], 1)
    assert r.status_code == 429 and r.json()["detail"]["error"] == "member_spend_cap_reached"


async def test_an_agent_can_be_minted_with_its_own_limit(clients, platform_on):
    org_id = await _org_id(clients)
    agent = await _agent(clients, org_id, "rep-2", daily_spend_cap_micro=0)
    assert agent["daily_spend_cap_micro"] == 0
    assert (await _call(clients, agent["token"], 1)).status_code == 429
    rotated = await _agent(clients, org_id, "rep-2")
    assert rotated["daily_spend_cap_micro"] == 0, "a rotate keeps the limit"


async def test_unlimited_members_write_no_spend_rows_and_a_release_removes_its_row(clients, platform_on):
    org_id = await _org_id(clients)
    open_agent = await _agent(clients, org_id, "rep-open")
    assert (await _call(clients, open_agent["token"], 1)).status_code == 200
    async with session_maker() as db:
        assert (await db.execute(select(MemberSpend))).scalars().all() == []

    await clients.patch(f"/orgs/{org_id}/settings", json={"member_daily_spend_cap_micro": 1_000_000})
    capped = await _agent(clients, org_id, "rep-capped")
    assert (await _call(clients, capped["token"], 2)).status_code == 200
    async with session_maker() as db:
        rows = (await db.execute(select(MemberSpend))).scalars().all()
    assert [(r.amount_micro, r.settled) for r in rows] == [(EP_MICRO, True)]

    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, EP, EP_MICRO, member_id=rows[0].membership_id)
        await ledger.release(db, call_id, reason="test")
        held = (await db.execute(select(MemberSpend).where(MemberSpend.hold_id == call_id))).all()
    assert held == []


async def test_the_limit_refuses_when_spend_cannot_be_read(clients, platform_on, monkeypatch):
    org_id = await _org_id(clients)
    await clients.patch(f"/orgs/{org_id}/settings", json={"member_daily_spend_cap_micro": 1_000_000})
    agent = await _agent(clients, org_id, "rep-3", daily_call_cap=1)
    real = ledger.member_spent_since

    async def _boom(db, membership_id, since):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(ledger, "member_spent_since", _boom)
    for n in (1, 2, 3):
        r = await _call(clients, agent["token"], n)
        assert r.status_code == 429 and r.json()["detail"]["error"] == "member_spend_cap_unavailable"
    # Retrying the unavailable refusal did not use up the member's one call a day.
    monkeypatch.setattr(ledger, "member_spent_since", real)
    assert (await _call(clients, agent["token"], 4)).status_code == 200


async def test_bad_limits_are_refused(clients, platform_on):
    org_id = await _org_id(clients)
    owner_id = await _owner_id(clients, org_id)
    assert (await clients.patch(f"/orgs/{org_id}/settings",
                                json={"member_daily_spend_cap_micro": -1})).status_code == 422
    assert (await clients.patch(f"/orgs/{org_id}/members/{owner_id}/spend-cap",
                                json={"daily_spend_cap_micro": -2})).status_code == 422
    settings = (await clients.get(f"/orgs/{org_id}/settings")).json()
    assert settings["member_daily_spend_cap_micro"] is None


async def test_a_call_refused_for_dollars_does_not_use_a_call_slot(clients, platform_on):
    org_id = await _org_id(clients)
    agent = await _agent(clients, org_id, "rep-4", daily_call_cap=2, daily_spend_cap_micro=EP_MICRO)
    assert (await _call(clients, agent["token"], 1)).status_code == 200
    for n in (2, 3, 4):
        r = await _call(clients, agent["token"], n)
        assert r.json()["detail"]["error"] == "member_spend_cap_reached"
    await clients.patch(f"/orgs/{org_id}/members/{agent['user_id']}/spend-cap",
                        json={"daily_spend_cap_micro": -1})
    assert (await _call(clients, agent["token"], 5)).status_code == 200, "slot 2 is still free"
    r = await _call(clients, agent["token"], 6)
    assert r.status_code == 429 and "daily usage limit" in r.json()["detail"]


async def test_setting_the_team_default_replaces_member_limits_but_not_an_owners(clients, platform_on):
    org_id = await _org_id(clients)
    owner_id = await _owner_id(clients, org_id)
    capped = await _agent(clients, org_id, "rep-5", daily_spend_cap_micro=5 * EP_MICRO)
    free = await _agent(clients, org_id, "rep-6", daily_spend_cap_micro=-1)
    await clients.patch(f"/orgs/{org_id}/members/{owner_id}/spend-cap",
                        json={"daily_spend_cap_micro": 7 * EP_MICRO})

    r = await clients.patch(f"/orgs/{org_id}/settings", json={"member_daily_spend_cap_micro": EP_MICRO})
    assert r.status_code == 200, r.text
    for agent in (capped, free):
        row = await _member(clients, org_id, agent["user_id"])
        assert row["daily_spend_cap_micro"] is None
        assert row["effective_daily_spend_cap_micro"] == EP_MICRO
    owner = await _member(clients, org_id, owner_id)
    assert owner["daily_spend_cap_micro"] == 7 * EP_MICRO, "the default never applies to an owner"
