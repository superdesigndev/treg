"""Observe mode for a shared rate window on treg's own keys: every platform call that goes upstream
is relayed unchanged, and the window's verdict is counted against the provider's answer."""
from __future__ import annotations

import os
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from httpx import AsyncClient

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.capacity import rate_observe
from treg.infra import kv, provider_rate
from treg.infra.upstream.limiter import limiter

from test_capacity_smoothing import _publish_rate, _relay_script
from test_marketplace_call import EP, _balance, platform_on  # noqa: F401


@pytest.fixture
def emitted(monkeypatch):
    lines: list[dict] = []
    monkeypatch.setattr(rate_observe.events, "emit",
                        lambda event, **f: event == "rate_observe" and lines.append({"event": event, **f}))
    monkeypatch.setattr(rate_observe, "FLUSH_S", 0.0)
    rate_observe.reset()
    limiter.reset()
    yield lines
    rate_observe.reset()
    limiter.reset()


@pytest.fixture
def observing(monkeypatch, platform_on, emitted):
    monkeypatch.setenv("TREG_PROVIDER_RATE_MODE", "observe")
    get_settings.cache_clear()
    verdicts: list[str | None] = []
    asked: list[tuple] = []

    async def admit(bucket, limit, window_s):
        asked.append((bucket, limit, window_s))
        return verdicts.pop(0) if verdicts else "admit"
    monkeypatch.setattr(provider_rate, "admit", admit)
    yield verdicts, asked
    get_settings.cache_clear()


def _counts(line: dict) -> dict:
    return {k: v for k, v in line.items() if k.endswith(("_2xx", "_429", "_other", "_none"))}


# ---- the call path ------------------------------------------------------------------------------

async def test_a_call_the_window_would_refuse_is_relayed_and_charged_as_before(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    verdicts, asked = observing
    await _publish_rate("tikhub", 20, 60.0)
    verdicts.append("over")
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script([(200, (), b'{"ok":true}')], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and len(seen) == 1, r.text
    assert await _balance(clients) < before, "observe mode refuses nothing and charges as before"
    assert asked == [("tikhub", 20, 60.0)]
    assert [(e["event"], e["provider"], e["bucket"], e["limit"], e["window_s"]) for e in emitted] == [
        ("rate_observe", "tikhub", "tikhub", 20, 60.0)]
    assert _counts(emitted[0]) == {"over_2xx": 1}


async def test_each_verdict_is_counted_against_what_the_provider_answered(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    verdicts, _ = observing
    await _publish_rate("tikhub", 20, 60.0)
    monkeypatch.setattr(rate_observe, "FLUSH_S", 3600.0)
    verdicts.extend(["admit", "over", None])
    monkeypatch.setattr(call_service, "relay", _relay_script(
        [(429, (), b'{"detail":"slow"}'), (429, (), b'{"detail":"slow"}'), (500, (), b"no")], []))
    for i in range(3):
        await clients.get(f"/call/{EP}?aweme_id={i}")
    assert emitted == []
    assert dict(rate_observe._tallies["tikhub"].counts) == {
        "admit_429": 1, "over_429": 1, "unknown_other": 1}


async def test_a_relay_that_raises_is_counted_as_no_answer(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    await _publish_rate("tikhub", 20, 60.0)

    async def broken(*a, **kw):
        raise httpx.ConnectError("connection reset")
    monkeypatch.setattr(call_service, "relay", broken)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code >= 500
    assert _counts(emitted[0]) == {"admit_none": 1}


async def test_own_key_calls_are_never_observed(clients: AsyncClient, observing, emitted, monkeypatch):
    _, asked = observing
    await _publish_rate("tikhub", 20, 60.0)
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier 2
    monkeypatch.setattr(call_service, "relay", _relay_script([(200, (), b'{"ok":true}')], []))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert asked == [] and emitted == []


async def test_off_by_default_asks_nothing(clients: AsyncClient, platform_on, emitted, monkeypatch):
    await _publish_rate("tikhub", 20, 60.0)
    asked = []

    async def admit(*a):
        asked.append(a)
        return "admit"
    monkeypatch.setattr(provider_rate, "admit", admit)
    monkeypatch.setattr(call_service, "relay", _relay_script([(200, (), b'{"ok":true}')], []))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert asked == [] and emitted == []


# ---- the tally ----------------------------------------------------------------------------------

def _seen(verdict="admit", limit=20, window_s=60.0):
    return rate_observe.Seen("contactout", "contactout.people.email.verify", limit, window_s, verdict)


def test_a_bucket_emits_once_its_minute_is_up(emitted, monkeypatch):
    monkeypatch.setattr(rate_observe, "FLUSH_S", 3600.0)
    for verdict, status in (("admit", 200), ("over", 200), ("over", 429), ("unknown", None)):
        rate_observe.record(_seen(verdict), status)
    assert emitted == []
    monkeypatch.setattr(rate_observe, "FLUSH_S", 0.0)
    rate_observe.record(_seen("admit"), 404)
    assert _counts(emitted[0]) == {"admit_2xx": 1, "over_2xx": 1, "over_429": 1, "unknown_none": 1,
                                   "admit_other": 1}
    assert "contactout.people.email.verify" not in rate_observe._tallies


def test_a_changed_limit_emits_the_old_counts_under_the_old_limit(emitted, monkeypatch):
    monkeypatch.setattr(rate_observe, "FLUSH_S", 3600.0)
    rate_observe.record(_seen(limit=20), 200)
    rate_observe.record(_seen(limit=40), 200)
    assert [(e["limit"], _counts(e)) for e in emitted] == [(20, {"admit_2xx": 1})]
    assert rate_observe._tallies["contactout.people.email.verify"].limit == 40


async def test_no_shared_store_gives_no_verdict(monkeypatch):
    monkeypatch.setattr(kv, "configured", lambda: False)
    assert await provider_rate.admit("p", 2, 1.0) is None


async def test_a_failing_store_gives_no_verdict(monkeypatch):
    class Down:
        async def acquire_slot(self, *a):
            raise ConnectionError("down")
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "store", lambda: Down())
    assert await provider_rate.admit("p", 2, 1.0) is None


# ---- the shared window, against disposable loopback Redis ----------------------------------------

@pytest.fixture
async def redis_store(monkeypatch):
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    monkeypatch.setenv("TREG_KV_URL", url)
    get_settings.cache_clear()
    await kv.close()
    yield
    await kv.close()
    get_settings.cache_clear()


async def test_the_window_admits_its_limit_then_refuses_until_the_oldest_expires(redis_store):
    import asyncio

    bucket = f"treg-test-{uuid4().hex}"
    assert [await provider_rate.admit(bucket, 2, 0.5) for _ in range(3)] == ["admit", "admit", "over"]
    await asyncio.sleep(0.6)
    assert await provider_rate.admit(bucket, 2, 0.5) == "admit"
