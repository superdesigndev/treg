"""A membership's default key is created once under concurrent first requests, on PostgreSQL.

Opt in with TREG_TEST_DB_URL naming a local instance; SQLite serializes writers and cannot show it.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from treg.api import app
from treg.domain.identity import api_keys as managed_keys
from treg.infra.db import reset_db, session_maker
from treg.models import ApiKey, ApiKeyEvent, Membership, User
from conftest import verified_signup

PARALLEL = 8


@pytest.fixture
async def local_postgres():
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires disposable PostgreSQL via TREG_TEST_DB_URL")
    assert engine.url.host in {"127.0.0.1", "localhost", "::1"}, "local test DB only"
    await reset_db()


async def _default_key_count(membership_id: int) -> int:
    async with session_maker() as db:
        return (await db.execute(select(func.count()).select_from(ApiKey).where(
            ApiKey.membership_id == membership_id, ApiKey.kind == managed_keys.DEFAULT_KIND,
        ))).scalar_one()


async def _drop_keys(membership_id: int) -> None:
    async with session_maker() as db:
        key_ids = select(ApiKey.id).where(ApiKey.membership_id == membership_id)
        await db.execute(delete(ApiKeyEvent).where(ApiKeyEvent.key_id.in_(key_ids)))
        await db.execute(delete(ApiKey).where(ApiKey.membership_id == membership_id))
        await db.commit()


async def test_two_sessions_creating_the_default_key_both_get_the_same_key(local_postgres):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        signup = await verified_signup(client, json={"email": "first-use@example.dev"})
        assert signup.status_code == 200, signup.text
    async with session_maker() as db:
        membership = (await db.execute(select(Membership))).scalars().first()
        membership_id, user_id = membership.id, membership.user_id
    await _drop_keys(membership_id)

    first_inserted = asyncio.Event()
    second_started = asyncio.Event()

    async def first() -> int:
        async with session_maker() as db:
            m, u = await db.get(Membership, membership_id), await db.get(User, user_id)
            key = await managed_keys.ensure_default_key(db, m, u)
            first_inserted.set()
            await second_started.wait()
            await asyncio.sleep(0.3)
            await db.commit()
            return key.id

    async def second() -> int:
        await first_inserted.wait()
        async with session_maker() as db:
            m, u = await db.get(Membership, membership_id), await db.get(User, user_id)
            second_started.set()
            key = await managed_keys.ensure_default_key(db, m, u)
            await db.commit()
            return key.id

    a, b = await asyncio.gather(first(), second())
    assert a == b
    assert await _default_key_count(membership_id) == 1


async def test_concurrent_first_requests_with_a_new_membership_token_do_not_fail(
    local_postgres, monkeypatch,
):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        signup = await verified_signup(client, json={"email": "burst@example.dev"})
        assert signup.status_code == 200, signup.text
        created = await client.post(
            "/orgs", headers={"X-Treg-Token": signup.json()["token"]}, json={"name": "Burst"})
        assert created.status_code == 200, created.text
        team_token, org_id = created.json()["token"], created.json()["org_id"]
        async with session_maker() as db:
            membership_id = (await db.execute(
                select(Membership.id).where(Membership.org_id == org_id))).scalars().first()
        await _drop_keys(membership_id)

        # Hold every request between its read of the default key and its insert until all have read,
        # so each sees no row: the interleaving that two processes meet by chance.
        barrier = asyncio.Barrier(PARALLEL)
        real_flush = AsyncSession.flush
        released = False

        async def flush(self, *args, **kwargs):
            nonlocal released
            if not released and any(
                isinstance(o, ApiKey) and o.kind == managed_keys.DEFAULT_KIND for o in self.new
            ):
                try:
                    await asyncio.wait_for(barrier.wait(), 5)
                except (asyncio.TimeoutError, asyncio.BrokenBarrierError):
                    pass
                released = True
            return await real_flush(self, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "flush", flush)

        results = await asyncio.gather(*[
            client.get("/auth/me", headers={"X-Treg-Token": team_token}) for _ in range(PARALLEL)
        ])
    assert [r.status_code for r in results] == [200] * PARALLEL
    assert await _default_key_count(membership_id) == 1
