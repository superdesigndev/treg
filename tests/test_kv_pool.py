"""The shared store's connection pool: bounded, waits briefly, degrades, closes idle connections."""
import asyncio
import inspect
import os
import time
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest

from treg.config import get_settings
from treg.infra import kv

_ARGS = {
    "str": "tregtest", "int": 1, "float": 1.0, "bool": False, "int | None": None,
    "str | None": None, "float | None": None, "list[str]": ["tregtest"], "list[int]": [1],
    "list[tuple[str, int]]": [("tregtest", 1)],
}


def _full_store(monkeypatch, *, wait_s=0.01):
    settings = get_settings()
    monkeypatch.setattr(settings, "kv_max_connections", 1)
    monkeypatch.setattr(settings, "kv_pool_wait_s", wait_s)
    store = kv.RedisStore("redis://127.0.0.1:1/0")
    busy = MagicMock()
    busy.disconnect = AsyncMock()
    store._client.connection_pool._in_use_connections.add(busy)
    return store


def _public_operations():
    for name, method in inspect.getmembers(kv.RedisStore, inspect.iscoroutinefunction):
        if name.startswith("_") or name == "aclose":
            continue
        params = list(inspect.signature(method).parameters.values())[1:]
        if any(p.default is inspect.Parameter.empty and p.annotation not in _ARGS for p in params):
            raise AssertionError(f"add an argument for {name}'s {params}")
        args = [_ARGS[p.annotation] for p in params
                if p.kind is p.POSITIONAL_OR_KEYWORD and p.default is inspect.Parameter.empty]
        kwargs = {p.name: _ARGS[p.annotation] for p in params
                  if p.kind is p.KEYWORD_ONLY and p.default is inspect.Parameter.empty}
        yield name, args, kwargs


def test_settings_bound_the_pool_per_process(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "kv_max_connections", 7)
    monkeypatch.setattr(settings, "kv_pool_wait_s", 0.3)
    pool = kv.RedisStore("redis://127.0.0.1:1/0")._client.connection_pool
    assert pool.max_connections == 7 and pool.timeout == 0.3
    assert pool.connection_kwargs["socket_timeout"] == kv._TIMEOUT_S


@pytest.mark.parametrize("name,args,kwargs", list(_public_operations()),
                         ids=[name for name, _, _ in _public_operations()])
async def test_a_full_pool_reads_as_an_unavailable_store_on_every_operation(
    monkeypatch, name, args, kwargs,
):
    monkeypatch.setattr(kv, "_note_fault", lambda exc: None)
    store = _full_store(monkeypatch)
    try:
        await asyncio.wait_for(getattr(store, name)(*args, **kwargs), 5)
    finally:
        await store.aclose()


async def test_a_full_pool_waits_no_longer_than_its_wait(monkeypatch):
    faults = []
    monkeypatch.setattr(kv, "_note_fault", faults.append)
    store = _full_store(monkeypatch, wait_s=0.05)
    started = time.monotonic()
    try:
        assert await store.acquire_slot("treg-test:slot", "t", 1000, 1) == "unavailable"
    finally:
        await store.aclose()
    assert 0.04 <= time.monotonic() - started < 0.5


async def test_connections_idle_past_the_limit_are_closed_on_the_next_release(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(kv, "_clock", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "kv_idle_close_s", 60.0)
    pool = kv.RedisStore("redis://127.0.0.1:1/0")._client.connection_pool
    old, recent, active = (MagicMock(disconnect=AsyncMock(), re_auth=AsyncMock(),
                                     should_reconnect=lambda: False)
                           for _ in range(3))
    pool._in_use_connections.update((old, recent, active))
    await pool.release(old)
    clock[0] += 50
    await pool.release(recent)
    clock[0] += 11
    await pool.release(active)
    assert pool._available_connections == [recent, active]
    old.disconnect.assert_awaited_once_with(nowait=True)
    recent.disconnect.assert_not_awaited()
    clock[0] += 61
    assert await pool.close_idle() == 2
    assert pool._available_connections == [] and not pool._released_at


async def test_a_peak_leaves_no_idle_connections_behind_against_redis(monkeypatch):
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    clock = [1000.0]
    monkeypatch.setattr(kv, "_clock", lambda: clock[0])
    settings = get_settings()
    monkeypatch.setattr(settings, "kv_max_connections", 4)
    monkeypatch.setattr(settings, "kv_idle_close_s", 60.0)
    store = kv.RedisStore(url)
    pool = store._client.connection_pool
    try:
        assert all(await asyncio.gather(*(store.ping() for _ in range(20))))
        assert len(pool._available_connections) == 4
        clock[0] += 61
        assert await store.ping()
        assert len(pool._available_connections) == 1, "only the connection just used stays open"
    finally:
        await store.aclose()
