"""Atomic slot protocol against explicitly configured disposable loopback Redis."""
import asyncio
import os
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from treg.infra import kv
from treg.infra import org_concurrency as guard
from treg.config import get_settings


@pytest.fixture
async def stores():
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    first, second = kv.RedisStore(url), kv.RedisStore(url)
    key = f"treg-test:org-slots:{uuid4().hex}"
    try:
        assert await first.ping()
        yield first, second, key
    finally:
        await first._client.delete(key, key + ":other")
        await asyncio.gather(first.aclose(), second.aclose())


async def test_atomic_n_slots_across_independent_clients_and_process(stores):
    first, second, key = stores
    results = await asyncio.gather(*(
        (first if n % 2 else second).acquire_slot(key, f"owner-{n}", 5000, 3)
        for n in range(20)
    ))
    assert results.count("acquired") == 3
    assert results.count("busy") == 17
    script = """
import asyncio, sys
from treg.infra.kv import RedisStore
async def main():
    store = RedisStore(sys.argv[1])
    try:
        print(await store.acquire_slot(sys.argv[2], 'other-process', 5000, 3))
    finally:
        await store.aclose()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, os.environ["TREG_TEST_KV_URL"], key,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0, stderr.decode()
    assert stdout.strip() == b"busy"
    assert await second.acquire_slot(key + ":other", "other-org", 5000, 1) == "acquired"


async def test_expired_owner_cannot_renew_or_release_replacement(stores):
    first, second, key = stores
    assert await first.acquire_slot(key, "old", 60, 1) == "acquired"
    await asyncio.sleep(0.1)
    assert await second.acquire_slot(key, "new", 1000, 1) == "acquired"
    assert await first.renew_slot(key, "old", 1000) == "lost"
    assert await first.release_slot(key, "old") == "not_owned"
    assert await first.acquire_slot(key, "third", 1000, 1) == "busy"
    assert await second.release_slot(key, "new") == "released"
    assert await first.acquire_slot(key, "third", 1000, 1) == "acquired"


async def test_renewal_outlives_original_lease_and_shorter_owner_does_not_shorten_key(stores):
    first, second, key = stores
    assert await first.acquire_slot(key, "long", 2000, 2) == "acquired"
    assert await second.acquire_slot(key, "short", 150, 2) == "acquired"
    assert await first._client.pttl(key) > 1500
    for _ in range(4):
        await asyncio.sleep(0.06)
        assert await second.renew_slot(key, "short", 150) == "renewed"
    assert await first.acquire_slot(key, "third", 1000, 2) == "busy"
    assert await second.release_slot(key, "short") == "released"
    assert await second.release_slot(key, "short") == "not_owned"
    assert await first.acquire_slot(key, "third", 1000, 2) == "acquired"


async def test_guard_heartbeat_retains_slot_past_original_lease(stores, monkeypatch):
    first, second, _ = stores
    org_id = uuid4().int
    settings = get_settings()
    monkeypatch.setattr(settings, "org_concurrency_enabled", True)
    monkeypatch.setattr(settings, "org_concurrency_limits", {org_id: 1})
    monkeypatch.setattr(settings, "org_concurrency_redis_failure", "fail_closed")
    monkeypatch.setattr(settings, "org_concurrency_lease_s", 0.6)
    monkeypatch.setattr(kv, "store", lambda: first)
    slot = await guard.acquire(org_id)
    try:
        await asyncio.sleep(1.3)
        assert await second.acquire_slot(slot.key, "other-worker", 2000, 1) == "busy"
        assert await first._client.zscore(slot.key, slot.token) is not None
        assert slot.renewal is not None and not slot.renewal.done()
        await slot.finish()
        assert await second.acquire_slot(slot.key, "other-worker", 2000, 1) == "acquired"
    finally:
        await slot.finish()
        await first._client.delete(slot.key)


async def test_redis_outage_and_local_store_are_distinct_from_contention():
    store = kv.RedisStore("redis://127.0.0.1:1")
    try:
        for backend in (store, kv.LocalStore()):
            assert await backend.acquire_slot("key", "owner", 1000, 1) == "unavailable"
            assert await backend.renew_slot("key", "owner", 1000) == "unavailable"
            assert await backend.release_slot("key", "owner") == "unavailable"
    finally:
        await store.aclose()
