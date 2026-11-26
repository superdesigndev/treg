"""A lease asked for `unless` a claim, against explicitly configured disposable loopback Redis."""
import os
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from treg.infra import kv


@pytest.fixture
async def store():
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    redis_store = kv.RedisStore(url)
    try:
        assert await redis_store.ping()
        yield redis_store
    finally:
        await redis_store.aclose()


async def test_a_claimed_lease_goes_only_to_the_claims_owner(store):
    key, claim = f"treg-test:lease:{uuid4().hex}", f"treg-test:claim:{uuid4().hex}"
    try:
        assert await store.acquire_lease(key, "anyone", 5000, unless=claim) == "acquired"
        assert await store.release_lease(key, "anyone") == "released"
        assert await store.acquire_lease(claim, "fold", 5000) == "acquired"
        assert await store.acquire_lease(key, "request", 5000, unless=claim) == "busy"
        assert await store._client.exists(key) == 0, "a claimed lease is left free"
        assert await store.acquire_lease(key, "fold", 5000, unless=claim) == "acquired"
        assert await store.acquire_lease(key, "request", 5000, unless=claim) == "busy"
        assert await store.release_lease(key, "fold") == "released"
        assert await store.release_lease(claim, "fold") == "released"
        assert await store.acquire_lease(key, "request", 5000, unless=claim) == "acquired"
        assert await store.acquire_lease(key, "fold", 5000) == "busy", "still a lease"
    finally:
        await store._client.delete(key, claim)
