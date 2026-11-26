"""The allowance's background work: apply queued charges to the ledger, and close the allowance
of a team that has stopped spending from it.

**Folding.** A finished call's charge waits in a queue (`CallCharge`). This loop is what applies
the queue: a team at a time, the longest-waiting first, a batch per transaction, on the
background pool. Request processes do not fold, so the connections folding uses do not grow with
traffic or with the number of busy teams. The teams are split into a fixed number of shares by
their numbers, and one process at a time runs the pass over each share (`_pass_lease`); a team
another process is working on is skipped, not waited for.

**Closing.** Each refill writes on the hold the instant until which calls may take from the team's counter, and
publishes that same instant to the counter. A call lives at most `hold_ttl_s()`. So a hold whose
instant passed that long ago has nobody spending from it, and all of it can go back to the
balance: what the counter still held, and whatever a crashed call or a lost give-back left behind.
The instant is read from the row, never recomputed from this process's settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from ..config import get_settings
from ..domain import money as ledger
from ..domain.money import allowance
from ..infra import db as infra_db
from ..infra import kv, money_admission
from ..infra.db import background_session_maker
from ..infra.money_session import bound_lock_wait, lock_timed_out, money_session
from ..infra.structured_logging import get_logger
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
_TEAMS_PER_PASS = 500
_TEAMS_LISTED = 5000
_ADMISSION_WAIT_S = 0.25
_LEASE_MS = 5000
_PASS_KEY = "treg:money-allowance-pass:v1"
_PASS_LEASE_MS = 30_000
# Skips are counted per kind of work and reason, and logged once a minute per process with the
# teams skipped most stale first (`_note_skip`); a window naming this many teams is logged early.
_SKIP_LOG_EVERY_S = 60.0
_SKIP_TEAMS_MAX = 1000
_SKIP_TOP = 5
_skips: dict[tuple[str, str], dict] = {}
_skip_log = get_logger("treg.allowance_fold_skips")


def _row_locks() -> bool:
    return not infra_db._is_sqlite


def _note_skip(kind: str, org_id: int, reason: str, oldest=None) -> None:
    """Count one team given up for the pass: `kind` is the work (`fold` or `close`), `reason`
    why (`busy_lock`: a money session of the team is open in this process, whoever holds the
    lease; `lease_held`: the lease is taken and no session of the team is open here, so another
    process has it; `admission_timeout`; `lock_timeout`; `failed`), `oldest`
    when the team's oldest queued charge was written. One `allowance_fold_skips` line per kind
    and reason per minute in this process: the skips, the teams, the longest wait, and the teams
    whose charges have waited longest. Diagnostics never stop a pass."""
    try:
        now = time.monotonic()
        window = _skips.get((kind, reason))
        if window is not None and org_id not in window["orgs"] and (
                len(window["orgs"]) >= _SKIP_TEAMS_MAX):
            _log_skips(now, kind, reason, window)
            window = None
        if window is None:
            window = _skips[(kind, reason)] = {"since": now, "skips": 0, "orgs": {}}
        window["skips"] += 1
        age_s = _waited_s(oldest)
        team = window["orgs"].setdefault(org_id, [0, None])
        team[0] += 1
        if age_s is not None:
            team[1] = max(team[1] or 0, age_s)
        _log_due_skips(now)
    except Exception:  # noqa: BLE001
        pass


def _waited_s(oldest) -> int | None:
    """Whole seconds since `oldest`, a queued charge's `created_at` as `queued_teams` read it
    (SQLite hands back text); None when there is none."""
    if isinstance(oldest, str):
        oldest = datetime.fromisoformat(oldest)
    if not isinstance(oldest, datetime):
        return None
    return max(0, int((utcnow_naive() - oldest.replace(tzinfo=None)).total_seconds()))


def _log_due_skips(now: float) -> None:
    for (kind, reason), window in list(_skips.items()):
        if now - window["since"] >= _SKIP_LOG_EVERY_S:
            _log_skips(now, kind, reason, window)


def _log_due_skips_safely() -> None:
    try:
        _log_due_skips(time.monotonic())
    except Exception:  # noqa: BLE001
        pass


def _log_skips(now: float, kind: str, reason: str, window: dict) -> None:
    _skips.pop((kind, reason), None)
    teams = sorted(window["orgs"].items(), key=lambda item: (-(item[1][1] or 0), -item[1][0],
                                                             item[0]))
    ages = [age for _, (_, age) in teams if age is not None]
    _skip_log.info("allowance_fold_skips %s", json.dumps({
        "kind": kind, "reason": reason, "skips": window["skips"], "orgs": len(teams),
        "window_s": round(now - window["since"], 1), "oldest_s": max(ages) if ages else None,
        "top": [[org_id, skips, age] for org_id, (skips, age) in teams[:_SKIP_TOP]],
    }, separators=(",", ":")))


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


def fold_shards() -> int:
    return get_settings().money_allowance_fold_shards


def pass_key(shard: allowance.Shard) -> str:
    """The lease of one share of the teams. The count is in the key, so processes running with
    different counts never take each other's lease for a different share."""
    index, count = shard
    return _PASS_KEY if count <= 1 else f"{_PASS_KEY}:{index}/{count}"


class _Pass:
    """One process's turn at a pass over one share of the teams (`shard`; None = every team).
    `rest_s` set by the holder keeps the lease after the pass for that long, so the pause after
    an idle pass is shared by every process, not taken by each in turn. `lost` is set once the
    lease is known to be gone: the pass then stops between teams, so it does not run beside the
    process that has the lease now."""

    def __init__(self, mine: bool, shard: allowance.Shard | None = None) -> None:
        self.mine = mine
        self.shard = shard
        self.rest_s = 0.0
        self.lost = False

    def holding(self) -> bool:
        return not self.lost


async def _keep_pass(store, key: str, token: str, turn: _Pass) -> None:
    """Renew the pass lease while a long pass runs, so a second process does not start one. A
    store that cannot answer is asked again sooner, within what is left of the lease; a lease
    known to be lost, or past its time, marks the turn `lost` and ends the renewal."""
    ttl_s = _PASS_LEASE_MS / 1000
    valid_until, wait_s = time.monotonic() + ttl_s, ttl_s / 3
    while True:
        await asyncio.sleep(wait_s)
        asked_at = time.monotonic()
        answer = await store.renew_lease(key, token, _PASS_LEASE_MS, deadline=valid_until)
        left_s = valid_until - time.monotonic()
        if answer == "renewed":
            valid_until, wait_s = asked_at + ttl_s, ttl_s / 3
        elif answer == "unavailable" and left_s > 0:
            wait_s = min(ttl_s / 3, max(left_s / 4, 0.005))
        else:
            turn.lost = True
            return


@asynccontextmanager
async def _pass_lease():
    """Yield this process's `_Pass`: at most one process at a time per share of the teams, and
    one share per process; a process that finds every share taken pauses.

    Every process runs the loop, but a pass reads the teams with queued charges, so many
    processes each reading them every second cost the database more than the folding itself;
    a fixed number of shares bounds that read and the connections folding uses, whatever the
    number of processes. A process tries the shares from a random one, so the shares are spread
    over the processes that wake. When the store cannot answer every process runs its pass over
    every team, as without the lease: the team leases and row locks keep two writers of one team
    apart either way."""
    store, token, count = kv.store(), uuid.uuid4().hex, fold_shards()
    first = random.randrange(count)
    acquired, shard = "busy", None
    for step in range(count):
        candidate = ((first + step) % count, count)
        acquired = await store.acquire_lease(pass_key(candidate), token, _PASS_LEASE_MS)
        if acquired == "acquired":
            shard = candidate
            break
        if acquired == "unavailable":
            break
    turn = _Pass(acquired != "busy", shard)
    key = pass_key(shard) if shard is not None else None
    keeper = (asyncio.create_task(_keep_pass(store, key, token, turn))
              if key is not None else None)
    try:
        yield turn
    finally:
        try:
            if keeper is not None:
                keeper.cancel()
                # gather hands back the keeper's own cancellation as a result; a cancellation of
                # this task while it waits still propagates, after the lease is given back.
                await asyncio.gather(keeper, return_exceptions=True)
        finally:
            if key is not None:
                if turn.rest_s > 0 and not turn.lost:
                    await store.renew_lease(key, token, max(1, int(turn.rest_s * 1000)))
                else:
                    await store.release_lease(key, token)


def idle_after_s() -> int:
    """How long after an allowance hold stopped admitting calls it is known to be unused: the
    longest a call admitted at the last moment can live, plus the clocks' margin."""
    return ledger.hold_ttl_s() + _SLACK_S


def _always() -> bool:
    return True


async def sweep_once(session_maker, *, keep_going: Callable[[], bool] = _always,
                     shard: allowance.Shard | None = None) -> int:
    """Close every allowance hold idle for `idle_after_s()` (of the teams in `shard`, or every
    team), a page of teams at a time, so a team is reached however many before it cannot be
    closed yet; stops between teams once `keep_going` says so. Returns how many were closed."""
    stale_before = utcnow_naive() - timedelta(seconds=idle_after_s())
    closed, after = 0, 0
    while True:
        async with session_maker() as db:
            org_ids = await allowance.stale_allowance_orgs(
                db, stale_before, after=after, limit=_TEAMS_PER_PAGE, shard=shard)
        for org_id in org_ids:
            if not keep_going():
                return closed
            closed += await _close_idle(session_maker, org_id, stale_before)
        if len(org_ids) < _TEAMS_PER_PAGE:
            return closed
        after = org_ids[-1]


async def _close_idle(session_maker, org_id: int, stale_before) -> bool:
    """Close one team's idle hold. False when another process has the team, its money sessions
    are busy (here or in another process), the hold is no longer idle, or on a fault: the next
    sweep comes back."""
    try:
        async with _team_lease(org_id) as free:
            lock = money_lock(org_id)
            if lock.locked() or not free:
                _note_skip("close", org_id, "busy_lock" if lock.locked() else "lease_held")
                return False
            async with (
                lock,
                money_admission.admit(
                    money_admission.require_balance_orgs([org_id]), operation="release",
                    max_wait_s=_ADMISSION_WAIT_S, refuse_on_timeout=True),
                money_session(session_maker()) as db,
            ):
                await bound_lock_wait(db)
                result = await allowance.close_stale_allowance_in_transaction(
                    db, org_id, stale_before=stale_before, reason="idle")
                await db.commit()
        if result is None:
            return False
        # After the commit: the hold is gone, so its counter must not be added to again. The
        # counter stopped admitting takes long ago; this only refuses a late writer.
        await kv.store().allowance_retire(org_id, result.epoch, _FENCE_TTL_MS)
        log.info("allowance_closed org_id=%s released=%s folded=%s",
                 org_id, result.released_micro, result.folded.calls)
        return True
    except money_admission.AdmissionTimeout:
        _note_skip("close", org_id, "admission_timeout")
        return False
    except Exception as exc:
        if lock_timed_out(exc):
            _note_skip("close", org_id, "lock_timeout")
        else:
            log.error("closing the idle allowance of org %s failed", org_id, exc_info=True)
            _note_skip("close", org_id, "failed")
        return False


async def fold_queued_once(session_maker, *, keep_going: Callable[[], bool] = _always,
                           shard: allowance.Shard | None = None) -> int:
    """Fold the queue of every team (in `shard`, or every team) that has charges queued, listed
    or not: a queue outlives its team's listing and its hold. The team whose oldest charge has
    waited longest goes first, so the longest wait is what a pass brings down. A pass works on
    at most `_TEAMS_PER_PASS` teams; a team it folded nothing of (another process or a session
    of the team's own is on it, or its rows stayed locked) does not count, so teams stuck at the
    front never use up a pass and the teams behind them are still reached. The next pass, which follows at once, comes
    back for the rest. The teams are read `_TEAMS_LISTED` at a time, and a pass reads on while it
    has places left, so a full list of teams it cannot have does not end it either. Stops between
    teams once `keep_going` says so. Returns how many charges
    were folded."""
    total, worked, after, queued_before = 0, 0, None, utcnow_naive()
    while True:
        async with session_maker() as db:
            page = await allowance.queued_teams(
                db, queued_before, limit=_TEAMS_LISTED, shard=shard, after=after)
        for org_id, oldest in page:
            if worked >= _TEAMS_PER_PASS or not keep_going():
                _log_due_skips(time.monotonic())
                return total
            folded = await _fold_team(session_maker, org_id, oldest)
            if folded is not None:
                worked += 1
                total += folded
        if len(page) < _TEAMS_LISTED:
            _log_due_skips(time.monotonic())
            return total
        after = page[-1]


async def _fold_team(session_maker, org_id: int, oldest=None) -> int | None:
    """A few batches of one team's queue; None when not one batch was committed. Stops when
    another process has the team, the team's money sessions are busy (a request in this process
    holding its lock among them), or its rows stay locked past `LOCK_WAIT_MS`, and the next
    pass comes back: the teams behind it are not kept waiting. Every such stop is counted
    (`_note_skip`) with when the team's `oldest` queued charge was written. One team failing
    does not stop the pass."""
    total, committed, skipped = 0, False, None
    try:
        for _ in range(_FOLD_BATCHES_PER_PASS):
            async with _team_lease(org_id) as free:
                lock = money_lock(org_id)
                if lock.locked() or not free:
                    skipped = "busy_lock" if lock.locked() else "lease_held"
                    break
                async with (
                    lock,
                    money_admission.admit(
                        money_admission.require_balance_orgs([org_id]), operation="close",
                        max_wait_s=_ADMISSION_WAIT_S, refuse_on_timeout=True),
                    money_session(session_maker()) as db,
                ):
                    await bound_lock_wait(db)
                    folded = await allowance.fold_charges_in_transaction(
                        db, org_id, limit=_FOLD_BATCH)
                    await db.commit()
                    committed = True
            total += folded.calls
            if folded.claimed < _FOLD_BATCH:
                break
    except money_admission.AdmissionTimeout:
        skipped = "admission_timeout"
    except Exception as exc:
        if lock_timed_out(exc):
            skipped = "lock_timeout"
        else:
            skipped = "failed"
            log.error("folding the queued charges of org %s failed", org_id, exc_info=True)
    if skipped is not None:
        _note_skip("fold", org_id, skipped, oldest)
    return total if committed else None


async def run_once(session_maker, *, sweep: bool,
                   keep_going: Callable[[], bool] = _always,
                   shard: allowance.Shard | None = None) -> int:
    """One pass over `shard` (or every team): the idle sweep when it is due, then the fold, one
    after the other, so never more than one connection. Returns how many charges were folded."""
    if sweep:
        await sweep_once(session_maker, keep_going=keep_going, shard=shard)
    return await fold_queued_once(session_maker, keep_going=keep_going, shard=shard)


async def _sweep_due(shard: allowance.Shard | None, every_s: float,
                     next_sweep: dict[allowance.Shard | None, float]) -> bool:
    """Whether the share's idle sweep is due, on a clock every process shares: a lease taken for
    the sweep period and never given back, so a share handed to another process is not swept
    again within the period. A store that cannot answer, or a pass over every team, falls back to
    this process's own clock."""
    if shard is not None:
        answer = await kv.store().acquire_lease(
            f"{pass_key(shard)}:sweep", uuid.uuid4().hex, max(1, int(every_s * 1000)))
        if answer != "unavailable":
            return answer == "acquired"
    if time.monotonic() < next_sweep.get(shard, 0.0):
        return False
    next_sweep[shard] = time.monotonic() + every_s
    return True


async def worker(*, fold_every_s: float = _FOLD_EVERY_S,
                 sweep_every_s: float = _SWEEP_EVERY_S) -> None:
    """Fold and sweep forever on the background pool, one process at a time per share of the
    teams (`_pass_lease`). A pass that folded something is followed by the next at once. An idle
    share pauses for every process (the holder keeps its lease through the pause), but the
    process goes on to another share: it pauses only once every share is taken or pausing, or
    when it ran over every team because the store could not answer. Each share's idle sweep is
    due on a clock every process shares (`_sweep_due`). Skips already counted are logged when
    due on every turn, so a process that no longer gets a share still writes them. A bad pass
    never kills the loop."""
    next_sweep: dict[allowance.Shard | None, float] = {}
    while True:
        _log_due_skips_safely()
        folded, pause = 0, True
        try:
            async with _pass_lease() as turn:
                if turn.mine:
                    sweep = await _sweep_due(turn.shard, sweep_every_s, next_sweep)
                    folded = await run_once(background_session_maker, sweep=sweep,
                                            keep_going=turn.holding, shard=turn.shard)
                    if not folded:
                        turn.rest_s = fold_every_s
                    pause = not folded and turn.shard is None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("allowance background pass failed", exc_info=True)
        if pause and not folded:
            await asyncio.sleep(fold_every_s)
