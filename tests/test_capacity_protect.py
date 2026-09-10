"""Step D of docs/PROVIDER-CAPACITY-PLAN.md — Protect: refuse-before-reserve on an exhausted
platform account, the typed `provider_capacity` 503, and the call-path breaker (strike, lock,
probe, clear).

Tiers 1/2 (an org's own tool or key) never consult any of this."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from sqlmodel import select

from treg import archive, audit, ratestore
from treg.config import get_settings
from treg.application.call import service as call_service
from treg.application.call import settle as call_settle
from treg.application.call.types import CallFailure, UpstreamResponse
from treg.infra.db import session_maker
from treg.domain.capacity import marks as capacity_marks
from treg.domain.capacity.marks import DEFAULT_LOCK, LOCK_NS, MAX_LOCK, Lock
from treg.domain.capacity.policy import LatestState
from treg.domain.capacity.sweep import STATE_NS
from treg.domain.capacity.view import view as capacity_view
from treg.models import Hold, LedgerEntry
from treg.timeutil import utcnow_naive

from test_marketplace_call import EP, EP_MICRO, PLATFORM_KEYS, _balance, _fake_relay, platform_on  # noqa: F401

OUT = b'{"detail":"Insufficient balance"}'  # matches the bare-402 balance signature


async def _publish(provider: str, *, exhausted: bool, hours: float = 1.0, health: str | None = None):
    now = utcnow_naive()
    state = LatestState(provider, 0.0 if exhausted else 500.0, "USD", now, "exact",
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


async def _lock_by_two_signals(clients: AsyncClient, monkeypatch, relay=None) -> Lock:
    monkeypatch.setattr(capacity_marks, "STRIKE_MIN_GAP", timedelta(0))  # two calls, seconds apart
    monkeypatch.setattr(call_service, "relay", relay or _fake_relay(402, OUT))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code in (402, 429)
    assert not (await _lock("tikhub") or await _lock(EP)).is_active(), "one strike never locks"
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code in (402, 429)
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
    assert r.status_code == 402, "the vendor's answer relays unchanged"
    assert "X-Treg-Error" not in r.headers
    assert r.headers["X-Treg-Cost-Micro"] == "0" and await _balance(clients) == before
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
    capacity_marks._last_probe.clear()
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 402, "the probe relays"
    assert (await _lock(lock.key)).lock_id == lock.lock_id
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 503


async def test_a_vendor_500_or_caller_400_neither_strikes_nor_clears(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(capacity_marks, "STRIKE_MIN_GAP", timedelta(0))
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 402
    for status, body in ((500, b"down"), (400, b'{"detail":"bad aweme_id"}'), (429, b'{"detail":"slow down"}')):
        monkeypatch.setattr(call_service, "relay", _fake_relay(status, body))
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == status
        assert (await _lock("tikhub")).strikes == 1, "only a 2xx is evidence of capacity"
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, OUT))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 402
    assert (await _lock("tikhub")).is_active(), "402, 500, 402 is still two strikes in a row"


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
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 402 and r.headers["X-Treg-Cost-Micro"] == "0"


def test_provider_capacity_is_a_treg_blamed_typed_failure():
    exc = CallFailure("provider_capacity", status_code=503, detail={"error": "x"})
    assert exc.blame == "treg" and exc.status_code == 503


async def test_pdl_identify_quota_breaker_preserves_enrichment(clients, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(capacity_marks, 'STRIKE_MIN_GAP', timedelta(0))
    mk = SimpleNamespace(tier='platform', provider='pdl', endpoint_id='pdl.x.person-identify')
    body = b'{"error":{"message":"You have hit your account maximum for person_identify (all matches used)"}}'
    for _ in range(2):
        assert await call_settle._note_capacity_signal(mk, 402, {}, body) == 'quota'
    await capacity_view.load()
    assert capacity_view.is_exhausted('pdl', 'pdl.x.person-identify')
    assert not capacity_view.is_exhausted('pdl', 'pdl.people.enrich')
    assert not capacity_view.is_exhausted('pdl', 'pdl.companies.enrich')
