"""The allowance's background work: apply queued charges to the ledger, and close the allowance
of a team that has stopped spending from it.

**Folding.** A finished call's charge waits in a queue (`CallCharge`). This loop is what applies
the queue: a team at a time, a batch per transaction, on the background pool. Request processes
do not fold, so the connections folding uses do not grow with traffic or with the number of
busy teams: one per process running this loop. A team another process is working on is skipped,
not waited for.

**Closing.** Each refill writes on the hold the instant until which calls may take from the team's counter, and
publishes that same instant to the counter. A call lives at most `hold_ttl_s()`. So a hold whose
instant passed that long ago has nobody spending from it, and all of it can go back to the
balance: what the counter still held, and whatever a crashed call or a lost give-back left behind.
The instant is read from the row, never recomputed from this process's settings.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from ..domain import money as ledger
from ..domain.money import allowance
from ..infra import db as infra_db
from ..infra import kv, money_admission
from ..infra.db import background_session_maker
from ..infra.money_session import money_session
from .call.allowance import money_key, money_lock
from ..timeutil import utcnow_naive

log = logging.getLogger("treg.money_allowance")

# Clock differences between processes and a refill slow to reach the counter.
_SLACK_S = 30
_SWEEP_EVERY_S = 60.0
_FOLD_EVERY_S = 1.0
_FENCE_TTL_MS = 24 * 3600 * 1000
# A batch is one transaction under the team's lease, which a refill may be waiting for: short
# batches. A pass moves on to the next team after a few, so one long queue does not keep the
# others waiting; the next pass comes back to it.
_FOLD_BATCH = 100
_FOLD_BATCHES_PER_PASS = 10
_TEAMS_PER_PAGE = 50
_ADMISSION_WAIT_S = 0.25
_LEASE_MS = 5000


def _row_locks() -> bool:
    return not infra_db._is_sqlite


@asynccontextmanager
async def _team_lease(org_id: int):
    """Hold the team's allowance money lease; yield whether this pass may write the team's money.

    Not while another process has the lease. When the store cannot answer there is no lease to
    have: PostgreSQL still makes two writers wait on the hold's row lock, so the sweep goes on
    there; SQLite has no row locks, and the write waits for a pass that can take the lease."""
    store, key, token = kv.store(), money_key(org_id), uuid.uuid4().hex
    acquired = await store.acquire_lease(key, token, _LEASE_MS)
    try:
        yield acquired == "acquired" or (acquired == "unavailable" and _row_locks())
    finally:
        if acquired == "acquired":
            await store.release_lease(key, token)


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
            async with _team_lease(org_id) as free:
                if not free:
                    continue
                async with (
                    money_lock(org_id),
                    money_admission.admit(
                        money_admission.require_balance_orgs([org_id]), operation="release",
                        max_wait_s=_ADMISSION_WAIT_S, refuse_on_timeout=True),
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
        except money_admission.AdmissionTimeout:
            continue  # the team's money sessions are busy: the next sweep comes back
        except Exception:
            log.error("closing the idle allowance of org %s failed", org_id, exc_info=True)
    return closed


async def fold_queued_once(session_maker) -> int:
    """Fold the queue of every team that has charges queued, listed or not: a queue outlives
    its team's listing and its hold. A pass goes through all of them, a page of teams at a
    time, so a team is reached however many others keep queueing. Returns how many charges
    were folded."""
    total, after, queued_before = 0, 0, utcnow_naive()
    while True:
        async with session_maker() as db:
            org_ids = await allowance.orgs_with_queued_charges(
                db, queued_before, after=after, limit=_TEAMS_PER_PAGE)
        for org_id in org_ids:
            total += await _fold_team(session_maker, org_id)
        if len(org_ids) < _TEAMS_PER_PAGE:
            return total
        after = org_ids[-1]


async def _fold_team(session_maker, org_id: int) -> int:
    """A few batches of one team's queue. Stops when another process has the team or the team's
    money sessions are busy, and the next pass comes back: the teams behind it are not kept
    waiting. One team failing does not stop the pass."""
    total = 0
    try:
        for _ in range(_FOLD_BATCHES_PER_PASS):
            async with _team_lease(org_id) as free:
                if not free:
                    break
                async with (
                    money_lock(org_id),
                    money_admission.admit(
                        money_admission.require_balance_orgs([org_id]), operation="close",
                        max_wait_s=_ADMISSION_WAIT_S, refuse_on_timeout=True),
                    money_session(session_maker()) as db,
                ):
                    folded = await allowance.fold_charges_in_transaction(
                        db, org_id, limit=_FOLD_BATCH)
                    await db.commit()
            total += folded.calls
            if folded.claimed < _FOLD_BATCH:
                break
    except money_admission.AdmissionTimeout:
        pass
    except Exception:
        log.error("folding the queued charges of org %s failed", org_id, exc_info=True)
    return total


async def run_once(session_maker, *, sweep: bool) -> int:
    """One pass: the idle sweep when it is due, then the fold, one after the other, so never
    more than one connection. Returns how many charges were folded."""
    if sweep:
        await sweep_once(session_maker)
    return await fold_queued_once(session_maker)


async def worker(*, fold_every_s: float = _FOLD_EVERY_S,
                 sweep_every_s: float = _SWEEP_EVERY_S) -> None:
    """Fold and sweep forever on the background pool. A pass that folded something is followed
    by the next at once; an idle one by a pause. A bad pass never kills the loop."""
    next_sweep = 0.0
    while True:
        folded = 0
        sweep = time.monotonic() >= next_sweep
        if sweep:
            next_sweep = time.monotonic() + sweep_every_s
        try:
            folded = await run_once(background_session_maker, sweep=sweep)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("allowance background pass failed", exc_info=True)
        if not folded:
            await asyncio.sleep(fold_every_s)
