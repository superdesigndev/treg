"""Close the allowance of a team that has stopped spending from it.

Each refill writes on the hold the instant until which calls may take from the team's counter, and
publishes that same instant to the counter. A call lives at most `hold_ttl_s()`. So a hold whose
instant passed that long ago has nobody spending from it, and all of it can go back to the
balance: what the counter still held, and whatever a crashed call or a lost give-back left behind.
The instant is read from the row, never recomputed from this process's settings.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from ..domain import money as ledger
from ..domain.money import allowance
from ..infra import kv, money_admission
from ..infra.db import background_session_maker
from ..infra.money_session import money_session
from .call.allowance import money_lock
from ..timeutil import utcnow_naive

log = logging.getLogger("treg.money_allowance")

# Clock differences between processes and a refill slow to reach the counter.
_SLACK_S = 30
_SWEEP_EVERY_S = 60.0
_FENCE_TTL_MS = 24 * 3600 * 1000
_FOLD_BATCHES_PER_PASS = 20


def idle_after_s() -> int:
    """How long after an allowance hold stopped admitting calls it is known to be unused: the
    longest a call admitted at the last moment can live, plus the clocks' margin."""
    return ledger.hold_ttl_s() + _SLACK_S


async def sweep_once(session_maker) -> int:
    """Close every allowance hold idle for `idle_after_s()`. Returns how many were closed."""
    stale_before = utcnow_naive() - timedelta(seconds=idle_after_s())
    async with session_maker() as db:
        org_ids = await allowance.stale_allowance_orgs(db, stale_before)
    closed = 0
    for org_id in org_ids:
        try:
            async with (
                money_lock(org_id),
                money_admission.admit(
                    money_admission.require_balance_orgs([org_id]), operation="release"),
                money_session(session_maker()) as db,
            ):
                result = await allowance.close_stale_allowance_in_transaction(
                    db, org_id, stale_before=stale_before, reason="idle")
                await db.commit()
            if result is None:
                continue
            closed += 1
            # After the commit: the hold is gone, so its counter must not be added to again. The
            # counter stopped admitting takes long ago; this only refuses a late writer.
            await kv.store().allowance_retire(org_id, result.epoch, _FENCE_TTL_MS)
            log.info("allowance_closed org_id=%s released=%s folded=%s",
                     org_id, result.released_micro, result.folded.calls)
        except Exception:
            log.error("closing the idle allowance of org %s failed", org_id, exc_info=True)
    return closed


async def fold_queued_once(session_maker) -> int:
    """Fold the queue of every team that has a charge older than a sweep interval. A busy
    team's refills fold its queue; this is for a queue nothing else is folding: a team no longer
    listed, a charge written after its hold was closed. Returns how many charges were folded."""
    queued_before = utcnow_naive() - timedelta(seconds=_SWEEP_EVERY_S)
    async with session_maker() as db:
        org_ids = await allowance.orgs_with_queued_charges(db, queued_before)
    total = 0
    for org_id in org_ids:
        try:
            for _ in range(_FOLD_BATCHES_PER_PASS):
                async with (
                    money_lock(org_id),
                    money_admission.admit(
                        money_admission.require_balance_orgs([org_id]), operation="close"),
                    money_session(session_maker()) as db,
                ):
                    folded = await allowance.fold_charges_in_transaction(db, org_id)
                    await db.commit()
                total += folded.calls
                if folded.calls + folded.duplicates < allowance.FOLD_BATCH:
                    break
        except Exception:
            log.error("folding the queued charges of org %s failed", org_id, exc_info=True)
    return total


async def run_once(session_maker) -> None:
    """One pass of both jobs, one after the other: never more than one connection."""
    await sweep_once(session_maker)
    await fold_queued_once(session_maker)


async def worker(*, every_s: float = _SWEEP_EVERY_S) -> None:
    """Sweep forever on the background pool, one connection at a time. A bad pass never kills
    the loop."""
    while True:
        try:
            await run_once(background_session_maker)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("allowance sweep pass failed", exc_info=True)
        await asyncio.sleep(every_s)
