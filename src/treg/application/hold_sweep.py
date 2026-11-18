"""The scheduled stale-hold sweep, `treg-worker holds reap` (money.md "The hold reaper").

The same releases as the lazy reap inside `reserve`, for every org and outside any call: each stale
hold is released in its own admitted money session, so the sweep waits behind a busy team's queue
like any other close and never holds a team's rows for longer than one release. A hold whose release
raises is counted, logged at a limited rate and passed over, so it never keeps another team's holds.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..domain import money as ledger
from ..infra import money_admission
from ..infra.db import session_maker
from ..infra.money_session import money_session

log = logging.getLogger("treg.ledger")
_FAILURE_LOG_INTERVAL_S = 60.0
_failure_logged_at: float | None = None


@dataclass
class SweepResult:
    seen: int = 0
    released_micro: int = 0
    failed: int = 0
    batches: int = 0
    complete: bool = True


def _log_failure(hold: ledger.StaleHold, exc: Exception, failed: int) -> None:
    global _failure_logged_at
    now = time.monotonic()
    if _failure_logged_at is not None and now - _failure_logged_at < _FAILURE_LOG_INTERVAL_S:
        return
    _failure_logged_at = now
    log.warning("stale hold release failed for hold %s (org %s): %s: %s; %s failed in this run",
                hold.call_id, hold.org_id, type(exc).__name__, exc, failed)


async def _release(hold: ledger.StaleHold) -> int:
    async with (
        money_admission.admit(money_admission.require_balance_orgs([hold.org_id]),
                              operation="release"),
        money_session(session_maker()) as db,
    ):
        return await ledger.release(
            db, hold.call_id, reason="stale_hold_reaped", meta={"age_s": hold.age_s})


async def reap_stale_holds(*, batch_size: int = 100, max_batches: int = 50) -> SweepResult:
    """Release every hold older than `ledger.hold_ttl_s()`, at most `max_batches` reads of
    `batch_size`. Each read starts past the last hold the previous one returned, so a hold another
    close already claimed, one `release` skips, or one whose release raises never stalls the sweep.
    `complete` is False when the last read was full, so the next run continues."""
    result = SweepResult()
    after = None
    while result.batches < max_batches:
        async with session_maker() as db:
            batch = await ledger.stale_holds(db, limit=batch_size, after=after)
        result.batches += 1
        if batch:
            after = (batch[-1].created_at, batch[-1].call_id)
        for hold in batch:
            result.seen += 1
            try:
                result.released_micro += await _release(hold)
            except Exception as exc:  # noqa: BLE001 - one hold must not stop every other team's
                result.failed += 1
                _log_failure(hold, exc, result.failed)
        if len(batch) < batch_size:
            return result
    result.complete = False
    return result
