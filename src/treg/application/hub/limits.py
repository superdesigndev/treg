"""Hub runs in flight, per team and per tool.

A run holds a request and, on the script road, a sandbox, for up to its wall clock. Two caps bound
that: `MAX_RUNS_PER_TEAM` for one team, and `hub_tool_max_runs` for one tool across every team, so
one tool cannot spend a shared upstream quota for everyone. With the shared store configured both
are counted across every server process on renewed leases (`infra/provider_concurrency.py`), so a
crashed process frees its places within one lease. Without a store, or when it does not answer,
the cap falls back to this process's own counter: still a bound, per process.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

from ...config import get_settings
from ...infra import provider_concurrency

MAX_RUNS_PER_TEAM = 4
RETRY_AFTER_S = 5

Scope = Literal["team", "tool"]
_active: dict[tuple[Scope, str], int] = defaultdict(int)


class HubBusy(Exception):
    def __init__(self, scope: Scope, cap: int) -> None:
        super().__init__(f"the {scope} already has {cap} hub runs in flight")
        self.scope, self.cap = scope, cap

    @property
    def message(self) -> str:
        if self.scope == "team":
            return (f"your team already has {self.cap} hub runs in flight; "
                    f"try again in {RETRY_AFTER_S} s")
        return f"this tool already has {self.cap} runs in flight; try again in {RETRY_AFTER_S} s"

    def detail(self) -> dict:
        return {"error": "hub_busy", "scope": self.scope, "max": self.cap,
                "retry_after_s": RETRY_AFTER_S, "message": self.message}


class _Place:
    """One admitted run, counted in this process whichever store admitted it, so a fallback after
    a store outage still sees the runs the shared store let in."""

    def __init__(self, key: tuple[Scope, str], lease: provider_concurrency.Lease | None) -> None:
        self.key, self.lease = key, lease
        _active[key] += 1

    def release(self) -> None:
        if self.lease is not None:
            self.lease.release()
        _active[self.key] -= 1
        if _active[self.key] <= 0:
            _active.pop(self.key, None)


async def _take(scope: Scope, ident: str, cap: int) -> _Place:
    key = (scope, ident)
    if provider_concurrency.enabled():
        held = await provider_concurrency.try_acquire(ident, cap, namespace=f"hub-{scope}")
        if held == provider_concurrency.BUSY:
            raise HubBusy(scope, cap)
        if held is not None:
            return _Place(key, held)
    if _active.get(key, 0) >= cap:
        raise HubBusy(scope, cap)
    return _Place(key, None)


@asynccontextmanager
async def slot(org_id: int, tool_id: str) -> AsyncIterator[None]:
    """Hold one of the team's places and one of the tool's for a run; HubBusy at either cap."""
    held = []
    try:
        for scope, ident, cap in (("team", str(org_id), MAX_RUNS_PER_TEAM),
                                  ("tool", tool_id, get_settings().hub_tool_max_runs)):
            if cap > 0:
                held.append(await _take(scope, ident, cap))
        yield
    finally:
        for place in held:
            place.release()


def active(org_id: int) -> int:
    """This process's own count of a team's runs, shared admissions included."""
    return _active.get(("team", str(org_id)), 0)
