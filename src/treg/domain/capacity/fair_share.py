"""Fair share of a provider's concurrency on treg's OWN platform keys.

Every team calling a provider on treg's key shares ONE account, and a provider that caps the calls
in progress on that account (`_CONCURRENCY` in policy.py) is first come, first served without
this gate: one team's burst takes every place and every other team's call is refused upstream.

The rule is max-min fairness on the places, and it never leaves a place empty while a call waits:

1. A place is free and no one is waiting: the call goes straight through.
2. Every place is taken: the call waits in one line. Each place that frees goes to the waiting
   call whose team has the FEWEST calls in progress (the oldest call on a tie). A light team's
   call is served at the next free place; a heavy team keeps whatever no lighter team wants.
3. A call that waited `max_wait_s` leaves the line with `ShareBusy`, so a routed job moves to
   its next provider and a direct call is refused (not charged) with a retry hint.

DB-free on purpose, like the rate limiter (infra/upstream/limiter.py): it runs after the DB phase
ended and before the relay. The line and the fewest-first choice are per process. With the shared
store configured (infra/provider_concurrency.py) the `cap` is per account across every process:
every call queues, and each process asks the store for places on the account's behalf, as many at
once as it has free places and waiting calls (one at a time, with backoff, after the store said the
account is full), handing each one that lands to the call rule 2 picks at that moment. A place
that arrives after its call left goes straight back. While the store fails, places are handed out on this process's count
alone; without the store, each process enforces `cap` on its own.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

from ...infra import provider_concurrency

DEFAULT_MAX_WAIT_S = 2.0
RETRY_AFTER_S = 2
_FIRST_POLL_S = 0.02
_MAX_POLL_S = 0.2
_STORE_DOWN_S = 5.0

log = logging.getLogger("treg.fair_share")


class ShareBusy(Exception):
    """No place freed for this call within the wait; `active` is its team's calls in progress."""

    def __init__(self, provider: str, team: int, active: int, waited_s: float) -> None:
        super().__init__(f"{provider}: team {team} waited {waited_s:.1f} s with {active} calls in progress")
        self.provider, self.team, self.active, self.waited_s = provider, team, active, waited_s


@dataclass
class _Waiter:
    team: int
    future: asyncio.Future
    contended: bool  # the call met a full provider; only then does its wait show
    since: float = field(default_factory=time.monotonic)


@dataclass
class _Pool:
    name: str
    cap: int
    shared: bool = False
    active: int = 0
    by_team: dict[int, int] = field(default_factory=dict)
    line: list[_Waiter] = field(default_factory=list)
    asking: set[asyncio.Task] = field(default_factory=set)  # store requests in flight
    store_down_until: float = 0.0
    full_until: float = 0.0  # the store said full; ask again after this
    probing: bool = False  # after "full", one request at a time until a place lands
    poll_s: float = _FIRST_POLL_S
    retry: asyncio.TimerHandle | None = None

    def take(self, team: int) -> None:
        self.active += 1
        self.by_team[team] = self.by_team.get(team, 0) + 1

    def give_back(self, team: int) -> None:
        self.active -= 1
        left = self.by_team.get(team, 0) - 1
        if left > 0:
            self.by_team[team] = left
        else:
            self.by_team.pop(team, None)

    def next_waiter(self) -> _Waiter | None:
        """The waiting call whose team has the fewest in progress, oldest first; drops cancelled."""
        self.line = [w for w in self.line if not w.future.done()]
        if not self.line:
            return None
        return min(self.line, key=lambda w: (self.by_team.get(w.team, 0), w.since))

    def hand(self, waiter: _Waiter, lease: provider_concurrency.Lease | None) -> None:
        self.line.remove(waiter)
        self.take(waiter.team)
        waiter.future.set_result(lease)

    def dispatch(self) -> None:
        """Hand every free place to the waiting call whose team has the fewest in progress."""
        now = time.monotonic()
        if not self.shared or now < self.store_down_until:
            while self.active < self.cap and (waiter := self.next_waiter()) is not None:
                self.hand(waiter, None)
            return
        if now < self.full_until:
            if self.retry is None:
                self.retry = asyncio.get_running_loop().call_later(
                    self.full_until - now, self._retry)
            return
        self.next_waiter()  # drop callers that left, so `line` counts only the waiting
        want = min(self.cap - self.active, len(self.line), 1 if self.probing else self.cap)
        want -= len(self.asking)
        for _ in range(max(0, want)):
            task = asyncio.get_running_loop().create_task(self._ask())
            self.asking.add(task)
            task.add_done_callback(self.asking.discard)

    def _retry(self) -> None:
        self.retry = None
        self.dispatch()

    async def _ask(self) -> None:
        """One store request; the place it gets goes to the call rule 2 picks when it lands."""
        try:
            outcome = await provider_concurrency.try_acquire(self.name, self.cap)
        except Exception:  # the waiting calls time out into ShareBusy rather than fail the call
            log.exception("fair_share store request for %s failed", self.name)
            return
        finally:
            self.asking.discard(asyncio.current_task())  # no longer in flight for `dispatch`
        lease = None
        if outcome == provider_concurrency.BUSY:
            for waiter in self.line:
                waiter.contended = True
            now = time.monotonic()
            if now >= self.full_until:  # replies from the same round back off once
                self.full_until = now + self.poll_s * random.uniform(0.5, 1.0)
                self.poll_s = min(self.poll_s * 2, _MAX_POLL_S)
            self.probing = True
        elif outcome is None:  # skip the store for a while instead of waiting on it per call
            self.store_down_until = time.monotonic() + _STORE_DOWN_S
        else:
            lease = outcome
            self.poll_s, self.probing = _FIRST_POLL_S, False
        if lease is not None:
            waiter = self.next_waiter() if self.active < self.cap else None
            if waiter is None:  # every caller left (or the cap fell) while the store answered
                lease.release()
            else:
                self.hand(waiter, lease)
        self.dispatch()


class Slot:
    """One place held by one call. `release()` is idempotent; every call path releases."""

    __slots__ = ("_gate", "_provider", "_team", "waited_ms", "lease")

    def __init__(self, gate: "FairShare | None", provider: str = "", team: int = 0,
                 waited_ms: int = 0, lease: provider_concurrency.Lease | None = None) -> None:
        self._gate, self._provider, self._team, self.waited_ms = gate, provider, team, waited_ms
        self.lease = lease

    def release(self) -> None:
        if self.lease is not None:
            lease, self.lease = self.lease, None
            lease.release()
        if self._gate is not None:
            gate, self._gate = self._gate, None
            gate._release(self._provider, self._team)


NO_SLOT = Slot(None)


class FairShare:
    def __init__(self) -> None:
        self._pools: dict[str, _Pool] = {}

    def queued(self, provider: str) -> int:
        """Calls in progress or waiting on `provider` beyond its places, 0 when it has room: how a
        provider with several accounts sends a call where a place is free. Sync, takes nothing."""
        pool = self._pools.get(provider)
        return 0 if pool is None else max(0, pool.active + len(pool.line) - pool.cap + 1)

    def _pool(self, provider: str, cap: int) -> _Pool:
        pool = self._pools.get(provider)
        if pool is None:
            pool = self._pools[provider] = _Pool(provider, cap, shared=provider_concurrency.enabled())
        elif pool.cap != cap:  # a changed limit applies at once; a raise frees waiting calls
            pool.cap = cap
            pool.dispatch()
        return pool

    async def acquire(self, provider: str, team: int, cap: int, *,
                      max_wait_s: float = DEFAULT_MAX_WAIT_S) -> Slot:
        """Take a place for `team` on `provider`, waiting at most `max_wait_s` (rule 3)."""
        if cap <= 0:
            return NO_SLOT
        pool = self._pool(provider, cap)
        full = pool.active >= pool.cap or bool(pool.line)
        if not pool.shared and not full:
            pool.take(team)
            return Slot(self, provider, team)
        waiter = _Waiter(team, asyncio.get_running_loop().create_future(), contended=full)
        pool.line.append(waiter)
        pool.dispatch()  # the newcomer may outrank a heavier team for a place that is free now
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), timeout=max_wait_s)
        except asyncio.TimeoutError:
            if waiter.future.done() and not waiter.future.cancelled():
                # handed a place in the same instant the wait ran out: keep it
                return self._slot(provider, waiter)
            waiter.future.cancel()
            if waiter in pool.line:
                pool.line.remove(waiter)
            raise ShareBusy(provider, team, pool.by_team.get(team, 0),
                            time.monotonic() - waiter.since) from None
        except BaseException:
            # the caller went away while waiting: return a place it may have been handed
            if waiter.future.done() and not waiter.future.cancelled():
                self._slot(provider, waiter).release()
            else:
                waiter.future.cancel()
                if waiter in pool.line:
                    pool.line.remove(waiter)
            raise
        return self._slot(provider, waiter)

    def _slot(self, provider: str, waiter: _Waiter) -> Slot:
        return Slot(self, provider, waiter.team,
                    _ms_since(waiter.since) if waiter.contended else 0, waiter.future.result())

    def _release(self, provider: str, team: int) -> None:
        pool = self._pools.get(provider)
        if pool is None:
            return
        pool.give_back(team)
        pool.dispatch()

    def reset(self) -> None:
        for pool in self._pools.values():
            for task in list(pool.asking):
                task.cancel()
            if pool.retry is not None:
                pool.retry.cancel()
        self._pools.clear()


def _ms_since(t: float) -> int:
    return int((time.monotonic() - t) * 1000)


fair_share = FairShare()
"""The process-wide instance (one pool per provider)."""
