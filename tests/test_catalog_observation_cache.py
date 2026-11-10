"""Process-cache semantics for optional Catalog reliability observations."""

from __future__ import annotations

import asyncio

from treg.infra.catalog_observations import (
    CachedEndpointObservationReader, EndpointObservationUnavailable,
)


class _Source:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.value = 1.0
        self.failure: Exception | None = None

    async def get_many(self, endpoint_ids):
        self.calls.append(tuple(endpoint_ids))
        self.started.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return {
            endpoint_id: {
                "samples": 5,
                "ok_rate": self.value,
                "p50_ms": 10,
                "p95_ms": 20,
                "last_ok_days": 0,
            }
            for endpoint_id in endpoint_ids
        }


async def test_one_hundred_cold_reads_share_one_refresh_task():
    source = _Source()
    source.release.clear()
    reader = CachedEndpointObservationReader(source)

    reads = await asyncio.gather(*(reader.get_many(["endpoint.a"]) for _ in range(100)))
    assert reads == [{}] * 100
    await asyncio.wait_for(source.started.wait(), timeout=1)
    assert len(source.calls) == 1
    assert reader.counts.miss == 100 and reader.counts.refresh == 1

    source.release.set()
    await reader.wait_for_idle()
    assert (await reader.get_many(["endpoint.a"]))["endpoint.a"]["ok_rate"] == 1.0
    assert reader.counts.fresh == 1
    await reader.aclose()


async def test_stale_value_returns_immediately_while_one_refresh_replaces_it():
    now = [0.0]
    source = _Source()
    reader = CachedEndpointObservationReader(source, clock=lambda: now[0])

    assert await reader.get_many(["endpoint.a"]) == {}
    await reader.wait_for_idle()
    now[0] = 301
    source.value = 0.8
    source.started.clear()
    source.release.clear()

    stale = await reader.get_many(["endpoint.a"])
    assert stale["endpoint.a"]["ok_rate"] == 1.0
    await asyncio.wait_for(source.started.wait(), timeout=1)
    assert reader.counts.stale == 1 and reader.counts.refresh == 2

    source.release.set()
    await reader.wait_for_idle()
    assert (await reader.get_many(["endpoint.a"]))["endpoint.a"]["ok_rate"] == 0.8
    await reader.aclose()


async def test_refresh_failure_keeps_stale_data_and_backs_off(caplog):
    now = [0.0]
    source = _Source()
    reader = CachedEndpointObservationReader(
        source, clock=lambda: now[0], fresh_ttl_s=5, stale_ttl_s=30, retry_s=5,
    )

    await reader.get_many(["endpoint.a"])
    await reader.wait_for_idle()
    now[0] = 6
    source.failure = RuntimeError("database unavailable")

    stale = await reader.get_many(["endpoint.a"])
    await reader.wait_for_idle()
    assert stale["endpoint.a"]["ok_rate"] == 1.0
    assert reader.counts.refresh_failure == 1

    # Still stale and still successful for the caller; the one-minute minimum prevents a query
    # storm against an already-failing database.
    again = await reader.get_many(["endpoint.a"])
    assert again["endpoint.a"]["ok_rate"] == 1.0
    assert reader.counts.refresh == 2

    now[0] = 12
    await reader.get_many(["endpoint.a"])
    await reader.wait_for_idle()
    assert reader.counts.refresh == 2, "even a shorter configured retry cannot bypass the minimum"

    now[0] = 31
    assert await reader.get_many(["endpoint.a"]) == {}, \
        "older than the 30-minute-equivalent stale TTL degrades to no reliability data"
    await reader.aclose()


async def test_partial_refresh_keeps_unavailable_entry_age_and_caches_completed_endpoints():
    now = [0.0]
    source = _Source()
    reader = CachedEndpointObservationReader(source, clock=lambda: now[0])
    await reader.get_many(["endpoint.async", "endpoint.sync"])
    await reader.wait_for_idle()

    now[0] = 301
    completed = {
        "endpoint.sync": {"samples": 20, "ok_rate": 0.8},
        "endpoint.quiet": {"samples": 0, "ok_rate": None},
    }
    source.failure = EndpointObservationUnavailable(completed)
    stale = await reader.get_many(["endpoint.async", "endpoint.sync", "endpoint.quiet"])
    assert stale["endpoint.async"]["ok_rate"] == 1.0
    await reader.wait_for_idle()
    got = await reader.get_many(["endpoint.async", "endpoint.sync", "endpoint.quiet"])
    assert got["endpoint.sync"] == completed["endpoint.sync"]
    assert got["endpoint.quiet"]["samples"] == 0
    assert got["endpoint.async"]["ok_rate"] == 1.0

    # Several successful partial refreshes must not keep the unavailable async result alive.
    for at in (602, 903, 1204, 1505):
        now[0] = at
        await reader.get_many(["endpoint.async", "endpoint.sync"])
        await reader.wait_for_idle()
    now[0] = 1801
    expired = await reader.get_many(["endpoint.async", "endpoint.sync"])
    assert "endpoint.async" not in expired
    assert expired["endpoint.sync"] == completed["endpoint.sync"]
    await reader.wait_for_idle()
    assert "endpoint.async" not in await reader.get_many(["endpoint.async"])

    # Once repaired, the missing endpoint can publish again without erasing complete peers.
    source.failure = None
    source.value = 0.6
    original = source.get_many

    async def repaired(endpoint_ids):
        values = await original(endpoint_ids)
        return {endpoint_id: completed.get(endpoint_id, value)
                for endpoint_id, value in values.items()}

    source.get_many = repaired
    now[0] = 1862
    await reader.get_many(["endpoint.async", "endpoint.sync"])
    await reader.wait_for_idle()
    recovered = await reader.get_many(["endpoint.async", "endpoint.sync"])
    assert recovered["endpoint.async"]["ok_rate"] == 0.6
    assert recovered["endpoint.sync"] == completed["endpoint.sync"]
    await reader.aclose()


async def test_cold_unavailable_fold_is_not_cached_as_an_empty_success():
    now = [0.0]
    source = _Source()
    source.failure = EndpointObservationUnavailable()
    reader = CachedEndpointObservationReader(source, clock=lambda: now[0])
    await reader.get_many(["endpoint.a"])
    await reader.wait_for_idle()
    assert await reader.get_many(["endpoint.a"]) == {}
    assert reader.counts.fresh == 0
    now[0] = 59
    await reader.get_many(["endpoint.a"])
    await reader.wait_for_idle()
    assert len(source.calls) == 1

    now[0] = 60
    source.failure = None
    await reader.get_many(["endpoint.a"])
    await reader.wait_for_idle()
    assert len(source.calls) == 2
    assert (await reader.get_many(["endpoint.a"]))["endpoint.a"]["samples"] == 5
    await reader.aclose()


async def test_shutdown_cancels_the_bootstrap_owned_refresh_task():
    source = _Source()
    source.release.clear()
    reader = CachedEndpointObservationReader(source)

    await reader.get_many(["endpoint.a"])
    await asyncio.wait_for(source.started.wait(), timeout=1)
    await reader.aclose()
    assert reader.counts.refresh == 1
    assert await reader.get_many(["endpoint.a"]) == {}


async def test_an_endpoint_nobody_called_is_read_once_and_pending_only_until_then():
    source = _Source()
    source.release.clear()
    real = source.get_many

    async def only_a(endpoint_ids):
        return {k: v for k, v in (await real(endpoint_ids)).items() if k == "endpoint.a"}
    source.get_many = only_a
    reader = CachedEndpointObservationReader(source)

    assert await reader.get_many(["endpoint.a", "endpoint.quiet"]) == {}
    assert reader.pending(["endpoint.quiet"])
    source.release.set()
    await reader.wait_for_idle()

    assert set(await reader.get_many(["endpoint.a", "endpoint.quiet"])) == {"endpoint.a"}
    assert not reader.pending(["endpoint.a", "endpoint.quiet"])
    assert len(source.calls) == 1 and reader.counts.fresh == 2
    await reader.aclose()
