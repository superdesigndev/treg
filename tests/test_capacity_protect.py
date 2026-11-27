"""Provider capacity: Protect: refuse-before-reserve on an exhausted
platform account, the typed `provider_capacity` 503, and the call-path breaker (strike, lock,
probe, clear).

Tiers 1/2 (an org's own tool or key) never consult any of this."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import archive, audit, ratestore
from treg.config import get_settings
from treg.application.call import service as call_service
from treg.application.call import settle as call_settle
from treg.application.call.types import UpstreamResponse
from treg.infra import org_traffic
from treg.infra.db import session_maker
from treg.domain.capacity import events as capacity_events
from treg.domain.capacity import marks as capacity_marks
from treg.domain.capacity.marks import DEFAULT_LOCK, LOCK_NS, MAX_LOCK, Lock
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.domain.catalog import store as catalog_store
from treg.models import Hold, LedgerEntry
from treg.timeutil import utcnow_naive

from test_marketplace_call import (  # noqa: F401
    EP, EP_MICRO, PLATFORM_KEYS, _balance, _fake_relay, platform_on,
)

OUT = b'{"detail":"Insufficient balance"}'  # matches the bare-402 balance signature


async def _publish(provider: str, *, exhausted: bool, hours: float = 1.0, health: str | None = None,
                   ago: timedelta = timedelta(0)):
    now = utcnow_naive()
    state = LatestState(provider, 0.0 if exhausted else 500.0, "USD", now - ago, "exact",
                        exhausted_until=(now + timedelta(hours=hours)) if exhausted else None,
                        health=health or ("exhausted" if exhausted else "ok"))
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, provider, state.to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()


async def _lock(key: str) -> Lock | None:
    async with session_maker() as db:
        raw = await ratestore.kv_get(db, LOCK_NS, key)
    return Lock.from_json(raw) if raw else None


async def _rows(model):
    async with session_maker() as db:
        return (await db.execute(select(model))).scalars().all()


def _relay(status: int, body: bytes, headers=()):
    async def relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        async def _s():
            yield body
        async def _c():
            return None
        return UpstreamResponse(status, tuple(headers), _s(), _c)
    return relay


async def _no_wait(*_args, **_kwargs) -> int:
    return 0


def _counting(relay):
    """The relay, counting the calls that reached the vendor: a probe and a refusal before any
    hold both answer the typed 503, and only this tells them apart."""
    async def counted(*a, **k):
        counted.calls += 1
        return await relay(*a, **k)
    counted.calls = 0
    return counted


def _capacity_503(r) -> bool:
    return r.status_code == 503 and r.json()["detail"]["error"] == "provider_capacity_unavailable"


async def _lock_by_two_signals(clients: AsyncClient, monkeypatch, relay=None) -> Lock:
    monkeypatch.setattr(capacity_marks, "STRIKE_MIN_GAP", timedelta(0))  # two calls, seconds apart
    monkeypatch.setattr(call_service, "relay", relay or _fake_relay(402, OUT))
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7"))
    assert not (await _lock("tikhub") or await _lock(EP)).is_active(), "one strike never locks"
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7"))
    lock = await _lock("tikhub") or await _lock(EP)
    assert lock.is_active()
    return lock


async def test_exhausted_platform_account_is_refused_before_any_hold(clients: AsyncClient, platform_on):
    await _publish("tikhub", exhausted=True)
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503, r.text
    assert r.headers["X-Treg-Error"] == "1"
    assert "X-Treg-Call-Id" in r.headers
    body = r.json()["detail"]
    assert body["error"] == "provider_capacity_unavailable" and body["provider"] == "tikhub"
    assert body["endpoint_id"] == EP and body["resets_at"] and "own key" in body["message"]
    assert isinstance(body["alternatives"], list)
    assert await _balance(clients) == before, "no charge"
    assert await _rows(Hold) == [], "refused BEFORE reserve: no hold row ever existed"
    assert {e.kind for e in await _rows(LedgerEntry)} <= {"grant"}, "no reserve/release entry either"
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert row["status_code"] == 503 and row["refused_by"] == "capacity" and row["tool_name"] == EP


async def test_a_capacity_refusal_names_the_provider_in_the_request_log(clients: AsyncClient, platform_on, caplog):
    await _publish("tikhub", exhausted=True)
    caplog.set_level(logging.INFO, logger=org_traffic.log.name)
    org_traffic.log.addHandler(caplog.handler)
    try:
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 503
    finally:
        org_traffic.log.removeHandler(caplog.handler)
    [row] = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
             if r.name == org_traffic.log.name and r.getMessage().startswith("request ")]
    assert (row["status"], row["provider"], row["refused_by"]) == (503, "tikhub", "capacity")


async def test_own_key_is_never_affected_by_an_exhausted_platform_account(clients: AsyncClient, platform_on):
    await _publish("tikhub", exhausted=True)
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier 2
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer MKKEY"


async def test_a_stale_or_ok_view_never_refuses(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    await _publish("tikhub", exhausted=False)
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    now = utcnow_naive()
    stale = LatestState("tikhub", None, "", now - timedelta(hours=9), "stale", health="stale")
    async with session_maker() as db:
        await ratestore.kv_put(db, STATE_NS, "tikhub", stale.to_json(), ttl_s=3600)
        await db.commit()
    capacity_view.invalidate()
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200


async def test_one_signature_is_a_strike_that_a_2xx_erases(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    before = await _balance(clients)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert _capacity_503(r), "treg's typed refusal, not the vendor's 402"
    assert "Insufficient balance" not in r.text and await _balance(clients) == before
    lock = await _lock("tikhub")
    assert lock.strikes == 1 and not lock.is_active()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200, "never refused"
    assert await _lock("tikhub") is None, "a success between two signals resets the count"


async def test_two_signatures_lock_and_the_next_call_is_refused(clients: AsyncClient, platform_on, monkeypatch):
    lock = await _lock_by_two_signals(clients, monkeypatch)
    assert "balance signature" in lock.note
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"never":"reached"}'))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "provider_capacity_unavailable"
    assert "once a minute" in r.json()["detail"]["message"]
    assert await _rows(Hold) == []


async def test_a_probe_a_minute_goes_through_and_its_2xx_clears_the_lock(clients: AsyncClient, platform_on, monkeypatch):
    lock = await _lock_by_two_signals(clients, monkeypatch)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 503, "the first probe waits"
    capacity_marks._last_probe.clear()  # a minute passes
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, "the probe is a real call: the caller gets the answer"
    assert await _lock(lock.key) is None
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200, "open again for everyone"


async def test_a_probe_that_fails_again_keeps_the_lock(clients: AsyncClient, platform_on, monkeypatch):
    lock = await _lock_by_two_signals(clients, monkeypatch)
    relay = _counting(_fake_relay(402, OUT))
    monkeypatch.setattr(call_service, "relay", relay)
    capacity_marks._last_probe.clear()
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7")) and relay.calls == 1, "the probe is sent"
    assert (await _lock(lock.key)).lock_id == lock.lock_id
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7")) and relay.calls == 1


@pytest.fixture
def capacity_events_log(caplog):
    caplog.set_level(logging.INFO, logger=capacity_events.log.name)
    capacity_events.log.addHandler(caplog.handler)
    try:
        yield lambda: [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
                       if r.name == capacity_events.log.name]
    finally:
        capacity_events.log.removeHandler(caplog.handler)


async def test_events_name_each_transition_once_and_never_the_body(
        clients: AsyncClient, platform_on, monkeypatch, capacity_events_log):
    lock = await _lock_by_two_signals(clients, monkeypatch, _fake_relay(402, b'{"detail":"Insufficient balance","q":"secret-query"}'))
    capacity_marks._last_probe.clear()
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7")), "a probe that fails again"
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 503
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    capacity_marks._last_probe.clear()
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    events = capacity_events_log()
    assert [e["event"] for e in events] == ["strike", "lock_opened", "lock_cleared"], \
        "the failed probe meets the lock in place: no second opening"
    opened = events[1]
    assert opened["provider"] == "tikhub" and opened["kind"] == "balance" and opened["status"] == 402
    assert opened["key"] == lock.key and opened["lock_id"] == lock.lock_id and opened["until"]
    assert events[2]["lock_id"] == lock.lock_id and events[2]["via"] == "probe"
    assert events[2]["locked_s"] >= 0
    assert "secret-query" not in json.dumps(events)


async def test_a_lock_is_aged_from_its_opening_not_its_first_strike(
        clients: AsyncClient, platform_on, monkeypatch, capacity_events_log):
    await capacity_marks.strike("tikhub", endpoint_id=EP, kind="balance", resets_at=None,
                                now=utcnow_naive() - timedelta(minutes=8))
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7"))
    lock = await _lock("tikhub")
    assert lock.is_active() and lock.opened_at - lock.first_signal_at >= timedelta(minutes=7)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    capacity_marks._last_probe.clear()
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    [cleared] = [e for e in capacity_events_log() if e["event"] == "lock_cleared"]
    assert cleared["locked_s"] < 60, "eight minutes of pending strike are not lock time"


async def test_a_vendor_500_or_caller_400_neither_strikes_nor_clears(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(capacity_marks, "STRIKE_MIN_GAP", timedelta(0))
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7"))
    for status, body in ((500, b"down"), (400, b'{"detail":"bad aweme_id"}'), (429, b'{"detail":"slow down"}')):
        monkeypatch.setattr(call_service, "relay", _fake_relay(status, body))
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == status
        assert (await _lock("tikhub")).strikes == 1, "only a 2xx is evidence of capacity"
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7"))
    assert (await _lock("tikhub")).is_active(), "402, 500, 402 is still two strikes in a row"


async def test_an_account_out_refusal_tells_clients_when_to_retry(clients: AsyncClient, platform_on, monkeypatch):
    """The replaced answer is a 503, which many clients retry at once: it says when to come back,
    the lock's reset when one is known, else a minute."""
    monkeypatch.setattr(capacity_marks, "STRIKE_MIN_GAP", timedelta(0))
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert _capacity_503(r) and r.headers["Retry-After"] == "60", "no reset known yet"
    monkeypatch.setattr(call_service, "relay", _relay(429, b'{"detail":"quota"}', [(b"retry-after", b"7200")]))
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7"))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert _capacity_503(r)
    capacity_marks._last_probe.clear()
    r = await clients.get(f"/call/{EP}?aweme_id=7")  # the probe meets the same quota
    assert _capacity_503(r) and 7000 < int(r.headers["Retry-After"]) <= 7200, r.headers


async def test_a_refusal_before_any_hold_tells_clients_when_to_retry(clients: AsyncClient, platform_on):
    await _publish("tikhub", exhausted=True)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert _capacity_503(r)
    resets = datetime.fromisoformat(r.json()["detail"]["resets_at"].rstrip("Z"))
    wait = (resets - utcnow_naive()).total_seconds()
    assert wait - 5 <= int(r.headers["Retry-After"]) <= wait + 5, "the seconds until the reset"


async def test_steady_traffic_while_dry_never_extends_the_lock_and_a_probe_lifts_it(
        clients: AsyncClient, platform_on, monkeypatch):
    """Three minutes of five calls a minute while the account stays dry: only one probe a minute
    reaches the provider, nothing is charged and the lock keeps its end. After a top-up the next
    probe lifts it for everyone."""
    lock = await _lock_by_two_signals(clients, monkeypatch)
    before = await _balance(clients)
    dry = _counting(_fake_relay(402, OUT))
    monkeypatch.setattr(call_service, "relay", dry)
    for _ in range(3):
        capacity_marks._last_probe.clear()  # a minute passes
        for _ in range(5):
            assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7"))
        now = await _lock(lock.key)
        assert now.lock_id == lock.lock_id and now.until == lock.until, "never extended"
    assert dry.calls == 3 and await _balance(clients) == before
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    capacity_marks._last_probe.clear()
    assert [(await clients.get(f"/call/{EP}?aweme_id=7")).status_code for _ in range(5)] == [200] * 5
    assert await _lock(lock.key) is None


async def test_a_quota_lock_ends_at_its_reset_without_a_probe(clients: AsyncClient, platform_on, monkeypatch):
    relay = _relay(429, b'{"detail":"quota"}', [(b"retry-after", b"7200")])
    await _lock_by_two_signals(clients, monkeypatch, relay)
    later = utcnow_naive() + timedelta(hours=2, minutes=1)
    monkeypatch.setattr(capacity_marks, "utcnow_naive", lambda: later)
    capacity_view.invalidate()
    ok = _counting(_fake_relay(200, b'{"ok":true}'))
    monkeypatch.setattr(call_service, "relay", ok)
    assert [(await clients.get(f"/call/{EP}?aweme_id=7")).status_code for _ in range(10)] == [200] * 10
    assert ok.calls == 10


async def test_a_retry_with_the_same_idempotency_key_after_the_refusal_is_served(
        clients: AsyncClient, platform_on, monkeypatch):
    """The refusal is not stored under the key: once the account serves again, the same key gets
    the answer, not a replay of the 503."""
    headers = {"Idempotency-Key": "account-out-retry"}
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7", headers=headers))
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=7", headers=headers)).status_code == 200


async def test_quota_429_locks_the_endpoint_only_until_the_reset(clients: AsyncClient, platform_on, monkeypatch):
    relay = _relay(429, b'{"detail":"quota"}', [(b"retry-after", b"7200")])
    lock = await _lock_by_two_signals(clients, monkeypatch, relay)
    assert lock.key == EP and await _lock("tikhub") is None, "an allowance is per operation"
    assert timedelta(hours=1, minutes=55) < (lock.until - utcnow_naive()) <= timedelta(hours=2)
    await capacity_view.load()
    assert capacity_view.is_exhausted("tikhub", EP) and not capacity_view.is_exhausted("tikhub")


async def test_a_burst_of_concurrent_signals_is_one_strike(clients: AsyncClient, platform_on):
    t0 = utcnow_naive()
    kw = dict(endpoint_id=EP, kind="balance", resets_at=None)
    for offset in (0, 1, 3, 8):  # parallel callers hitting the same empty instant
        lock = await capacity_marks.strike("tikhub", now=t0 + timedelta(seconds=offset), **kw)
        assert lock.strikes == 1 and not lock.is_active(t0 + timedelta(seconds=offset))
    lock = await capacity_marks.strike("tikhub", now=t0 + timedelta(seconds=20), **kw)
    assert lock.strikes == 2 and lock.is_active(t0 + timedelta(seconds=20)), "a second, later burst locks"


async def test_a_lock_never_outlives_the_ceiling_whatever_the_vendor_said(clients: AsyncClient, platform_on):
    far = utcnow_naive() + timedelta(days=10)
    await capacity_marks.strike("tikhub", endpoint_id=EP, kind="quota", resets_at=far,
                                now=utcnow_naive() - timedelta(seconds=30))
    lock = await capacity_marks.strike("tikhub", endpoint_id=EP, kind="quota", resets_at=far)
    assert lock.is_active() and lock.until - utcnow_naive() <= MAX_LOCK


async def test_a_guessed_hold_lasts_an_hour(clients: AsyncClient, platform_on, monkeypatch):
    lock = await _lock_by_two_signals(clients, monkeypatch)
    assert timedelta(minutes=59) < lock.until - utcnow_naive() <= DEFAULT_LOCK


async def test_a_pending_endpoint_strike_does_not_hide_a_provider_lock(clients: AsyncClient, platform_on, monkeypatch):
    await capacity_marks.strike("tikhub", endpoint_id=EP, kind="quota", resets_at=None)  # one, pending
    await _lock_by_two_signals(clients, monkeypatch)  # provider-wide, active
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"never":"reached"}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 503


async def test_a_2xx_clears_a_strike_the_cached_view_has_not_seen(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200  # the view is loaded, no lock
    # another call's settle strikes while this one is in flight: it writes, then invalidates
    await capacity_marks.strike("tikhub", endpoint_id=EP, kind="balance", resets_at=None)
    capacity_view.invalidate()
    assert (await _lock("tikhub")).strikes == 1 and capacity_view.locks("tikhub", EP) == []
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert await _lock("tikhub") is None, "the success counts even though this process's view was stale"


async def test_a_probe_never_takes_an_archived_answer(clients: AsyncClient, platform_on, monkeypatch):
    lock = await _lock_by_two_signals(clients, monkeypatch)
    monkeypatch.setattr(get_settings(), "archive_mode", "serve")
    lookups = []

    async def lookup(**kw):
        lookups.append(kw)
        return None
    monkeypatch.setattr(archive, "lookup", lookup)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    capacity_marks._last_probe.clear()
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert lookups == [], "a probe must reach the vendor"
    assert await _lock(lock.key) is None
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert len(lookups) == 1, "an ordinary call consults the archive again"


async def test_clear_is_conditional_on_the_lock_id(clients: AsyncClient, platform_on):
    await capacity_marks.strike("tikhub", endpoint_id=EP, kind="balance", resets_at=None,
                                now=utcnow_naive() - timedelta(seconds=30))
    lock = await capacity_marks.strike("tikhub", endpoint_id=EP, kind="balance", resets_at=None)
    assert lock.is_active()
    assert not await capacity_marks.clear("tikhub", lock_id="stale-probe")
    assert not await capacity_marks.clear("tikhub", lock_id=None), "a plain 2xx never clears a lock"
    assert (await _lock("tikhub")).lock_id == lock.lock_id
    assert await capacity_marks.clear("tikhub", lock_id=lock.lock_id)
    assert await _lock("tikhub") is None


async def _state(provider: str) -> LatestState:
    async with session_maker() as db:
        return LatestState.from_json(await ratestore.kv_get(db, STATE_NS, provider))


async def test_a_sweep_reading_admits_a_probe_a_minute_and_its_2xx_lifts_it(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"ok":true}'))
    await _publish("tikhub", exhausted=True)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 503, "the first probe waits a minute after the reading"
    assert "once a minute" in r.json()["detail"]["message"]
    await _publish("tikhub", exhausted=True, ago=timedelta(minutes=2))  # topped up since the reading
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200, "the probe is a real call"
    assert (await _state("tikhub")).exhausted_until is None
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200, "open again for everyone"


async def test_a_probe_through_a_sweep_reading_that_fails_keeps_it(clients: AsyncClient, platform_on, monkeypatch):
    relay = _counting(_fake_relay(402, OUT))
    monkeypatch.setattr(call_service, "relay", relay)
    await _publish("tikhub", exhausted=True, ago=timedelta(minutes=2))
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7")) and relay.calls == 1, "the probe is sent"
    assert (await _state("tikhub")).exhausted_until is not None
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7")) and relay.calls == 1, "one probe a minute"


async def test_a_probe_never_lifts_a_newer_sweep_reading(clients: AsyncClient, platform_on):
    await _publish("tikhub", exhausted=True, ago=timedelta(minutes=2))
    old = capacity_marks.sweep_probe_id((await _state("tikhub")).observed_at)
    await _publish("tikhub", exhausted=True)
    assert not await capacity_marks.clear_sweep_state("tikhub", probe_id=old)
    assert (await _state("tikhub")).exhausted_until is not None
    new = capacity_marks.sweep_probe_id((await _state("tikhub")).observed_at)
    assert await capacity_marks.clear_sweep_state("tikhub", probe_id=new)


async def test_the_sweep_cannot_undo_a_call_path_lock(clients: AsyncClient, platform_on, monkeypatch):
    await _lock_by_two_signals(clients, monkeypatch)
    await _publish("tikhub", exhausted=False)  # the balance API sees a different meter
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"never":"reached"}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 503


async def test_a_failed_strike_never_fails_the_call(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b"out"))

    async def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(call_settle.capacity_marks, "strike", boom)
    # strike itself swallows; simulate the seam above it raising to prove the guard
    assert _capacity_503(await clients.get(f"/call/{EP}?aweme_id=7"))


DL_PHONE, DL_SEARCH = "dropleads.people.phone.find", "dropleads.people.search"  # per_success / free
DL_OUT = b'{"error":"Insufficient Credits","message":"Insufficient credits. Required: 3, Available: 0"}'


def _by_path(answers: dict[str, tuple[int, bytes]]):
    async def relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        status, body = next(v for path, v in answers.items() if path in upstream_url)
        return await _relay(status, body)(request, upstream_url, tool, secrets, client)
    return relay


async def _dl(clients: AsyncClient, endpoint: str):
    body = ({"linkedin_url": "https://www.linkedin.com/in/example"} if endpoint == DL_PHONE
            else {"filters": {"companyDomains": ["example.com"]}})
    return await clients.post(f"/call/{endpoint}", json=body)


async def test_a_free_route_neither_lifts_nor_is_held_by_a_balance_mark(clients: AsyncClient, monkeypatch):
    """Dropleads answers searches (free) at zero credits while every paid route 402s. Each free 2xx
    used to erase the paid routes' balance strikes and lift the sweep's zero reading, so treg relayed
    every paid call into a 402, while the free route was refused whenever a mark did stand. A route
    that spends nothing proves nothing about the balance, either way."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_DROPLEADS", "PLATFORM-DROPLEADS-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dropleads")
    get_settings.cache_clear()
    monkeypatch.setattr(capacity_marks, "STRIKE_MIN_GAP", timedelta(0))
    monkeypatch.setattr(call_service, "relay", _by_path({
        "/mobile-finder": (402, DL_OUT), "/leads/search": (200, b'{"data":[]}')}))
    try:
        assert _capacity_503(await _dl(clients, DL_PHONE))
        assert (await _lock("dropleads")).strikes == 1
        assert (await _dl(clients, DL_SEARCH)).status_code == 200
        assert (await _lock("dropleads")).strikes == 1, "a free 2xx keeps the paid route's strike"
        assert _capacity_503(await _dl(clients, DL_PHONE))
        assert (await _lock("dropleads")).is_active(), "the second strike locks"
        assert (await _dl(clients, DL_PHONE)).status_code == 503
        capacity_marks._last_probe.clear()
        assert (await _dl(clients, DL_SEARCH)).status_code == 200, "the lock never refuses a free route"
        assert (await _lock("dropleads")).is_active(), "and a free 2xx is no probe"

        await capacity_marks.clear("dropleads", lock_id=(await _lock("dropleads")).lock_id)
        await _publish("dropleads", exhausted=True, ago=timedelta(minutes=2))
        assert (await _dl(clients, DL_SEARCH)).status_code == 200
        assert (await _state("dropleads")).exhausted_until is not None, "nor lifts the sweep's zero"
        assert _capacity_503(await _dl(clients, DL_PHONE)), "the paid probe is sent and fails"
        assert _capacity_503(await _dl(clients, DL_PHONE))
    finally:
        get_settings.cache_clear()


WIZA_ENRICH, WIZA_EMAIL = "wiza.companies.enrich", "wiza.people.email.find"
WIZA_OUT = b'{"status":{"code":400,"message":"Insufficient API credits. You need 2 credits for company enrichment."}}'
WIZA_EMAIL_BODY = {"individual_reveal": {"profile_url": "https://www.linkedin.com/in/example"},
                   "enrichment_level": "partial",
                   "email_options": {"accept_work": True, "accept_personal": False, "accept_generic": False}}


async def test_a_wiza_200_between_two_credit_errors_keeps_the_lock_closed(clients: AsyncClient, monkeypatch):
    """A 2xx from another Wiza route clears the pending strike of a credit error, so 400, 200, 400
    does not lock."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_WIZA", "PLATFORM-WIZA-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "wiza")
    get_settings.cache_clear()
    monkeypatch.setattr(capacity_marks, "STRIKE_MIN_GAP", timedelta(0))
    monkeypatch.setattr(call_service.provider_limiter, "acquire", _no_wait)  # Wiza's published pacing
    monkeypatch.setattr(call_service, "relay", _by_path({
        "/company_enrichments": (400, WIZA_OUT),
        "/individual_reveals": (200, b'{"data":{"id":910001,"status":"queued"}}')}))
    try:
        assert _capacity_503(await clients.post(f"/call/{WIZA_ENRICH}", json={"company_domain": "example.com"}))
        assert (await _lock("wiza")).strikes == 1, "the credit error is a strike"
        assert (await clients.post(f"/call/{WIZA_EMAIL}", json=WIZA_EMAIL_BODY)).status_code == 200
        assert await _lock("wiza") is None, "the find's 200 erases the strike"
        assert _capacity_503(await clients.post(f"/call/{WIZA_ENRICH}", json={"company_domain": "example.com"}))
        lock = await _lock("wiza")
        assert lock.strikes == 1 and not lock.is_active(), "400, 200, 400 does not lock"
    finally:
        get_settings.cache_clear()


APIFY_DEAD_TOKEN = (b'{"error":{"type":"user-or-token-not-found","message":"User was not found or authentication '
                    b'token is not valid"}}')

# A provider's own words for treg's account being out, beside a mistake of the caller's that the same
# provider answers: (endpoint, treg's account out, the caller's mistake). Each call sends the row's
# catalog `test_request`.
_ACCOUNT_OUT = [
    pytest.param("apify.linkedin.search.jobs", (401, APIFY_DEAD_TOKEN),
                 (400, b'{"error":{"type":"invalid-input","message":"Input is not valid: Field input.jobTitles '
                       b'must be array"}}'), id="apify"),
    pytest.param("dataforseo.google.keywords.volume",
                 (403, b'{"version":"0.1.20251117","status_code":40104,"status_message":"Please verify your '
                       b'account before using the API.","cost":0,"tasks_count":0,"tasks_error":0,"tasks":null}'),
                 (200, b'{"version":"0.1.20251117","status_code":20000,"status_message":"Ok.","cost":0,'
                       b'"tasks_count":1,"tasks_error":1,"tasks":[{"status_code":40501,'
                       b'"status_message":"Invalid Field: \'location_code\'.","cost":0,"result":null}]}'),
                 id="dataforseo"),
    pytest.param("marketstack.eod.latest",
                 (401, b'{"error":{"code":"inactive_user","message":"Permission denied - User not active"}}'),
                 (422, b'{"error":{"code":"validation_error","message":"Request failed with validation error"}}'),
                 id="marketstack"),
    pytest.param("minimax.voice-gen.speech-2-8-hd",
                 (200, b'{"data":null,"base_resp":{"status_code":1008,"status_msg":"insufficient balance"}}'),
                 (200, b'{"data":null,"base_resp":{"status_code":2013,"status_msg":"invalid params"}}'),
                 id="minimax"),
    pytest.param("openrouter.ai-judge.decide",
                 (403, b'{"error":{"code":403,"message":"Your account has an overdue balance"}}'),
                 (403, b'{"error":{"code":403,"message":"Your chosen model requires moderation and your input '
                       b'was flagged"}}'), id="openrouter"),
    pytest.param("scrubby.people.email.verify", (403, b'{"detail": "Insufficient credits."}'),
                 (422, b'{"detail":[{"loc":["body","email"],"msg":"value is not a valid email address"}]}'),
                 id="scrubby"),
]


def _on_tregs_key(monkeypatch, provider: str) -> None:
    key = "login:password" if provider == "dataforseo" else f"PLATFORM-{provider.upper()}-KEY"
    monkeypatch.setenv(f"TREG_PLATFORM_KEY_{provider.upper()}", key)
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", provider)
    monkeypatch.setattr(call_service.provider_limiter, "acquire", _no_wait)
    get_settings.cache_clear()


async def _test_request(clients: AsyncClient, endpoint: str, **query):
    ep = catalog_store.load().by_id[endpoint]
    sent = ep["test_request"]
    params = {**(sent.get("pathParams") or {}), **(sent.get("queryParams") or {}), **query}
    return await clients.request(ep["method"], f"/call/{endpoint}", params=params, json=sent.get("body"))


@pytest.mark.parametrize("endpoint, out, mistake", _ACCOUNT_OUT)
async def test_a_providers_own_account_answer_on_tregs_key_is_the_typed_refusal(
        clients: AsyncClient, monkeypatch, endpoint, out, mistake):
    """On treg's key the provider's answer for treg's account (empty, unpaid, deactivated) is the
    typed 503, uncharged, and a strike; the caller's own mistake is relayed as sent, no strike."""
    provider = endpoint.split(".")[0]
    _on_tregs_key(monkeypatch, provider)
    try:
        monkeypatch.setattr(call_service, "relay", _fake_relay(*mistake))
        r = await _test_request(clients, endpoint)
        assert (r.status_code, r.content) == mistake, r.text
        assert await _lock(provider) is None

        monkeypatch.setattr(call_service, "relay", _fake_relay(*out))
        before = await _balance(clients)
        r = await _test_request(clients, endpoint)
        assert _capacity_503(r), r.text
        assert out[1].decode() not in r.text
        assert await _balance(clients) == before and await _rows(Hold) == []
        assert (await _lock(provider)).strikes == 1
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("endpoint, out, _mistake", _ACCOUNT_OUT)
async def test_the_same_answer_on_a_teams_own_key_is_relayed_as_sent(
        clients: AsyncClient, monkeypatch, endpoint, out, _mistake):
    """The team's own key wins over treg's, and its account is the team's business."""
    provider = endpoint.split(".")[0]
    _on_tregs_key(monkeypatch, provider)
    try:
        assert (await clients.post("/secrets", json={"name": provider, "value": "TEAM-OWN-KEY"})).status_code == 200
        monkeypatch.setattr(call_service, "relay", _fake_relay(*out))
        r = await _test_request(clients, endpoint)
        assert (r.status_code, r.content) == out, r.text
        assert await _lock(provider) is None
    finally:
        get_settings.cache_clear()


async def test_a_callers_own_apify_token_never_reaches_apify_on_tregs_key(clients: AsyncClient, monkeypatch):
    """Apify also reads a `token` query parameter. Refusing one on treg's key, before any hold, is
    what lets a 401 there mean treg's token: a caller's own wrong token gets the same answer."""
    _on_tregs_key(monkeypatch, "apify")
    try:
        relay = _counting(_fake_relay(401, APIFY_DEAD_TOKEN))
        monkeypatch.setattr(call_service, "relay", relay)
        before = await _balance(clients)
        cat = catalog_store.load()
        rows = [ep["id"] for ep in cat.endpoints if ep["provider"] == "apify" and cat.platform_eligible(ep)]
        for endpoint in rows:  # every row on treg's key, the free run polls and a new row included
            r = await _test_request(clients, endpoint, token="CALLER-TOKEN")
            assert r.status_code == 400 and r.json()["detail"]["error"] == "catalog_parameter_invalid", \
                (endpoint, r.text)
        assert rows and relay.calls == 0 and await _balance(clients) == before and await _lock("apify") is None
    finally:
        get_settings.cache_clear()
