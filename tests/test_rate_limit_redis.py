"""The Lua scripts and the policy cache against a real Redis, and the migration on a clean Postgres.

Set TREG_TEST_KV_URL (a scratch Redis) and TREG_TEST_DB_URL (Postgres); the CI Postgres job sets
both. Keys are unique per test so parallel workers never share a lease set.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import anyio
import pytest

from treg.application.ratelimit import background, config_state, limiter
from treg.config import get_settings
from treg.domain.ratelimit import RateLimitConfig
from treg.infra import kv

KV_URL = os.environ.get("TREG_TEST_KV_URL", "")
DB_URL = os.environ.get("TREG_TEST_DB_URL", "")
needs_redis = pytest.mark.skipif(not KV_URL, reason="set TREG_TEST_KV_URL to a scratch Redis")
needs_postgres = pytest.mark.skipif(not DB_URL.startswith("postgresql"), reason="set TREG_TEST_DB_URL to Postgres")


@pytest.fixture
async def redis_store():
    store = kv.RedisStore(KV_URL)
    yield store
    await store.aclose()


def _key() -> str:
    return f"rl:test:{uuid.uuid4().hex}:c"


@needs_redis
async def test_admit_counts_refuses_and_releases(redis_store):
    key = _key()
    try:
        assert await redis_store.rl_admit_key(key, "a", 2, 60_000, True) == (True, True, 1)
        assert await redis_store.rl_admit_key(key, "b", 2, 60_000, True) == (True, True, 2)
        assert await redis_store.rl_admit_key(key, "c", 2, 60_000, True) == (False, False, 3)
        assert await redis_store._client.zcard(key) == 2  # enforce took nothing for the refusal
        assert await redis_store.rl_admit_key(key, "d", 2, 60_000, False) == (True, False, 3)  # shadow
        await redis_store.rl_release_key(key, "a")
        await redis_store.rl_release_key(key, "d")
        assert await redis_store._client.zcard(key) == 1
        assert 0 < await redis_store._client.pttl(key) <= 60_000
    finally:
        await redis_store._client.delete(key)


@needs_redis
async def test_a_lease_ages_out_after_its_ttl_and_renewal_keeps_it(redis_store):
    key = _key()
    try:
        await redis_store.rl_admit_key(key, "kept", None, 300, True)
        await redis_store.rl_admit_key(key, "lost", None, 300, True)
        await asyncio.sleep(0.2)
        assert await redis_store.rl_renew(key, ["kept", "never-admitted"], 300) == 1  # XX: no re-add
        await asyncio.sleep(0.2)
        _, _, n = await redis_store.rl_admit_key(key, "probe", None, 300, False)
        members = set(await redis_store._client.zrange(key, 0, -1))
        assert members == {"kept", "probe"} and n == 2  # the unrenewed lease aged out
    finally:
        await redis_store._client.delete(key)


@needs_redis
async def test_publish_writes_only_a_strictly_newer_version(redis_store):
    key = f"rl:test:{uuid.uuid4().hex}:config"
    doc = lambda v: json.dumps({"version": v, "config": {}})
    try:
        assert await redis_store.rl_publish(key, doc(2), 2) == 2
        assert await redis_store.rl_publish(key, doc(1), 1) == 2  # stale: refused
        assert await redis_store.rl_publish(key, doc(2), 2) == 2  # same: no rewrite
        assert json.loads(await redis_store.rl_get(key))["version"] == 2
        assert await redis_store.rl_publish(key, doc(3), 3) == 3
        await redis_store._client.delete(key)  # Redis lost its data
        assert await redis_store.rl_publish(key, doc(1), 1) == 1  # an empty cache takes anything...
    finally:
        await redis_store._client.delete(key)


@pytest.fixture
def live_redis(monkeypatch):
    """The app's store pointed at the real Redis, with a per-test global key."""
    monkeypatch.setenv("TREG_KV_URL", KV_URL)
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "redis-admin")
    get_settings.cache_clear()
    monkeypatch.setattr(kv, "_store", None)
    monkeypatch.setattr(kv, "RL_GLOBAL_KEY", _key())
    monkeypatch.setattr(kv, "RL_CONFIG_KEY", f"rl:test:{uuid.uuid4().hex}:config")
    limiter.reset_for_tests()
    background.reset_for_tests()
    yield
    limiter.reset_for_tests()
    get_settings.cache_clear()


def _policy(**update) -> dict:
    doc = get_settings().rate_limit_defaults.model_dump(mode="json")
    doc.update(update)
    return doc


@needs_redis
async def test_concurrent_revert_and_stale_republish_keep_the_newer_policy(clients, live_redis, monkeypatch):
    """...and a stale writer after a Redis flush is never what a worker serves."""
    from treg.application.ratelimit import admin as rl_admin, config_sync
    h = {"X-Treg-Token": "redis-admin"}
    v1 = (await clients.put("/admin/rate-limit/config", headers=h, json={
        "config": _policy(mode="enforce", global_concurrency=50), "note": "go",
        "enforce_approval": "ok"})).json()["version"]
    from treg.infra.db import session_maker
    from treg.models import RateLimitConfigVersion
    async with session_maker() as db:
        stale = await db.get(RateLimitConfigVersion, v1)
    real_latest = rl_admin._latest
    entered, release = asyncio.Event(), asyncio.Event()

    async def stale_read(db):
        entered.set()  # the republish has its pre-revert row; it publishes only after the revert
        await release.wait()
        return stale

    monkeypatch.setattr(rl_admin, "_latest", stale_read)
    republish = asyncio.create_task(clients.post("/admin/rate-limit/config/republish", headers=h))
    await asyncio.wait_for(entered.wait(), 2)
    monkeypatch.setattr(rl_admin, "_latest", real_latest)
    v2 = (await clients.post("/admin/rate-limit/config/revert-to-shadow", headers=h)).json()["version"]
    release.set()
    await republish
    raw = await kv.store().rl_get(kv.RL_CONFIG_KEY)
    assert json.loads(raw)["version"] == v2
    assert (await config_sync.poll_once()).version == v2
    await kv.store()._client.delete(kv.RL_CONFIG_KEY)  # Redis restarted empty
    old = RateLimitConfig.model_validate(_policy(mode="enforce", global_concurrency=50))
    await rl_admin._publish(v1, old, actor="stale-writer")
    state = await config_sync.poll_once()
    assert (state.version, state.config.mode) == (v2, "shadow")
    await kv.store()._client.delete(kv.RL_CONFIG_KEY)


@needs_redis
async def test_a_cancelled_request_gives_its_redis_lease_back(clients, live_redis):
    from treg.bootstrap_ratelimit import RateLimitMiddleware
    doc = _policy(mode="enforce", global_concurrency=1)
    config_state._swap(config_state.ConfigState(RateLimitConfig.model_validate(doc), 9, "db", False))
    assert (await clients.get("/tools")).status_code == 200  # learn the key
    token = clients.headers["X-Treg-Token"]

    async def forever(scope, receive, send):
        await asyncio.Event().wait()

    mw = RateLimitMiddleware(forever)
    scope = {"type": "http", "method": "GET", "path": "/tools", "client": ("10.0.0.9", 1),
             "headers": [(b"x-treg-token", token.encode())]}

    async def call():
        await mw(scope, None, lambda m: asyncio.sleep(0))

    client = kv.store()._client
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if await client.zcard(kv.RL_GLOBAL_KEY) == 1:
                    break
            assert await client.zcard(kv.RL_GLOBAL_KEY) == 1
            tg.cancel_scope.cancel()
        assert await client.zcard(kv.RL_GLOBAL_KEY) == 0
    finally:
        await client.delete(kv.RL_GLOBAL_KEY)


@needs_postgres
def test_the_migrations_upgrade_and_downgrade_on_a_clean_postgres():
    """A fresh database: upgrade to head, step the newest revision down and up again."""
    import asyncpg
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy.engine import make_url
    from treg.maintenance import _alembic_config

    base = make_url(DB_URL)
    name = f"treg_migrate_{uuid.uuid4().hex[:10]}"
    admin_dsn = base.set(drivername="postgresql").render_as_string(hide_password=False)

    async def admin(sql: str) -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    try:
        config = _alembic_config()
        url = base.set(database=name).render_as_string(hide_password=False)
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        head = ScriptDirectory.from_config(config).get_current_head()
        command.upgrade(config, "head")
        command.downgrade(config, "-1")
        command.upgrade(config, "head")

        async def version() -> str:
            conn = await asyncpg.connect(base.set(drivername="postgresql", database=name)
                                         .render_as_string(hide_password=False))
            try:
                return await conn.fetchval("SELECT version_num FROM alembic_version")
            finally:
                await conn.close()

        assert asyncio.run(version()) == head
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
