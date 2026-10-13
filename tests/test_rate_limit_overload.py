"""The per-process overload cap: an in-process semaphore in the outermost middleware."""

from __future__ import annotations

import asyncio
import json

import pytest

from treg.application.ratelimit import limiter, metrics
from treg.bootstrap_ratelimit import RateLimitMiddleware
from treg.config import get_settings


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    monkeypatch.setenv("TREG_OVERLOAD_CAP_ENABLED", "true")
    monkeypatch.setenv("TREG_OVERLOAD_CAP_SIZE", "2")
    monkeypatch.setenv("TREG_OVERLOAD_CAP_RETRY_AFTER_S", "3")
    get_settings.cache_clear()
    limiter.reset_for_tests()
    yield
    limiter.reset_for_tests()
    get_settings.cache_clear()


def _scope(path: str = "/call/x") -> dict:
    return {"type": "http", "path": path, "headers": [], "client": ("127.0.0.1", 1)}


class Gate:
    """An app that answers only when released, streaming two chunks."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = 0

    async def __call__(self, scope, receive, send):
        self.entered += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await self.release.wait()
        await send({"type": "http.response.body", "body": b"b"})


async def _call(mw, path="/call/x"):
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    await mw(_scope(path), receive, send)
    return sent


def _status(sent) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def test_off_by_default(monkeypatch):
    monkeypatch.delenv("TREG_OVERLOAD_CAP_ENABLED")
    get_settings.cache_clear()
    assert get_settings().overload_cap_enabled is False


async def test_a_full_cap_refuses_at_once_with_503_overload_cap():
    gate = Gate()
    mw = RateLimitMiddleware(gate)
    held = [asyncio.create_task(_call(mw)) for _ in range(2)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    metrics.drain()
    sent = await asyncio.wait_for(_call(mw), timeout=1)  # never waits for a permit
    assert _status(sent) == 503
    headers = dict(sent[0]["headers"])
    assert headers[b"retry-after"] == b"3" and headers[b"x-treg-overload-reason"] == b"overload_cap"
    assert json.loads(sent[1]["body"])["detail"]["reason"] == "overload_cap"
    assert gate.entered == 2
    _, gauge = metrics.drain()
    assert gauge["overload_rejects"] == 1
    gate.release.set()
    await asyncio.gather(*held)
    assert _status(await _call(mw)) == 200


async def test_a_stream_holds_its_permit_until_the_last_chunk():
    gate = Gate()
    mw = RateLimitMiddleware(gate)
    streams = [asyncio.create_task(_call(mw)) for _ in range(2)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Both handlers have started their responses, but the streams are still open.
    assert mw._overload.locked()
    gate.release.set()
    await asyncio.gather(*streams)
    assert not mw._overload.locked() and mw._overload._value == 2


async def test_a_client_disconnect_mid_stream_releases_the_permit():
    async def disconnected(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise OSError("client went away")  # what a server raises when the socket is gone

    mw = RateLimitMiddleware(disconnected)
    for _ in range(4):
        with pytest.raises(OSError):
            await _call(mw)
    assert mw._overload._value == 2


async def test_no_permit_leaks_on_exceptions():
    async def boom(scope, receive, send):
        raise RuntimeError("handler failed")

    mw = RateLimitMiddleware(boom)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            await _call(mw)
    assert mw._overload._value == 2


async def test_no_permit_leaks_on_cancellation():
    gate = Gate()
    mw = RateLimitMiddleware(gate)
    tasks = [asyncio.create_task(_call(mw)) for _ in range(2)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert mw._overload.locked()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert mw._overload._value == 2


@pytest.mark.parametrize("path", ["/health", "/admin/blocklist", "/admin/rate-limit/status", "/admin"])
async def test_health_and_admin_are_never_counted(path):
    gate = Gate()
    mw = RateLimitMiddleware(gate)
    held = [asyncio.create_task(_call(mw)) for _ in range(2)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    async def ok(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw.app = ok
    assert _status(await _call(mw, path)) == 200
    mw.app = gate
    gate.release.set()
    await asyncio.gather(*held)


# ---- the blocklist cache: nothing matches by accident -------------------------------------------
def _list(*entries):
    from treg.domain.ratelimit import BlockEntry, Blocklist
    return Blocklist.build(1, "2026-10-12T00:00:00Z", tuple(
        BlockEntry(k, v, "r", 4_000_000_000, "o") for k, v in entries))


def test_a_null_org_id_never_matches():
    bl = _list(("org_id", "7"))
    for value in (None, "", "None", "null", "0", "7 ", "07"):
        assert bl.match("org_id", value, now=0) is None
    rl = limiter.limiter()
    assert rl._match("org_id", None) is None and rl._match("org_id", "") is None


def test_a_hash_mismatch_never_matches():
    key = "ab" * 32
    bl = _list(("key_hash", key))
    assert bl.match("key_hash", key, now=0) is not None
    for value in (key.upper(), key[:-1] + "0", key[:63], "", None):
        assert bl.match("key_hash", value, now=0) is None
    assert bl.match("org_id", key, now=0) is None  # a hash is never compared as an org id


# ---- per-process telemetry for sizing the cap -----------------------------------------------------
from treg.application.ratelimit import background, inflight  # noqa: E402


async def test_in_flight_is_counted_with_the_cap_off_and_never_leaks(monkeypatch):
    monkeypatch.setenv("TREG_OVERLOAD_CAP_ENABLED", "false")
    get_settings.cache_clear()
    inflight.reset_for_tests()
    gate = Gate()
    mw = RateLimitMiddleware(gate)
    held = [asyncio.create_task(_call(mw)) for _ in range(3)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert inflight.current() == 3
    held[0].cancel()
    gate.release.set()
    await asyncio.gather(*held, return_exceptions=True)

    async def boom(scope, receive, send):
        raise RuntimeError("x")

    mw.app = boom
    with pytest.raises(RuntimeError):
        await _call(mw)
    assert inflight.current() == 0
    assert inflight.take_peak() == 3 and inflight.take_peak() == 0


def test_the_gauge_emits_in_flight_and_db_pool_checkouts_tagged_with_the_pid(monkeypatch, caplog):
    import logging
    import os
    from treg.infra import db as infra_db
    inflight.reset_for_tests()
    inflight.enter(); inflight.enter(); inflight.leave()
    monkeypatch.setattr(background, "pool_snapshot", lambda: {"api": {"checked_out": 4, "capacity": 15}})
    monkeypatch.setattr(infra_db, "_checkout_peaks", {"api": 9})  # what the checkout listener saw
    events: list = []
    monkeypatch.setattr(background.analytics, "capture", lambda _id, e, p=None, **_: events.append((e, p)))
    gauge = background.InflightGauge()
    caplog.set_level(logging.INFO, logger="treg.ratelimit")
    row = gauge.emit()
    assert (row["pid"], row["in_flight"], row["in_flight_peak"]) == (os.getpid(), 1, 2)
    assert (row["db_api_checked_out"], row["db_api_peak"], row["db_api_capacity"]) == (4, 9, 15)
    assert (row["overload_cap_enabled"], row["overload_cap_size"]) == (True, 2)  # this file's fixture
    assert events[0][0] == "inflight_gauge" and events[0][1]["pid"] == os.getpid()
    assert any(r.getMessage().startswith("inflight_gauge {") for r in caplog.records)
    assert gauge.emit()["db_api_peak"] == 4  # reset at each emit, to what is still checked out
    inflight.leave()


async def test_a_burst_shorter_than_any_interval_still_shows_in_the_peak(monkeypatch):
    """The peak is updated on every request, not sampled: five requests that come and go between
    two emits still report 5, and the next window starts from what is in flight then."""
    monkeypatch.setenv("TREG_OVERLOAD_CAP_ENABLED", "false")  # counted whether the cap is on or not
    get_settings.cache_clear()
    monkeypatch.setattr(background, "pool_snapshot", lambda: {})
    monkeypatch.setattr(background.analytics, "capture", lambda *a, **k: None)
    inflight.reset_for_tests()
    gauge = background.InflightGauge()
    gate = Gate()
    mw = RateLimitMiddleware(gate)
    burst = [asyncio.create_task(_call(mw, "/call/x")) for _ in range(5)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.release.set()
    await asyncio.gather(*burst)  # all done before any emit
    assert inflight.current() == 0
    row = gauge.emit()
    assert (row["in_flight"], row["in_flight_peak"]) == (0, 5)
    assert gauge.emit()["in_flight_peak"] == 0


def test_the_db_checkout_peak_is_the_true_maximum_and_restarts_each_window(monkeypatch):
    from treg.infra import db as infra_db
    monkeypatch.setattr(infra_db, "_checkout_peaks", {})
    infra_db._checkout_peaks["api"] = 12  # a burst of checkouts between emits
    assert infra_db.take_checkout_peaks({"api": {"checked_out": 3, "capacity": 15}}) == {"api": 12}
    assert infra_db.take_checkout_peaks({"api": {"checked_out": 3, "capacity": 15}}) == {"api": 3}


async def test_a_broken_gauge_never_stops_or_fails_anything(monkeypatch):
    def broken():
        raise RuntimeError("pool introspection failed")

    monkeypatch.setattr(background, "pool_snapshot", broken)
    monkeypatch.setattr(background, "install_checkout_peaks", lambda: None)
    sleeps: list = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(background.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await background.inflight_loop()
    assert sleeps == [10.0, 10.0, 10.0]  # kept emitting through every failure

    async def ok(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    assert _status(await _call(RateLimitMiddleware(ok))) == 200
