"""The call-path breaker: the one sanctioned dataplane write of this domain.

A lock lives in ratestore as `capacity:lock:<key>` (`LOCK_NS`), a namespace the sweep never writes,
so a balance API that meters a different allowance than the one that ran out (a monthly meter
beside a daily cap) cannot undo it. The key is the provider for a balance signature (no money
serves nothing) and the endpoint id for a quota one (allowances are per operation: a search cap
says nothing about verifications). The row is a DB write on its own short session AFTER the
settle, never while an upstream request is in flight, and never raises: a lost write costs one
more relayed 402, not the call. Listed in `tests/test_call_architecture.py` as
`capacity_exhausted_mark`.

Open conservatively: a signature is a strike; the second strike within `STRIKE_WINDOW`, at least
`STRIKE_MIN_GAP` after the first (concurrent calls that hit the same instant are one strike), with
no 2xx in between, locks. Close eagerly: while locked, `probe_due` admits one real call per process
per `PROBE_EVERY_S`; that call's 2xx clears the lock (conditionally on the lock id it was admitted
under, so a late probe cannot erase a newer lock). Nothing else clears it except `until`: a guessed
hold lasts `DEFAULT_LOCK`, a vendor-stated reset at most `MAX_LOCK`.

The sweep's own exhausted reading (`capacity:state:<provider>`) gets the same probe: without one, a
top-up stays refused until the next sweep. A probe's 2xx lifts that reading (`clear_sweep_state`),
conditionally on the reading it was admitted under, so a newer sweep's word stands.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from ... import ratestore
from ...infra.db import session_maker
from ...timeutil import utcnow_naive
from .policy import LatestState
from .sweep import STATE_NS, STATE_TTL_S

LOCK_NS = "capacity:lock"
LOCK_TTL_S = 24 * 3600
STRIKE_WINDOW = timedelta(minutes=10)
STRIKE_MIN_GAP = timedelta(seconds=15)
DEFAULT_LOCK = timedelta(hours=1)
MAX_LOCK = timedelta(hours=6)
PROBE_EVERY_S = 60.0
SWEEP_PROBE = "sweep:"

log = logging.getLogger("treg.capacity")


def lock_key(provider: str, endpoint_id: str | None, kind: str, slot: str | None = None) -> str:
    """Provider for a balance signature, endpoint for a quota one; a further treg-owned account
    (`slot`) runs dry on its own, so its rows are its own (`<key>#<slot>`)."""
    key = endpoint_id if kind == "quota" and endpoint_id else provider
    return f"{key}#{slot}" if slot else key


@dataclass
class Lock:
    key: str
    provider: str
    lock_id: str
    strikes: int
    first_signal_at: datetime
    until: datetime | None      # None while only strikes are pending
    note: str = ""

    def is_active(self, now: datetime | None = None) -> bool:
        return self.until is not None and (now or utcnow_naive()) < self.until

    def to_json(self) -> dict:
        return {"key": self.key, "provider": self.provider, "lock_id": self.lock_id,
                "strikes": self.strikes, "first_signal_at": self.first_signal_at.isoformat(),
                "until": self.until.isoformat() if self.until else None, "note": self.note}

    @classmethod
    def from_json(cls, d: dict) -> "Lock":
        return cls(d["key"], d["provider"], d["lock_id"], int(d.get("strikes", 0)),
                   datetime.fromisoformat(d["first_signal_at"]),
                   datetime.fromisoformat(d["until"]) if d.get("until") else None,
                   d.get("note", ""))


async def strike(provider: str, *, endpoint_id: str | None, kind: str,
                 resets_at: datetime | None, note: str = "", immediate: bool = False,
                 now: datetime | None = None, slot: str | None = None) -> Lock | None:
    """One balance/quota signature on treg's own account. Returns the row as written (active or
    pending), or None when nothing could be written. `immediate` locks on the first strike."""
    now = now or utcnow_naive()
    key = lock_key(provider, endpoint_id, kind, slot)
    try:
        async with session_maker() as db:
            raw = await ratestore.kv_get(db, LOCK_NS, key)
            prev = Lock.from_json(raw) if raw else None
            if prev is not None and prev.is_active(now):
                return prev
            if prev is not None and not immediate and now - prev.first_signal_at < STRIKE_MIN_GAP:
                return prev  # the same burst as the first strike
            fresh = prev is None or now - prev.first_signal_at > STRIKE_WINDOW
            strikes = 1 if fresh else prev.strikes + 1
            first_at = now if fresh else prev.first_signal_at
            until = None
            if immediate or strikes >= 2:
                until = min(resets_at or now + DEFAULT_LOCK, now + MAX_LOCK)
            lock = Lock(key, provider, secrets.token_hex(8), strikes, first_at, until, note[:200])
            await ratestore.kv_put(db, LOCK_NS, key, lock.to_json(), ttl_s=LOCK_TTL_S)
            await db.commit()
    except Exception:  # noqa: BLE001 - a lock is a hint for the next call, never this call's fate
        log.warning("capacity lock %s not written", key, exc_info=True)
        return None
    if until is not None:
        _last_probe[key] = time.monotonic()  # the first probe waits a full interval
    return lock


async def clear(key: str, *, lock_id: str | None = None) -> bool:
    """A 2xx on treg's own account. Clears pending strikes, or the active lock the call was
    admitted under (`lock_id`); a lock this call did not probe is left alone."""
    try:
        async with session_maker() as db:
            raw = await ratestore.kv_get(db, LOCK_NS, key)
            if raw is None:
                return False
            lock = Lock.from_json(raw)
            if lock.is_active() and lock.lock_id != lock_id:
                return False
            await ratestore.kv_pop(db, LOCK_NS, key)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.warning("capacity lock %s not cleared", key, exc_info=True)
        return False
    return True


_last_probe: dict[str, float] = {}


def probe_due(key: str) -> bool:
    """Admit one call per process per `PROBE_EVERY_S` through an active lock. Sync, no I/O."""
    now = time.monotonic()
    if now - _last_probe.get(key, float("-inf")) < PROBE_EVERY_S:
        return False
    _last_probe[key] = now
    return True


def sweep_probe_id(observed_at: datetime) -> str:
    return SWEEP_PROBE + observed_at.isoformat()


def sweep_probe_due(provider: str, observed_at: datetime | None, now: datetime | None = None) -> bool:
    """`probe_due` through the sweep's exhausted reading; the first probe waits a full interval
    after the reading, as after a lock. Sync, no I/O."""
    if observed_at is None or (now or utcnow_naive()) - observed_at < timedelta(seconds=PROBE_EVERY_S):
        return False
    return probe_due(SWEEP_PROBE + provider)


async def clear_sweep_state(provider: str, *, probe_id: str) -> bool:
    """A probe's 2xx through the sweep's exhausted reading: lift it until the next sweep reads the
    balance again. A reading published after the probe was admitted is left alone."""
    try:
        async with session_maker() as db:
            raw = await ratestore.kv_get(db, STATE_NS, provider)
            if raw is None:
                return False
            state = LatestState.from_json(raw)
            if (state.exhausted_until is None or state.observed_at is None
                    or probe_id != sweep_probe_id(state.observed_at)):
                return False
            state.exhausted_until = None
            state.health = "unknown"
            state.note = f"a probe's 2xx lifted this exhausted reading; {state.note}"[:200]
            await ratestore.kv_put(db, STATE_NS, provider, state.to_json(), ttl_s=STATE_TTL_S)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.warning("capacity state %s not cleared", provider, exc_info=True)
        return False
    return True
