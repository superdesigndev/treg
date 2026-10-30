"""A shared rate window on treg's own keys: every platform call that goes upstream is counted
against the provider's answer, and a full window refuses a call only on an enforced bucket."""
from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from treg.application.call import service as call_service
from treg.config import Settings, get_settings
from treg.domain.capacity import rate_observe
from treg.infra import kv, provider_rate
from treg.infra.upstream.limiter import limiter
from treg.timeutil import utcnow_naive

from test_capacity_smoothing import _publish_rate, _relay_script
from test_marketplace_call import EP, _balance, platform_on  # noqa: F401
from test_org_endpoint_quota_call import _assert_no_money


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    yield
    get_settings.cache_clear()


@pytest.fixture
def emitted(monkeypatch):
    lines: list[dict] = []
    monkeypatch.setattr(rate_observe.events, "emit",
                        lambda event, **f: event == "rate_observe" and lines.append({"event": event, **f}))
    rate_observe.reset()
    limiter.reset()
    yield lines
    rate_observe.reset()
    limiter.reset()


@pytest.fixture
def observing(monkeypatch, platform_on, emitted):
    """Observe mode with a scripted window: each verdict is `admit`, `over` or None (no store)."""
    monkeypatch.setenv("TREG_PROVIDER_RATE_MODE", "observe")
    monkeypatch.setenv("TREG_KV_URL", "redis://127.0.0.1:6399/0")
    get_settings.cache_clear()
    monkeypatch.setattr(kv, "store", lambda: kv.LocalStore())
    verdicts: list[str | None] = []
    asked: list[tuple] = []
    released: list[provider_rate.Admission] = []

    async def admit(bucket, limit, window_s):
        asked.append((bucket, limit, window_s))
        verdict = verdicts.pop(0) if verdicts else "admit"
        return None if verdict is None else provider_rate.Admission(
            verdict, f"k:{bucket}", uuid4().hex, 7 if verdict == "over" else 0)
    monkeypatch.setattr(provider_rate, "admit", admit)
    monkeypatch.setattr(provider_rate, "release", released.append)
    yield verdicts, asked, released
    get_settings.cache_clear()


def _enforce(monkeypatch, buckets: str) -> None:
    monkeypatch.setenv("TREG_PROVIDER_RATE_ENFORCE", buckets)
    get_settings.cache_clear()


def _counts(line: dict) -> dict:
    return {k: v for k, v in line.items() if k.endswith(("_2xx", "_429", "_other", "_none", "_refused"))}


def _flushed(emitted: list[dict]) -> list[dict]:
    rate_observe.flush()
    return emitted


# ---- observing ----------------------------------------------------------------------------------

async def test_a_call_the_window_would_refuse_is_relayed_and_charged_as_before(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    verdicts, asked, _ = observing
    await _publish_rate("tikhub", 20, 60.0)
    verdicts.append("over")
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_script([(200, (), b'{"ok":true}')], seen))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and len(seen) == 1, r.text
    assert await _balance(clients) < before, "an unenforced bucket refuses nothing and charges as before"
    assert asked == [("tikhub", 20, 60.0)]
    lines = _flushed(emitted)
    assert [(e["provider"], e["bucket"], e["limit"], e["window_s"], e["enforced"]) for e in lines] == [
        ("tikhub", "tikhub", 20, 60.0, False)]
    assert _counts(lines[0]) == {"over_2xx": 1}


async def test_each_verdict_is_counted_against_what_the_provider_answered(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    verdicts, _, _ = observing
    await _publish_rate("tikhub", 20, 60.0)
    verdicts.extend(["admit", "over", None])
    monkeypatch.setattr(call_service, "relay", _relay_script(
        [(429, (), b'{"detail":"slow"}'), (429, (), b'{"detail":"slow"}'), (500, (), b"no")], []))
    for i in range(3):
        await clients.get(f"/call/{EP}?aweme_id={i}")
    assert emitted == [], "nothing is emitted before its minute is up"
    assert _counts(_flushed(emitted)[0]) == {"admit_429": 1, "over_429": 1, "unknown_other": 1}


async def test_a_relay_that_raises_is_counted_as_no_answer(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    await _publish_rate("tikhub", 20, 60.0)

    async def broken(*a, **kw):
        raise httpx.ConnectError("connection reset")
    monkeypatch.setattr(call_service, "relay", broken)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code >= 500
    assert _counts(_flushed(emitted)[0]) == {"admit_none": 1}


async def test_own_key_calls_are_never_observed(clients: AsyncClient, observing, emitted, monkeypatch):
    _, asked, _ = observing
    _enforce(monkeypatch, "tikhub")
    await _publish_rate("tikhub", 20, 60.0)
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier 2
    monkeypatch.setattr(call_service, "relay", _relay_script([(200, (), b'{"ok":true}')], []))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert asked == [] and _flushed(emitted) == []


async def test_an_archive_hit_is_never_observed(clients: AsyncClient, observing, emitted, monkeypatch):
    _, asked, _ = observing
    _enforce(monkeypatch, "tikhub")
    await _publish_rate("tikhub", 20, 60.0)
    monkeypatch.setattr(call_service.archive, "serving", lambda: True)

    async def lookup(**kw):
        return {"body": b'{"ok":"stored"}', "key_hash": "k", "content_hash": "c",
                "fetched_at": utcnow_naive(), "media_type": "application/json", "age_s": 5,
                "status_code": 200}
    monkeypatch.setattr(call_service.archive, "lookup", lookup)
    sent = []
    monkeypatch.setattr(call_service, "relay", _relay_script([], sent))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and r.headers.get("x-treg-cache") == "hit", r.text
    assert sent == [] and asked == [] and _flushed(emitted) == []


async def test_off_by_default_asks_nothing(clients: AsyncClient, platform_on, emitted, monkeypatch):
    await _publish_rate("tikhub", 20, 60.0)
    asked = []

    async def admit(*a):
        asked.append(a)
    monkeypatch.setattr(provider_rate, "admit", admit)
    monkeypatch.setattr(call_service, "relay", _relay_script([(200, (), b'{"ok":true}')], []))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert asked == [] and _flushed(emitted) == []


# ---- enforcing ----------------------------------------------------------------------------------

async def test_a_full_enforced_bucket_refuses_before_sending_and_charges_nothing(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    verdicts, _, _ = observing
    _enforce(monkeypatch, "tikhub")
    await _publish_rate("tikhub", 20, 60.0)
    verdicts.append("over")
    sent = []
    monkeypatch.setattr(call_service, "relay", _relay_script([], sent))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429, r.text
    assert r.json()["detail"]["error"] == "provider_rate_limited"
    assert r.json()["detail"]["retry_after_s"] == 7 and r.headers["retry-after"] == "7"
    assert r.headers.get("x-treg-error") == "1"
    assert sent == [] and await _balance(clients) == before
    await _assert_no_money()
    assert _counts(_flushed(emitted)[0]) == {"over_refused": 1}


async def test_an_enforced_bucket_with_room_or_no_store_relays(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    verdicts, _, _ = observing
    _enforce(monkeypatch, "tikhub")
    await _publish_rate("tikhub", 20, 60.0)
    verdicts.extend(["admit", None])
    sent = []
    monkeypatch.setattr(call_service, "relay", _relay_script(
        [(200, (), b'{"ok":true}'), (200, (), b'{"ok":true}')], sent))
    for i in range(2):
        assert (await clients.get(f"/call/{EP}?aweme_id={i}")).status_code == 200
    assert len(sent) == 2, "an unknown window lets the call through"
    assert _counts(_flushed(emitted)[0]) == {"admit_2xx": 1, "unknown_2xx": 1}


async def test_the_retry_re_send_takes_its_own_room(
    clients: AsyncClient, observing, emitted, monkeypatch,
):
    verdicts, asked, _ = observing
    await _publish_rate("tikhub", 20, 60.0)
    burst = (429, ((b"retry-after", b"0"),), b'{"detail":"slow"}')
    verdicts.extend(["admit", "over"])
    sent = []
    monkeypatch.setattr(call_service, "relay", _relay_script([burst, (200, (), b'{"ok":true}')], sent))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert len(sent) == 2 and len(asked) == 2
    assert _counts(_flushed(emitted)[0]) == {"admit_429": 1, "over_2xx": 1}

    _enforce(monkeypatch, "tikhub")
    verdicts.extend(["admit", "over"])
    sent.clear()
    monkeypatch.setattr(call_service, "relay", _relay_script([burst], sent))
    r = await clients.get(f"/call/{EP}?aweme_id=8")
    assert r.status_code == 429 and len(sent) == 1, "a full window keeps the provider's first answer"
    assert "provider_rate_limited" not in r.text


def test_an_enforced_name_covers_every_account_of_the_bucket(monkeypatch):
    monkeypatch.setenv("TREG_PROVIDER_RATE_MODE", "observe")
    monkeypatch.setenv("TREG_KV_URL", "redis://127.0.0.1:6399/0")
    _enforce(monkeypatch, " contactout.people.email.verify , tinyfish")
    assert rate_observe._enforced("contactout.people.email.verify")
    assert rate_observe._enforced("contactout.people.email.verify#b")
    assert rate_observe._enforced("tinyfish") and not rate_observe._enforced("tinyfish.web.fetch")
    _enforce(monkeypatch, "")
    assert not rate_observe._enforced("tinyfish")


def test_settings_refuse_an_incomplete_configuration():
    with pytest.raises(ValidationError, match="requires a valid Redis kv_url"):
        Settings(provider_rate_mode="observe", kv_url="")
    with pytest.raises(ValidationError, match="needs TREG_PROVIDER_RATE_MODE=observe"):
        Settings(provider_rate_enforce="tinyfish", kv_url="redis://h:6379/0")
    assert Settings(provider_rate_mode="observe", provider_rate_enforce="tinyfish",
                    kv_url="redis://h:6379/0").provider_rate_enforce == "tinyfish"


# ---- the tally ----------------------------------------------------------------------------------

def _seen(verdict="admit", limit=20, window_s=60.0):
    return rate_observe.Seen("contactout", "contactout.people.email.verify", limit, window_s, verdict)


def test_a_bucket_emits_once_its_minute_is_up(emitted, monkeypatch):
    for verdict, status in (("admit", 200), ("over", 200), ("over", 429), ("unknown", None),
                            ("over", "refused"), ("admit", 404)):
        rate_observe.record(_seen(verdict), status)
    rate_observe.flush(due_only=True)
    assert emitted == []
    monkeypatch.setattr(rate_observe, "FLUSH_S", 0.0)
    rate_observe.flush(due_only=True)
    assert _counts(emitted[0]) == {"admit_2xx": 1, "over_2xx": 1, "over_429": 1, "unknown_none": 1,
                                   "over_refused": 1, "admit_other": 1}
    assert rate_observe._tallies == {}


def test_each_declared_rate_is_its_own_tally(emitted):
    rate_observe.record(_seen(limit=20), 200)
    rate_observe.record(_seen(limit=40), 200)
    rate_observe.record(_seen(limit=20), 200)
    assert sorted((e["limit"], _counts(e)) for e in _flushed(emitted)) == [
        (20, {"admit_2xx": 2}), (40, {"admit_2xx": 1})]


async def test_the_flush_worker_emits_without_waiting_for_a_next_call(emitted, monkeypatch):
    monkeypatch.setattr(rate_observe, "FLUSH_S", 0.04)
    rate_observe.record(_seen(), 200)
    worker = asyncio.create_task(rate_observe.flush_worker())
    try:
        await asyncio.sleep(0.2)
    finally:
        worker.cancel()
    assert [_counts(e) for e in emitted] == [{"admit_2xx": 1}]


# ---- the store ----------------------------------------------------------------------------------

@pytest.fixture
def fresh_breaker():
    provider_rate.reset()
    yield
    provider_rate.reset()


async def test_no_shared_store_gives_no_verdict(monkeypatch, fresh_breaker):
    monkeypatch.setattr(kv, "configured", lambda: False)
    assert await provider_rate.admit("p", 2, 1.0) is None


async def test_a_failing_store_is_skipped_for_a_while_then_probed_once(monkeypatch, fresh_breaker):
    asked = []

    class Store:
        down = True

        async def rate_window(self, *a):
            asked.append(a)
            await asyncio.sleep(0.01)
            return kv.RateWindowResult("unavailable" if self.down else "admit")
    store = Store()
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "store", lambda: store)
    assert await provider_rate.admit("p", 2, 1.0) is None
    assert await provider_rate.admit("p", 2, 1.0) is None and len(asked) == 1, "skipped while cooling"
    monkeypatch.setattr(provider_rate, "_down_until", time.monotonic() - 1)
    store.down = False
    probe, during = await asyncio.gather(provider_rate.admit("p", 2, 1.0), provider_rate.admit("p", 2, 1.0))
    assert probe.verdict == "admit" and during is None and len(asked) == 2, "one probe, the rest skip"
    assert (await provider_rate.admit("p", 2, 1.0)).verdict == "admit" and len(asked) == 3


async def test_a_slow_store_reply_is_an_unknown_verdict_within_its_deadline(monkeypatch, fresh_breaker):
    class SlowClient:
        async def eval(self, *a):
            await asyncio.sleep(1.0)
    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SlowClient()
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "store", lambda: store)
    started = time.monotonic()
    assert await provider_rate.admit("p", 2, 1.0) is None
    assert time.monotonic() - started < 0.5


@pytest.fixture
async def redis_store(monkeypatch, fresh_breaker):
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


async def test_the_window_admits_its_limit_then_says_when_the_oldest_expires(redis_store):
    bucket = f"treg-test-{uuid4().hex}"
    first, second, third = [await provider_rate.admit(bucket, 2, 0.5) for _ in range(3)]
    assert (first.verdict, second.verdict, third.verdict) == ("admit", "admit", "over")
    assert third.retry_after_s == 1, "rounded up to a whole second"
    await asyncio.sleep(0.6)
    assert (await provider_rate.admit(bucket, 2, 0.5)).verdict == "admit"


async def test_a_changed_rate_starts_its_own_window(redis_store):
    bucket = f"treg-test-{uuid4().hex}"
    assert [(await provider_rate.admit(bucket, 1, 60.0)).verdict for _ in range(2)] == ["admit", "over"]
    assert (await provider_rate.admit(bucket, 1, 1.0)).verdict == "admit"


async def test_a_released_member_gives_its_room_back(redis_store):
    bucket = f"treg-test-{uuid4().hex}"
    taken = await provider_rate.admit(bucket, 1, 60.0)
    assert (await provider_rate.admit(bucket, 1, 60.0)).verdict == "over"
    provider_rate.release(taken)
    await asyncio.gather(*provider_rate._pending)
    assert (await provider_rate.admit(bucket, 1, 60.0)).verdict == "admit"
