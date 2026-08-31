"""Process-cache semantics for optional Catalog reliability observations."""

from __future__ import annotations

import asyncio

from treg.infra.catalog_observations import CachedEndpointObservationReader


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

    # Still stale and still successful for the caller; the five-second backoff prevents a query
    # storm against an already-failing database.
    again = await reader.get_many(["endpoint.a"])
    assert again["endpoint.a"]["ok_rate"] == 1.0
    assert reader.counts.refresh == 2

    now[0] = 31
    assert await reader.get_many(["endpoint.a"]) == {}, \
        "older than the 30-minute-equivalent stale TTL degrades to no reliability data"
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
