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

In-process and DB-free on purpose, like the rate limiter (infra/upstream/limiter.py): it runs
after the DB phase ended and before the relay. Exact for one server process; several processes
each enforce `cap` on their own, so a multi-process deploy moves the counters to the shared store.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

DEFAULT_MAX_WAIT_S = 2.0
RETRY_AFTER_S = 2


class ShareBusy(Exception):
    """No place freed for this call within the wait; `active` is its team's calls in progress."""

    def __init__(self, provider: str, team: int, active: int, waited_s: float) -> None:
        super().__init__(f"{provider}: team {team} waited {waited_s:.1f} s with {active} calls in progress")
        self.provider, self.team, self.active, self.waited_s = provider, team, active, waited_s


@dataclass
class _Waiter:
    team: int
    future: asyncio.Future
    since: float = field(default_factory=time.monotonic)


@dataclass
class _Pool:
    cap: int
    active: int = 0
    by_team: dict[int, int] = field(default_factory=dict)
    line: list[_Waiter] = field(default_factory=list)

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

    def dispatch(self) -> None:
        """Hand every free place to the waiting call whose team has the fewest in progress."""
        while self.active < self.cap and self.line:
            best = min(range(len(self.line)),
                       key=lambda i: (self.by_team.get(self.line[i].team, 0), self.line[i].since))
            w = self.line.pop(best)
            if w.future.done():  # cancelled while it waited; its place goes to the next one
                continue
            self.take(w.team)
            w.future.set_result(None)


class Slot:
    """One place held by one call. `release()` is idempotent; every call path releases."""

    __slots__ = ("_gate", "_provider", "_team", "waited_ms")

    def __init__(self, gate: "FairShare | None", provider: str = "", team: int = 0,
                 waited_ms: int = 0) -> None:
        self._gate, self._provider, self._team, self.waited_ms = gate, provider, team, waited_ms

    def release(self) -> None:
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
            pool = self._pools[provider] = _Pool(cap)
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
        if pool.active < pool.cap and not pool.line:
            pool.take(team)
            return Slot(self, provider, team)
        waiter = _Waiter(team, asyncio.get_running_loop().create_future())
        pool.line.append(waiter)
        pool.dispatch()  # the newcomer may outrank a heavier team for a place that is free now
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), timeout=max_wait_s)
        except asyncio.TimeoutError:
            if waiter.future.done() and not waiter.future.cancelled():
                # handed a place in the same instant the wait ran out: keep it
                return Slot(self, provider, team, _ms_since(waiter.since))
            waiter.future.cancel()
            if waiter in pool.line:
                pool.line.remove(waiter)
            raise ShareBusy(provider, team, pool.by_team.get(team, 0),
                            time.monotonic() - waiter.since) from None
        except BaseException:
            # the caller went away while waiting: return a place it may have been handed
            if waiter.future.done() and not waiter.future.cancelled():
                self._release(provider, team)
            else:
                waiter.future.cancel()
                if waiter in pool.line:
                    pool.line.remove(waiter)
            raise
        return Slot(self, provider, team, _ms_since(waiter.since))

    def _release(self, provider: str, team: int) -> None:
        pool = self._pools.get(provider)
        if pool is None:
            return
        pool.give_back(team)
        pool.dispatch()

    def reset(self) -> None:
        self._pools.clear()


def _ms_since(t: float) -> int:
    return int((time.monotonic() - t) * 1000)


fair_share = FairShare()
"""The process-wide instance (one pool per provider)."""
