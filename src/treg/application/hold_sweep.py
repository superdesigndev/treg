"""The scheduled stale-hold sweep, `treg-worker holds reap` (money.md "The hold reaper").

The same releases as the lazy reap inside `reserve`, for every org and outside any call: each stale
hold is released in its own admitted money session, so the sweep waits behind a busy team's queue
like any other close and never holds a team's rows for longer than one release.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import money as ledger
from ..infra import money_admission
from ..infra.db import session_maker
from ..infra.money_session import money_session


@dataclass
class SweepResult:
    seen: int = 0
    released_micro: int = 0
    batches: int = 0
    complete: bool = True


async def reap_stale_holds(*, batch_size: int = 100, max_batches: int = 50) -> SweepResult:
    """Release every hold older than `ledger.hold_ttl_s()`, at most `max_batches` reads of
    `batch_size`. `complete` is False when the last read was full, so the next run continues."""
    result = SweepResult()
    while result.batches < max_batches:
        async with session_maker() as db:
            batch = await ledger.stale_holds(db, limit=batch_size)
        result.batches += 1
        for hold in batch:
            result.seen += 1
            async with (
                money_admission.admit(money_admission.require_balance_orgs([hold.org_id]),
                                      operation="release"),
                money_session(session_maker()) as db,
            ):
                result.released_micro += await ledger.release(
                    db, hold.call_id, reason="stale_hold_reaped", meta={"age_s": hold.age_s})
        if len(batch) < batch_size:
            return result
    result.complete = False
    return result
