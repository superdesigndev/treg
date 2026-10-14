"""Four runs at a time per team (docs/HUB-DECISIONS.md round 3 q9).

A run holds a request and, on the script road, a sandbox process, for up to 120 seconds; four
in flight per team bounds both. The counter is in-process, which is exact for production as
deployed (`python -m treg` runs one uvicorn process) and per-worker otherwise — stated here so a
future multi-worker deploy knows to move it to the rate store.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from typing import Iterator

MAX_RUNS_PER_TEAM = 4
RETRY_AFTER_S = 5

_active: dict[int, int] = defaultdict(int)


class TeamBusy(Exception):
    def __init__(self, org_id: int, active: int) -> None:
        super().__init__(f"team {org_id} has {active} runs in flight")
        self.org_id, self.active = org_id, active


@contextmanager
def slot(org_id: int) -> Iterator[None]:
    """Take one of the team's slots for the duration of a run; refuse with TeamBusy at the cap."""
    if _active[org_id] >= MAX_RUNS_PER_TEAM:
        raise TeamBusy(org_id, _active[org_id])
    _active[org_id] += 1
    try:
        yield
    finally:
        _active[org_id] -= 1
        if _active[org_id] <= 0:
            _active.pop(org_id, None)


def active(org_id: int) -> int:
    return _active.get(org_id, 0)
