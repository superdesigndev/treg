"""Calls in progress on one treg-owned provider account, counted across every server process.

One sorted set per account in the shared store (`infra/kv.py`), on the same lease scripts as org
concurrency: each call owns a member that expires unless renewed, so a crashed process frees its
places within one lease. A lease bounds abandoned occupancy; it cannot stop a request already sent.
No shared store (`TREG_KV_URL` unset) means `enabled()` is false; a store fault reads as `None`.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
import uuid
from typing import Final, Literal

from . import kv

log = logging.getLogger("treg.provider_concurrency")
_LEASE_S = 30.0
BUSY: Final = "busy"
_pending: set[asyncio.Task] = set()
# Leases whose acquire or release got no answer: the store may hold the place anyway (an acquire
# it ran late, or a release it dropped), so each is released again once the store answers.
_unanswered: collections.deque[Lease] = collections.deque(maxlen=10_000)


def enabled() -> bool:
    return kv.configured()


def _background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


class Lease:
    """One place on one account. `release()` is sync and idempotent; the store write runs behind."""

    def __init__(self, store: kv.Store, key: str) -> None:
        self.store = store
        self.key = key
        self.token = uuid.uuid4().hex
        self.ttl_ms = int(_LEASE_S * 1000)
        # Local clock bound for the Redis-time deadline: set from before the write was sent.
        self.valid_until = time.monotonic() + _LEASE_S
        self.renewal: asyncio.Task | None = None
        self._released = False

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(_LEASE_S / 3)
            started = time.monotonic()
            if started >= self.valid_until:
                outcome = "deadline_expired"
            else:
                outcome = await self.store.renew_slot(self.key, self.token, self.ttl_ms)
            if outcome == "renewed" and time.monotonic() < self.valid_until:
                self.valid_until = started + _LEASE_S
                continue
            if outcome == "unavailable" and time.monotonic() < self.valid_until:
                continue
            # Never recreate an expired owner; the call that holds it continues.
            log.warning("provider_concurrency lease_lost key=%s outcome=%s", self.key, outcome)
            return

    async def _release(self, *, quiet: bool = False) -> None:
        if await self.store.release_slot(self.key, self.token) == "unavailable":
            _unanswered.append(self)  # released again once the store answers
            if not quiet:
                log.warning("provider_concurrency release_unavailable key=%s; expires by lease", self.key)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.renewal is not None:
            self.renewal.cancel()
        _background(self._release())


async def _drop_after(attempt: asyncio.Future, lease: Lease) -> None:
    """The write may land after the caller left: wait for it, then drop only this token."""
    results = await asyncio.gather(attempt, return_exceptions=True)
    if results[0] not in ("acquired", "busy"):
        _unanswered.append(lease)
    await lease._release(quiet=True)


_DRAIN_WORKERS = 4
_draining: asyncio.Task | None = None


def _release_unanswered() -> None:
    """The store answers again: release the tokens that got no answer once more. Every one is
    released, however old: the store stamps a late acquire with its own clock at recovery, so it
    holds the place a full lease from then, and a token already gone just answers not owned.
    One background drain with a few requests at a time, so a long list never crowds the shared
    client's connections that other callers need; it stops at the first request the store does
    not answer and keeps the rest for the next answer."""
    global _draining
    loop = asyncio.get_running_loop()
    if not _unanswered or (_draining is not None and not _draining.done()
                           and _draining.get_loop() is loop):
        return
    _draining = loop.create_task(_drain())


async def _drain() -> None:
    stopped = False

    async def worker() -> None:
        nonlocal stopped
        while _unanswered and not stopped:
            lease = _unanswered.popleft()
            try:
                outcome = await lease.store.release_slot(lease.key, lease.token)
            except Exception:  # noqa: BLE001 - a store fault keeps the lease for later
                outcome = "unavailable"
            if outcome == "unavailable":
                _unanswered.appendleft(lease)
                stopped = True

    await asyncio.gather(*(worker() for _ in range(_DRAIN_WORKERS)))


async def try_acquire(account: str, cap: int) -> Lease | None | Literal["busy"]:
    """One attempt at a place on `account` among `cap` shared by every process: a renewed `Lease`,
    `BUSY` when every place is taken, `None` when the store does not answer."""
    return await try_acquire_slot(f"treg:provider-concurrency:v1:{account}", cap)


async def try_acquire_slot(key: str, cap: int) -> Lease | None | Literal["busy"]:
    """`try_acquire` on any sorted set of places: `key` names it whole."""
    try:
        store = kv.store()
    except Exception:  # noqa: BLE001 - a store that cannot initialize reads as unavailable
        return None
    lease = Lease(store, key)
    attempt = asyncio.ensure_future(store.acquire_slot(lease.key, lease.token, lease.ttl_ms, cap))
    try:
        outcome = await asyncio.shield(attempt)
    except asyncio.CancelledError:
        _background(_drop_after(attempt, lease))
        raise
    if outcome in ("acquired", "busy"):
        _release_unanswered()
    if outcome == "acquired" and time.monotonic() < lease.valid_until:
        lease.renewal = asyncio.get_running_loop().create_task(lease._renew())
        return lease
    if outcome == "busy":
        return BUSY
    # A late "acquired" may name an owner that already expired, and a lost reply may have
    # acquired: drop only this token. A late one asks again; a failing store reads as `None`.
    _background(lease._release(quiet=True))
    if outcome != "acquired":
        _unanswered.append(lease)
    return BUSY if outcome == "acquired" else None
