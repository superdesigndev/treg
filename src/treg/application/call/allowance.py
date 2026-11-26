"""Spending a metered call from the team's allowance instead of a hold of its own.

The allowance is one database hold per team (`domain/money/allowance.py`), mirrored as a counter in
the shared store. A call takes its reservation from the counter in one atomic step and, when it
ends, gives back what it did not use and queues its charge. The balance row is touched only by a
refill. Every failure here returns the call to the exact path or leaves money in the hold; none
spends money the hold does not have.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlmodel import select

from ... import archive
from ...config import get_settings
from ...domain import money as ledger
from ...domain.governance import budgets as budget_policy
from ...domain.money import allowance
from ...infra import db as infra_db
from ...infra import kv, money_admission
from ...infra.db import session_maker
from ...infra.money_session import bound_lock_wait, lock_timed_out, money_session
from ...models import Org
from .idempotency import IN_FLIGHT_MARK_MS

log = logging.getLogger("treg.money_allowance")

# A refill that could not make room is not retried per call: the team stays on the exact path.
_REFUSAL_COOLDOWN_S = 5.0
_REFILL_LEASE_MS = 5000
# How long a call waits for a refill another caller is doing before it takes its own hold. A
# refill is a short transaction; the exact path it would join instead is one call at a time.
_REFILL_WAIT_S = 1.0
# How long a refill asked for ahead of the deadline keeps trying for the team's lease.
_REFRESH_WAIT_S = 10.0
# One batch of the fold a frozen call asks for (`_settled`). A transaction that keeps the
# team's lease: kept short, so a refill never waits long behind it.
_BACKLOG_FOLD = 100
# The most batches that fold takes before leaving the rest of the queue to the background fold.
_BACKLOG_BATCHES = 5
# How long a call waits in all for its settle entry before answering with its charge.
_SETTLED_WAIT_S = 1.0
# The end of that wait kept for one last read of the entry, whatever the folding did with the
# rest (a fifth of it at most).
_LAST_READ_S = 0.2
# Long enough that a counter outlives any pause in refills that still leaves its hold open.
_COUNTER_TTL_MS = 24 * 3600 * 1000
_refused_until: dict[int, float] = {}
# Takes this process admitted and has not finished. Each will hand the charge writer one row at
# most, so admitting against the writer's room now is what keeps it from ever overflowing.
_in_flight = [0]
# The most charges this process may owe the database at once, in flight or waiting to be written.
_MAX_OWED = 20_000
# How often the charge writer renews the in-flight marks it holds: well inside their lifetime.
_MARK_RENEW_S = 60.0
_money_locks: dict[tuple[int, asyncio.AbstractEventLoop], asyncio.Lock] = {}


def money_lock(org_id: int) -> asyncio.Lock:
    """One allowance money session per team at a time in this process. PostgreSQL would make
    them wait on the hold's row lock anyway; SQLite has no row locks, and there two sessions
    that both read the hold before either wrote would lose one of the writes."""
    key = (org_id, asyncio.get_running_loop())
    lock = _money_locks.get(key)
    if lock is None:
        _prune_if_full()
        lock = _money_locks[key] = asyncio.Lock()
    return lock


_TEAM_STATE_PRUNE_AT = 1024
_next_prune_at = 0


def _team_state_size() -> int:
    return max(len(_money_locks), len(_refills), len(_refused_until))


def _prune_if_full() -> None:
    global _next_prune_at
    if _team_state_size() < max(_TEAM_STATE_PRUNE_AT, _next_prune_at):
        return
    _prune()
    _next_prune_at = 2 * _team_state_size()


def _prune() -> None:
    now = time.monotonic()
    for org_id in [org_id for org_id, until in _refused_until.items() if until <= now]:
        del _refused_until[org_id]
    # asyncio has no public count of a lock's waiters.
    for key in [key for key, lock in _money_locks.items()
                if not lock.locked() and not lock._waiters]:
        del _money_locks[key]
    busy = ({org_id for org_id, _ in _money_locks} | set(_refreshing) | set(_refused_until)
            | set(_taking))
    for org_id in [org_id for org_id in _refills if org_id not in busy]:
        del _refills[org_id]
_refills: dict[int, int] = {}
_taking: dict[int, int] = {}


def refills(org_id: int) -> int:
    """How many refills this process has committed for the team: a caller compares it around a
    take to learn that the balance moved."""
    return _refills.get(org_id, 0)


def listed(org_id: int) -> bool:
    """Whether the operator put the team on the allowance: every team when
    `money_allowance_all_orgs`, else the ones in `money_allowance_org_ids`, never an excluded
    one."""
    settings = get_settings()
    if org_id in settings.money_allowance_excluded_org_ids:
        return False
    return settings.money_allowance_all_orgs or org_id in settings.money_allowance_org_ids


def routed_listed(org_id: int) -> bool:
    """Whether a routed call of the team lets its children take from the allowance: a team on
    the allowance, named in `money_allowance_routed_org_ids` or every one under
    `money_allowance_routed_all_orgs`."""
    settings = get_settings()
    return listed(org_id) and (settings.money_allowance_routed_all_orgs
                               or org_id in settings.money_allowance_routed_org_ids)


def tagged_listed(org_id: int) -> bool:
    """Whether a tagged call of the team no tag budget limits may take from the allowance: a
    team on the allowance, named in `money_allowance_tagged_org_ids` or every one under
    `money_allowance_tagged_all_orgs`."""
    settings = get_settings()
    return listed(org_id) and (settings.money_allowance_tagged_all_orgs
                               or org_id in settings.money_allowance_tagged_org_ids)


def enabled_for(org_id: int) -> bool:
    return listed(org_id) and kv.configured() and _database_supported()


def _database_supported() -> bool:
    """The allowance runs on PostgreSQL only. It leans on row locks to order a refill, a fold, a
    charge write and a team's deletion, and on a team's number never being given to another
    team; SQLite offers neither."""
    return not infra_db._is_sqlite


def money_key(org_id: int) -> str:
    """The lease every allowance money session of a team holds: a refill, a drain, a fold, the
    sweep's close. PostgreSQL would make them wait on the hold's row lock anyway. SQLite has no
    row locks, so there this lease is what stops two processes from both reading the hold
    before either writes."""
    return f"treg:money-allowance-money:v1:{org_id}"


def fold_claim_key(org_id: int) -> str:
    """The claim the background fold sets on a team whose queue has waited too long
    (`allowance_sweep`): while another token holds it, nobody else gets the team's lease."""
    return f"treg:money-allowance-fold-claim:v1:{org_id}"


async def acquire_money_lease(store, org_id: int, token: str, ttl_ms: int) -> str:
    """Take the team's lease (`money_key`), unless the background fold has claimed its next turn
    (`fold_claim_key`) with another token: then `busy`, as if held."""
    return await store.acquire_lease(money_key(org_id), token, ttl_ms,
                                     unless=fold_claim_key(org_id))


async def take(org_id: int, amount_micro: int, *, daily_cap_micro: int = 0,
               after_refill: Callable[[], Awaitable[None]] | None = None) -> int | None:
    """Reserve `amount_micro` from the team's allowance. The counter's epoch, or None when the
    call must use the exact path: the store cannot answer, another process holds the refill for
    too long, or the balance or the daily cap leaves no room to refill.

    A refill this call starts beside itself commits after this returns, so `refills` around the
    call does not show it: `after_refill` is awaited once that refill has moved the balance."""
    if (_refused_until.get(org_id, 0.0) > time.monotonic() or _charges.behind()
            or org_id in _freezes_due or not _charges.has_room(_in_flight[0])):
        return None
    # The place is taken before anything is awaited, so calls arriving together cannot all pass
    # on the last one. `finish` gives it back, or hands it to the charge writer.
    _in_flight[0] += 1
    _taking[org_id] = _taking.get(org_id, 0) + 1
    epoch = None
    try:
        epoch = await _take(org_id, amount_micro, daily_cap_micro, after_refill)
    finally:
        if not epoch:
            _in_flight[0] -= 1
        _taking[org_id] -= 1
        if not _taking[org_id]:
            del _taking[org_id]
    return epoch


def _ahead() -> dict:
    """When a take should ask its caller to refill: half the counter's lifetime before its
    deadline, or with a quarter of a chunk left."""
    settings = get_settings()
    return {"ahead_ms": settings.money_allowance_fresh_s * 500,
            "low_micro": settings.money_allowance_chunk_micro // 4}


def _taken(org_id: int, answer: int | None, daily_cap_micro: int, after_refill) -> int | None:
    """The epoch a take answered. A negated one also asks this caller for a refill, which runs
    beside the call: the call has its money."""
    if answer is not None and answer < 0:
        _refresh_in_background(org_id, daily_cap_micro, after_refill)
        return -answer
    return answer


async def _take(org_id: int, amount_micro: int, daily_cap_micro: int, after_refill=None) -> int | None:
    store = kv.store()
    epoch = _taken(org_id, await store.allowance_take(org_id, amount_micro, **_ahead()),
                   daily_cap_micro, after_refill)
    if epoch or epoch is None:
        return epoch
    key, token = money_key(org_id), uuid.uuid4().hex
    deadline = time.monotonic() + _REFILL_WAIT_S
    while True:
        acquired = await acquire_money_lease(store, org_id, token, _REFILL_LEASE_MS)
        if acquired == "unavailable":
            return None
        if acquired == "acquired":
            try:
                refilled = await _refill(org_id, amount_micro, daily_cap_micro)
            except Exception:
                log.error("allowance refill failed for org %s", org_id, exc_info=True)
                refilled = False
            finally:
                await store.release_lease(key, token)
            if not refilled:
                return None
            return _taken(org_id, await store.allowance_take(org_id, amount_micro, **_ahead()),
                          daily_cap_micro, after_refill) or None
        # Another process holds the lease: for a refill, whose add is what this call is waiting
        # for, or for a fold, after which this call refills.
        await asyncio.sleep(0.01 + random.random() * 0.02)
        epoch = _taken(org_id, await store.allowance_take(org_id, amount_micro, **_ahead()),
                       daily_cap_micro, after_refill)
        if (epoch or epoch is None or time.monotonic() > deadline
                or _refused_until.get(org_id, 0.0) > time.monotonic()):
            return epoch or None


_refreshing: dict[int, asyncio.Task] = {}
# An ask that arrived while the team's refill was running, for that refill to act on when it is
# done: the running one may already have published what it added.
_refresh_again: dict[int, tuple] = {}
_after_refills: set[asyncio.Task] = set()


def _refresh_in_background(org_id: int, daily_cap_micro: int, after_refill) -> None:
    task = _refreshing.get(org_id)
    loop = asyncio.get_running_loop()
    if task is None or task.done() or task.get_loop() is not loop:
        _refresh_again.pop(org_id, None)
        _refreshing[org_id] = loop.create_task(
            _refresh(org_id, daily_cap_micro, after_refill), name="treg-allowance-refresh")
    else:
        _refresh_again[org_id] = (daily_cap_micro, after_refill)


async def _refresh(org_id: int, daily_cap_micro: int, after_refill) -> None:
    """Refill a counter that still admits calls, so none of them finds it lapsed. Nobody waits
    on this, so it waits for the lease far longer than a call does.

    It asks for no amount of its own: the counter goes back up to a chunk (or to the team's
    pace, `_sized_target`), and a balance that cannot pay for that leaves the counter with what
    it has. The call that started this was served; what the next one needs is for its own
    refill to decide.

    The store asks one caller, once. An ask that reaches this process while a refill is still
    on its way out is kept and gets a refill of its own, or nobody would act on it."""
    try:
        while True:
            try:
                await _refresh_once(org_id, daily_cap_micro, after_refill)
            except Exception:
                # A kept ask still gets its refill: this failure says nothing about the next.
                log.error("allowance refill ahead of the deadline failed for org %s", org_id,
                          exc_info=True)
            again = _refresh_again.pop(org_id, None)
            if again is None:
                return
            daily_cap_micro, after_refill = again
    finally:
        if _refreshing.get(org_id) is asyncio.current_task():
            del _refreshing[org_id]


async def _refresh_once(org_id: int, daily_cap_micro: int, after_refill) -> None:
    store, key, token = kv.store(), money_key(org_id), uuid.uuid4().hex
    deadline = time.monotonic() + _REFRESH_WAIT_S
    while True:
        acquired = await acquire_money_lease(store, org_id, token, _REFILL_LEASE_MS)
        if acquired == "acquired":
            before = refills(org_id)
            try:
                await _refill(org_id, 0, daily_cap_micro)
            finally:
                await store.release_lease(key, token)
            if after_refill is not None and refills(org_id) != before:
                # On its own: it may take a while, and the team's next refill must not wait.
                task = asyncio.get_running_loop().create_task(
                    _after_refill(org_id, after_refill), name="treg-allowance-after-refill")
                _after_refills.add(task)
                task.add_done_callback(_after_refills.discard)
            return
        if acquired == "unavailable" or time.monotonic() > deadline:
            return
        await asyncio.sleep(0.02 + random.random() * 0.02)


async def _after_refill(org_id: int, after_refill) -> None:
    try:
        await after_refill()
    except Exception:
        log.error("the step after an allowance refill failed for org %s", org_id, exc_info=True)


async def _current_team(org_id: int, db) -> allowance.TeamNow:
    """The team's daily cap and balance as its row has them now, with the row locked. A refill
    can run long after the call that asked for it read the team. A plain read would still miss
    a cap whose change is open in another transaction: the refill's balance update then waits
    for that change to commit, and checks only the balance. Locked, the read waits instead, and
    what the refill reads of the day's spend afterwards is under the same lock. The team's row
    is the last lock a money transaction takes, and the refill takes none after it. The balance
    read here is the one the refill's debit then meets: nothing else can move it meanwhile.

    The lock is the one an update of the row takes (NO KEY UPDATE), no stronger. By now the
    fold may have updated the row, and a top-up inserting a credit block holds a KEY SHARE on
    it from the foreign key check while it waits to update the balance. Asking for the full
    row lock here would wait for that top-up, which is waiting for this transaction."""
    row = (await db.execute(
        select(Org.daily_cap_micro, Org.balance_micro).where(Org.id == org_id)
        .with_for_update(key_share=True))).one_or_none()
    return allowance.TeamNow(budget_policy._effective_daily_cap(row),
                             int(row.balance_micro) if row is not None else 0)


def _sizing() -> bool:
    return get_settings().money_allowance_refill_window_s > 0


def _sized_target(spent_micro: int, over_ms: int, floor_micro: int) -> int | None:
    """What this refill asks to bring the counter up to when the team spends faster than the
    floor lasts (`allowance.paced_target`); the transaction keeps it within the team's money.
    None when the reading asks for no more than the floor."""
    settings = get_settings()
    target = allowance.paced_target(
        spent_micro, over_ms, window_s=settings.money_allowance_refill_window_s,
        floor_micro=floor_micro, ceiling_micro=settings.money_allowance_refill_max_micro)
    return target if target > floor_micro else None


async def _refill(org_id: int, need_micro: int, daily_cap_micro: int) -> bool:
    """Bring the counter up to a chunk, or to what the team spends over the refill window when
    that is more (`_sized_target`). False when the counter still cannot cover `need_micro`;
    the allowance is then drained back to the balance so the exact path sees all of the team's
    money.

    Queued charges are the background loop's to fold (`allowance_sweep`), not a refill's: calls
    wait on a refill. The one exception is a frozen counter, where the whole queue is folded
    here so the balance read after it is the true one."""
    store, settings, sizing = kv.store(), get_settings(), _sizing()
    # Sized to the team's spend, the refill reads the pace in the same script as the state: it
    # asks the store nothing a refill to the chunk does not.
    state = await (store.allowance_state_and_pace(org_id) if sizing
                   else store.allowance_state(org_id))
    if state is None:
        return False
    # Read BEFORE the transaction: the add below may thaw only the freezes counted here. One
    # that lands later was raised by a charge this transaction may not have folded.
    available, seen_frozen, thawed, counter_epoch, run = state[:5]
    target = max(settings.money_allowance_chunk_micro, need_micro)
    grow_to = _sized_target(state[5], state[6], target) if sizing else None
    try:
        async with (
            money_lock(org_id),
            money_admission.admit(
                money_admission.require_balance_orgs([org_id]), operation="reserve"),
            money_session(session_maker()) as db,
        ):
            await ledger.reap_stale_holds(db, org_id=org_id)
            # Frozen: a call cost more than it set aside. Calls may spend again only once every
            # queued charge is applied and the balance read after that, not after one batch.
            result = await allowance.refill_allowance_in_transaction(
                db, org_id, max(0, target - available), daily_cap_micro=daily_cap_micro,
                fold_limit=0, fold_all=seen_frozen != thawed,
                counter_epoch=counter_epoch, fresh_add_micro=target, counter_micro=available,
                current_team=functools.partial(_current_team, org_id), grow_to_micro=grow_to,
                balance_percent=settings.money_allowance_refill_balance_percent,
                ceiling_micro=settings.money_allowance_refill_max_micro)
            await db.commit()
    except money_admission.ReserveAdmissionTimeout:
        return False
    if result.epoch != counter_epoch:
        # The counter read was built for another hold. Publishing this one replaces it, and
        # what it showed goes with it.
        available = 0
    if org_id not in _refills:
        _prune_if_full()
    _refills[org_id] = _refills.get(org_id, 0) + 1
    log.info("allowance_refill org_id=%s added=%s hold=%s counter_before=%s folded=%s "
             "target=%s", org_id, result.added_micro, result.amount_micro, available,
             result.folded.calls, grow_to or target)
    if (result.epoch is None or result.over_cap or result.overdrawn or result.retired
            or available + result.added_micro < need_micro):
        _refused_until[org_id] = time.monotonic() + _REFUSAL_COOLDOWN_S
        if result.epoch is not None and not result.retired:
            await _drain(org_id, result.epoch, "no_room_to_refill")
        return False
    # The deadline is the one the refill wrote on the hold, not one counted from now: the idle
    # sweep reads the hold, so a refill published late must not admit calls past it.
    until_ms = (result.admit_until - datetime(1970, 1, 1)) // timedelta(milliseconds=1)
    added = await store.allowance_add(
        org_id, result.epoch, result.added_micro, until_ms=until_ms, ttl_ms=_COUNTER_TTL_MS,
        seen_frozen=seen_frozen, run=run)
    if not isinstance(added, int):
        return False
    if added > result.amount_micro:
        # The counter may never offer more than its hold has. It does only after a charge drew
        # from the hold what the counter was not told about.
        log.warning("allowance_counter_above_hold org_id=%s counter=%s hold=%s",
                    org_id, added, result.amount_micro)
        await store.allowance_take_up_to(org_id, result.epoch, added - result.amount_micro)
    return True


class _Fold(enum.Enum):
    SHORT = "short"  # the queue is short now
    MORE = "more"  # one batch folded, and more may be queued
    HERE = "here"  # this process has a money session of the team open
    ELSEWHERE = "elsewhere"  # another process has the team's lease
    GAVE_UP = "gave_up"  # admission or row locks not free in time, too long a queue, or a fault


async def _fold_backlog(org_id: int, budget: list[int], call_id: str | None = None,
                        found: list[int] | None = None, deadline: float | None = None) -> _Fold:
    """Fold one batch of a team's queued charges now, in its own transaction. For a call that
    must know what the ledger took for it (`_settled`), which reads its entry after each batch
    and comes back while the queue is long; everything else is left to the background loop.
    Calls keep taking from the counter meanwhile, since a fold moves only money their takes
    already removed from it.

    It never waits on another fold, here or elsewhere, and never waits long for the team's
    admission or row locks while it holds a connection: the call is answering, and the
    background fold applies the queue anyway. `budget` holds the batches the call has left and
    is spent here, so a call folds no more than `_BACKLOG_BATCHES` in all. With `call_id`, the
    call's settle entry is read in the fold's own transaction and appended to `found` once that
    transaction has committed, so what the ledger took is known even if the call's deadline
    passes while the session closes or the lease is given back. `deadline` (the event loop's
    clock) bounds it until it holds a database connection."""
    store, key, token = kv.store(), money_key(org_id), uuid.uuid4().hex
    try:
        # Until it holds a connection the batch is optional, and bound by the call's deadline:
        # past it, it does not take a connection a call waiting now could have. Once it holds
        # one it finishes, commit or rollback, close and give back, whatever the time.
        async with asyncio.timeout_at(deadline) as optional:
            if budget[0] <= 0:
                return _Fold.GAVE_UP
            # The lease is given up between batches, and a moment left for a refill to take it:
            # calls are waiting on a refill, nobody is waiting on this.
            await asyncio.sleep(0.05)
            if money_lock(org_id).locked():
                return _Fold.HERE
            try:
                acquired = await acquire_money_lease(store, org_id, token, _REFILL_LEASE_MS)
            except BaseException:
                # The store may have granted the lease before the call stopped waiting for its
                # answer: give back whatever this token holds, or the team's refills and the
                # background fold wait for it to expire. Release deletes only this token's, and
                # runs on its own: the call is past its deadline and does not wait for it.
                _release_lease_soon(store, key, token)
                raise
            if acquired != "acquired":
                # The lease may be held by this process's own fold, which took it while this
                # call waited for the store: its lock says so.
                return _Fold.HERE if money_lock(org_id).locked() else _Fold.ELSEWHERE
            try:
                # Asked for again after the wait: an idle lock can be pruned meanwhile, and the
                # one this process's other calls test is the one now in the map. Nothing is
                # awaited between this test and taking it, so a free lock is taken at once.
                lock = money_lock(org_id)
                if lock.locked():
                    return _Fold.HERE
                async with (
                    lock,
                    money_admission.admit(
                        money_admission.require_balance_orgs([org_id]), operation="close",
                        max_wait_s=_REFILL_WAIT_S, refuse_on_timeout=True),
                    money_session(session_maker()) as db,
                ):
                    await db.connection()
                    optional.reschedule(None)
                    budget[0] -= 1
                    await bound_lock_wait(db)
                    folded = await allowance.fold_charges_in_transaction(
                        db, org_id, limit=_BACKLOG_FOLD)
                    settled = (await allowance.settled_micro(db, org_id, call_id)
                               if call_id is not None else None)
                    await db.commit()
                    if settled is not None and found is not None:
                        found.append(settled)
            finally:
                # Given back by a task of its own: a deadline that passes while the store answers
                # stops this wait, not the release, which deletes only this token's lease. A call
                # already past its deadline (cancelled while it waited for a connection, say) does
                # not wait for the release at all.
                release = _release_lease_soon(store, key, token)
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    await asyncio.shield(release)
            return _Fold.SHORT if folded.claimed < _BACKLOG_FOLD else _Fold.MORE
    except (money_admission.AdmissionTimeout, TimeoutError):
        return _Fold.GAVE_UP
    except Exception as exc:
        if lock_timed_out(exc):
            log.info("folding the queued charges of org %s waited too long on its rows", org_id)
        else:
            log.error("folding the queued charges of org %s failed", org_id, exc_info=True)
        return _Fold.GAVE_UP


_lease_releases: set[asyncio.Task] = set()
_background_folds: set[asyncio.Task] = set()
# The request-path folds of each team running in this process, for a call that waits on one.
_team_folds: dict[int, set[asyncio.Task]] = {}


async def _fold_beside(org_id: int, budget: list[int], call_id: str, found: list[int],
                       started: list[asyncio.Task], deadline: float) -> _Fold:
    """`_fold_backlog` in a task of its own, which the call waits for only until its deadline.
    The batch, and what follows its commit (closing the session, giving back the team's
    admission and lease, its lock in this process), then finishes in order without the call,
    if by `deadline` it holds a connection; one still waiting for one then gives up. The task
    is appended to `started`, so the call can still take its answer later."""
    task = asyncio.ensure_future(_fold_backlog(org_id, budget, call_id, found, deadline))
    _background_folds.add(task)
    task.add_done_callback(_background_folds.discard)
    team = _team_folds.setdefault(org_id, set())
    team.add(task)

    def _forget(done: asyncio.Task) -> None:
        team.discard(done)
        if not team and _team_folds.get(org_id) is team:
            del _team_folds[org_id]

    task.add_done_callback(_forget)
    started.append(task)
    return await asyncio.shield(task)


async def _last_look(org_id: int, call_id: str, found: list[int],
                     fold: asyncio.Task | None) -> int | None:
    """Read the call's entry while a fold of the team may still commit it: the call's own, or
    the one this process is running for the team that the call waited on. Whichever answers
    first. A fold that ends without answering this call (another call's batch, which may still
    have applied this charge) is followed by one more read. The caller bounds it with its
    deadline."""
    read = asyncio.ensure_future(_read_settled(org_id, call_id))
    reads = [read]
    tasks = {read} if fold is None or fold.done() else {read, fold}
    try:
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if found:
                return found[0]
            if read in done:
                try:
                    settled = read.result()
                except Exception:
                    # The fold has a connection of its own and may still answer.
                    log.error("reading the settle entry of allowance call %s failed", call_id,
                              exc_info=True)
                    settled = None
                if settled is not None:
                    return settled
            if fold in done:
                # A read begun before that batch committed may not see it: ask again.
                read.cancel()
                read = asyncio.ensure_future(_read_settled(org_id, call_id))
                reads.append(read)
                tasks.discard(fold)
                tasks.add(read)
                fold = None
        return found[0] if found else None
    finally:
        for pending in reads:
            pending.cancel()


def _team_fold(org_id: int, started: list[asyncio.Task]) -> asyncio.Task | None:
    """The request-path fold of the team still running in this process, the call's own first."""
    for task in (*reversed(started), *_team_folds.get(org_id, ())):
        if not task.done():
            return task
    return None


async def _read_settled(org_id: int, call_id: str) -> int | None:
    async with session_maker() as db:
        return await allowance.settled_micro(db, org_id, call_id)


def _release_lease_soon(store, key: str, token: str) -> asyncio.Task:
    """Release `token`'s lease in a task that outlives a cancelled caller."""
    task = asyncio.ensure_future(store.release_lease(key, token))
    _lease_releases.add(task)
    task.add_done_callback(_lease_releases.discard)
    return task


async def _team_session_ended(org_id: int) -> None:
    """Wait, holding no connection, until this process has no money session of the team open."""
    lock = money_lock(org_id)
    async with lock:
        pass


async def _drain(org_id: int, epoch: int, reason: str) -> None:
    """Empty the counter and return what it held to the balance. The hold stays, with what calls
    already admitted may still charge. Both steps name the hold by its epoch: between them the
    hold can be closed and another opened, and what one counter held is not the other's to
    return."""
    store = kv.store()
    left = await store.allowance_drop(org_id, epoch)
    if not left:
        return
    async with (
        money_lock(org_id),
        money_admission.admit(money_admission.require_balance_orgs([org_id]), operation="release"),
        money_session(session_maker()) as db,
    ):
        await allowance.close_allowance_in_transaction(
            db, org_id, reason=reason, release_micro=left, epoch=epoch)
        await db.commit()


async def give_back_for_exact_path(org_id: int, short_micro: int) -> bool:
    """Return what a team's counter holds to its balance, for a call on the exact path that the
    balance alone could not pay for, by `short_micro`: money set aside for the allowance is out
    of that balance's sight. True when that much went back, and the call may try once more.
    Only while refills are sized to the team's spend, which can set aside much more than a
    chunk.

    It costs a refusal as little as it can. The counter is read first, without the team's
    lease, and a counter that cannot make up the difference is left alone. The team's lease is
    asked for once: held by a refill or a fold, the call is refused as before, not kept
    waiting. Then `_give_back`."""
    if not (_sizing() and enabled_for(org_id)):
        return False
    store, key, token = kv.store(), money_key(org_id), uuid.uuid4().hex
    leased = False
    # Whatever fails here, the call is refused as it would have been: never an error of its own.
    try:
        state = await store.allowance_state(org_id)
        if state is None or not state[3] or state[0] < max(1, short_micro):
            return False
        leased = await acquire_money_lease(store, org_id, token, _REFILL_LEASE_MS) == "acquired"
        if not leased:
            return False
        returned = await _give_back(org_id, state[3])
    except money_admission.AdmissionTimeout:
        return False
    except Exception:
        log.error("returning the allowance of org %s for the exact path failed", org_id,
                  exc_info=True)
        return False
    finally:
        if leased:
            await store.release_lease(key, token)
    if returned:
        log.info("allowance_given_back org_id=%s returned=%s", org_id, returned)
    return returned >= max(1, short_micro)


async def _give_back(org_id: int, epoch: int) -> int:
    """Empty the counter of `epoch` and return what it held to the balance, in one money
    session; what it held. The team's money admission is taken first, for no longer than a call
    waits for a refill and refusing rather than falling back, so a call short of money never
    queues behind a long admission, and until the counter is emptied nothing has changed: a
    timeout or a cancel before then leaves it as it was. The counter is frozen by the drop, so
    the next call on the allowance refills it from the balance, the whole queue folded first.
    A release that fails after the drop gives the money back to that frozen counter. A commit
    that fails proves nothing about whether it landed, so then the money stays in the hold,
    for the idle sweep to return."""
    store = kv.store()
    async with (
        money_lock(org_id),
        money_admission.admit(
            money_admission.require_balance_orgs([org_id]), operation="release",
            max_wait_s=_REFILL_WAIT_S, refuse_on_timeout=True),
        money_session(session_maker()) as db,
    ):
        left = await store.allowance_drop(org_id, epoch)
        if not left:
            return 0
        try:
            await allowance.close_allowance_in_transaction(
                db, org_id, reason="exact_path_short", release_micro=left, epoch=epoch)
        except BaseException:
            # Nothing is committed and the hold still has it: back on the counter it came from,
            # whatever stopped the release. The drop froze the counter, so no call spends it
            # before a refill has read the balance again.
            await asyncio.shield(store.allowance_give_back(org_id, epoch, left))
            raise
        await db.commit()
    return left


class _Written(NamedTuple):
    """What the writer tells a call about its charge. `confirmed`: the charge is known
    committed. `took_from_balance`: a charge of the call's team in the same batch took from the
    balance, so the team's credit may not cover every charge and the call answers with what
    the ledger took for it. The counter's freeze says the same, but a refill can lift it
    before the call is told."""

    confirmed: bool
    took_from_balance: bool = False


class _Unwritten(Exception):
    """A batch left some teams' rows unconfirmed; `orgs` names those teams. Every other team's
    rows are committed and its freeze is with the store; `took_from_balance` names the ones of
    them with a charge that took from the balance."""

    def __init__(self, orgs, took_from_balance=()) -> None:
        self.orgs = frozenset(orgs)
        self.took_from_balance = frozenset(took_from_balance)
        super().__init__(f"call charges of {len(self.orgs)} team(s) are unconfirmed")


class _ChargeWriter:
    """Queues finished calls' charges and inserts them a batch at a time.

    One insert per call would put a transaction back on every call. Calls that finish within
    the linger share one, and each still waits for its batch to commit before it answers.

    A write that raises proves nothing about whether it committed. Such rows are reported
    unconfirmed, never as "not charged", and are written again until they land: recording a
    call twice charges it once, so repeating is safe and giving up is not. A batch is written a
    team at a time and answered a team at a time: one team's failure leaves another team's
    calls, whose rows are committed, confirmed.

    Every row here belongs to a call that was admitted while there was room for it (`owed`
    counts each from `submit` until it is known written), so nothing is ever cut from a queue.

    A keyed call's in-flight mark (`marked`) is the writer's from `submit` on: a stale claim
    reads the mark until it can read the charge, so the writer renews every mark it holds on its
    own clock (`_keep_marks`), whatever its batches are doing, and clears one only once its charge
    is known written, whatever happened to the call meanwhile. One mark may stand for several
    charges (a routed root's, for its children: `mark_id`, `hold_mark`): it is cleared once the
    last of them is written and every hold on it is released.

    The writer, not the call, freezes a team's counter for an overrun, and does it after the
    charge is committed and before the call is told: only then can a refill that reads the
    freeze have the charge in front of it, and a call cancelled while it waited changes nothing.
    """

    def __init__(self) -> None:
        self._pending: list[tuple[allowance.Charge, tuple[int, str] | None, asyncio.Future]] = []
        self._unconfirmed: list[tuple[allowance.Charge, tuple[int, str] | None]] = []
        self._task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self.owed = 0
        # mark -> charges and holds still keeping it; charge call id -> the mark it keeps
        self._marked: dict[str, int] = {}
        self._mark_of: dict[str, str] = {}
        self._marks_task: asyncio.Task | None = None
        # Orders a renewal against a clear: a mark set again after its clear would outlive its
        # charge, and a claim would wait for it to expire.
        self._marks_lock = asyncio.Lock()

    def submit(self, charge: allowance.Charge, archive_use: tuple[int, str] | None, *,
               marked: bool = False, mark_id: str | None = None) -> asyncio.Future:
        """Queue one charge now, with no await: a call cancelled right after still pays. The
        future resolves to a `_Written`: confirmed once the charge is known committed,
        unconfirmed when that is not known yet and the writer keeps trying."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending.append((charge, archive_use, future))
        self.owed += 1
        if marked:
            mark = mark_id or charge.call_id
            self._marked[mark] = self._marked.get(mark, 0) + 1
            self._mark_of[charge.call_id] = mark
            self._marks_task = self._ensure(self._marks_task, self._keep_marks, "treg-allowance-marks")
        self._task = self._ensure(self._task, self._run, "treg-allowance-charges")
        return future

    @staticmethod
    def _ensure(task: asyncio.Task | None, run, name: str) -> asyncio.Task:
        loop = asyncio.get_running_loop()
        if task is None or task.done() or task.get_loop() is not loop:
            return loop.create_task(run(), name=name)
        return task

    async def _run(self) -> None:
        while self._pending:
            await asyncio.sleep(get_settings().money_allowance_linger_ms / 1000)
            batch = self._pending[:allowance.FOLD_BATCH]
            del self._pending[:len(batch)]
            rows = [(charge, use) for charge, use, _ in batch]
            failed, drew, fault = await self._attempt(rows)
            # A team whose rows are committed is told now: its calls do not wait for another
            # team's retry.
            self._answer(batch, failed, drew, final=False)
            if failed:  # one bounded retry on a fresh transaction, of the teams that failed
                await asyncio.sleep(0.2)
                failed, drew, fault = await self._attempt(
                    [row for row in rows if row[0].org_id in failed], again=True)
            owed = [row for row in rows if row[0].org_id in failed]
            await self._committed([row for row in rows if row[0].org_id not in failed])
            if owed:
                log.error("writing %d call charges is unconfirmed; retrying in the background",
                          len(owed), exc_info=fault)
                self._unconfirmed.extend(owed)
                self._retry_task = self._ensure(
                    self._retry_task, self._retry, "treg-allowance-charge-retry")
            self._answer(batch, failed, drew, final=True)

    @staticmethod
    def _answer(batch: list, failed: frozenset[int], drew: frozenset[int], *,
                final: bool) -> None:
        """Tell each waiting call of a team that was written. A team that failed is told only
        once it has been tried again (`final`)."""
        for charge, _, future in batch:
            if future.done() or (charge.org_id in failed and not final):
                continue
            future.set_result(_Written(charge.org_id not in failed, charge.org_id in drew))

    async def _attempt(self, rows: list, *, again: bool = False
                       ) -> tuple[frozenset[int], frozenset[int], BaseException | None]:
        """Write the rows. Returns the teams whose rows are not confirmed written, the teams
        written with a charge that took from the balance, and the fault: none when every
        team's rows were written. A fault that names no team leaves them all unconfirmed."""
        try:
            drew = await self._write(rows, again=again)
        except _Unwritten as exc:
            return exc.orgs, exc.took_from_balance, exc.__cause__ or exc
        except Exception as exc:  # noqa: BLE001 - whatever the fault, the rows are tried again
            return frozenset(charge.org_id for charge, _ in rows), frozenset(), exc
        return frozenset(), frozenset(drew or ()), None

    async def _retry(self) -> None:
        delay = 1.0
        while self._unconfirmed:
            await asyncio.sleep(delay)
            rows = self._unconfirmed[:allowance.FOLD_BATCH]
            failed, _, _ = await self._attempt(rows, again=True)
            written = [row for row in rows if row[0].org_id not in failed]
            await self._committed(written)
            # The rows still unconfirmed go to the back: a team whose writes keep failing does
            # not keep the teams queued behind it from being tried.
            self._unconfirmed = (self._unconfirmed[len(rows):]
                                 + [row for row in rows if row[0].org_id in failed])
            if written:
                log.info("%d unconfirmed call charges are now written", len(written))
            delay = min(delay * 2, 60.0) if failed else 1.0

    def has_room(self, in_flight: int) -> bool:
        """Whether one more admitted call can still be given a place if every write fails."""
        return in_flight + self.owed < _MAX_OWED

    def behind(self) -> bool:
        """This process has charges it could not confirm written. Until it has, it admits no
        more calls to the allowance: each would add a charge the database is not taking."""
        return bool(self._unconfirmed)

    async def _committed(self, rows: list) -> None:
        """The rows are in the database and their teams' freezes with the store: count the rows
        as written, and clear their calls' in-flight marks, which a stale claim no longer needs."""
        self.owed = max(0, self.owed - len(rows))
        async with self._marks_lock:
            done = []
            for charge, _ in rows:
                mark = self._mark_of.pop(charge.call_id, None)
                if mark is not None and self._drop_mark(mark):
                    done.append(mark)
            if done:
                # One round trip for the batch: a store that does not answer costs the writer one
                # short timeout, not one per row.
                await kv.store().clear_calls_in_flight(done)

    def _drop_mark(self, mark: str) -> bool:
        """One charge or hold no longer keeps `mark`; True when none does."""
        left = self._marked.get(mark, 0) - 1
        if left > 0:
            self._marked[mark] = left
            return False
        self._marked.pop(mark, None)
        return True

    def hold_mark(self, mark: str) -> None:
        """Keep `mark` renewed until `release_mark`, whatever charges come and go meanwhile."""
        self._marked[mark] = self._marked.get(mark, 0) + 1
        self._marks_task = self._ensure(self._marks_task, self._keep_marks, "treg-allowance-marks")

    async def release_mark(self, mark: str) -> None:
        """Let go of a `hold_mark`; the mark is cleared now if no unwritten charge keeps it."""
        async with self._marks_lock:
            if self._drop_mark(mark):
                await kv.store().clear_calls_in_flight([mark])

    async def _keep_marks(self) -> None:
        """Renew every mark the writer holds, queued, being written or unconfirmed alike, until
        none is left: until its charge is written, the mark is all a stale claim can read of the
        call, and the request that set it may be gone."""
        while self._marked:
            await asyncio.sleep(_MARK_RENEW_S)
            # Set, not only extended: a store that restarted lost the marks, and the writer
            # knows these calls' charges are still unwritten.
            async with self._marks_lock:
                if self._marked:
                    await kv.store().mark_calls_in_flight(sorted(self._marked), IN_FLIGHT_MARK_MS)

    @staticmethod
    async def _write(rows: list, *, again: bool = False) -> set[int]:
        """Write the rows, one transaction per team, and freeze each team's counter as soon as
        its own rows are committed.

        A transaction that held several teams' hold rows would have to take them in an order
        every other holder of two agreed on, and a team's first hold can appear after a deletion
        of several teams has chosen its order. And a team's freeze must not wait for another
        team's write: its rows are committed, and its counter is still admitting calls.

        Returns the teams with a charge that took from the balance. Raises `_Unwritten`, after
        every team was tried, naming the teams whose write failed or whose freeze did not reach
        the store. Their rows are then gone through again, which writes nothing twice and sends
        the freeze again."""
        store = kv.store()
        by_team: dict[int, list] = {}
        for row in rows:
            by_team.setdefault(row[0].org_id, []).append(row)
        failed: dict[int, Exception] = {}
        drew: set[int] = set()
        for org_id in sorted(by_team):
            # One team's failure is not another's: every team's rows are tried, so a store
            # that does not take one team's freeze keeps no other team's charge in memory.
            try:
                if await _ChargeWriter._write_team(org_id, by_team[org_id], again):
                    if (await store.allowance_freeze(org_id, ttl_ms=_COUNTER_TTL_MS) is None
                            and kv.configured()):
                        raise ConnectionError("the store did not take a freeze")
                    drew.add(org_id)
            except Exception as exc:  # noqa: BLE001 - reported once every team was tried
                failed[org_id] = exc
        if failed:
            raise _Unwritten(failed, drew) from next(iter(failed.values()))
        return drew

    @staticmethod
    async def _write_team(org_id: int, rows: list, again: bool) -> bool:
        """Write one team's rows. True when one of them takes from the balance: it overran what
        its call set aside, or its own hold no longer stands behind it (closed since, with or
        without another in its place)."""
        # Bounded like every money transaction: this one holds the team's hold row, and a
        # process that stalled here would keep its refills, folds and sweeps waiting.
        async with money_session(session_maker()) as db:
            # PostgreSQL: the shared lock on the hold row comes first, the order a team's
            # deletion uses too. Where there are no row locks the insert comes first and the
            # hold is read under the write lock it took.
            row_locks = db.get_bind().dialect.name == "postgresql"
            holds = await allowance.holds_behind(db, [org_id]) if row_locks else {}
            # A charge admitted for a team that has been deleted is nobody's: neither queued
            # nor marked as a paid question.
            if not await allowance.team_exists(db, org_id):
                log.error("allowance_charges_orphaned org_id=%s dropped=%s: the team they were "
                          "admitted for has been deleted", org_id, len(rows))
                return False
            new = await allowance.record_charges(db, [charge for charge, _ in rows])
            if not row_locks:
                holds = await allowance.holds_behind(db, [org_id])
            # A row that was already there was committed by an earlier attempt, which marked its
            # paid question too. On a rewrite, so was a row that is new only because the earlier
            # attempt's row has been applied and deleted since: its call has a settle entry, and
            # the row just written is taken out again.
            if again and new:
                new -= await allowance.drop_settled_charges(db, sorted(new))
            uses = [use for charge, use in rows if use is not None and charge.call_id in new]
            if uses:
                await archive.note_org_uses_in_transaction(db, uses)
            await db.commit()
        return any(_overran(charge)
                   or (charge.epoch is not None and holds.get(org_id) != charge.epoch)
                   for charge, _ in rows)


def _overran(charge: allowance.Charge) -> bool:
    return (charge.reserved_micro is not None
            and allowance.charged_micro(charge) > charge.reserved_micro)


_charges = _ChargeWriter()


# Freezes asked for and not yet taken by the store, per team.
_freezes_due: dict[int, int] = {}
_freeze_waiters: dict[int, list[asyncio.Future]] = {}
_freeze_tasks: dict[int, asyncio.Task] = {}
_freeze_wake: dict[int, asyncio.Event] = {}
_FREEZE_RETRY_S = 0.5


async def overran(db, org_id: int | None, charged_micro: int, reserved_micro: int | None) -> bool:
    """Whether a settlement outside the allowance owes the team's counter a freeze: it took more
    than its hold, and the team has an allowance hold. Asked in the settlement's transaction,
    after its ledger write and before its commit; the hold row decides, not this process's list
    of teams, which another process may read differently."""
    if (org_id is None or reserved_micro is None or charged_micro <= reserved_micro
            or not kv.configured()):
        return False
    return await allowance.has_allowance(db, org_id)


def freeze(org_id: int) -> asyncio.Future | None:
    """Stop admitting a team's calls from its counter until a refill has read the balance again.
    For a settlement outside the allowance that took more than its hold, called right after its
    commit: the team's credit may be gone, and the counter does not know.

    Not a coroutine: the freeze is owed from this line on, to a task of its own, so a request
    cancelled at its next await has still handed it over. The task sends until the store has
    taken it. The future resolves once it has been tried; awaiting it is optional. Never raises."""
    if not kv.configured():
        return None
    _freezes_due[org_id] = _freezes_due.get(org_id, 0) + 1
    tried = asyncio.get_running_loop().create_future()
    _freeze_waiters.setdefault(org_id, []).append(tried)
    _freeze_tasks[org_id] = _ChargeWriter._ensure(
        _freeze_tasks.get(org_id), lambda: _deliver_freezes(org_id), "treg-allowance-freeze")
    wake = _freeze_wake.get(org_id)
    if wake is not None:
        wake.set()
    return tried


async def deliver_owed_freezes(timeout_s: float = 10.0) -> int:
    """For a process about to exit (a worker tick): wait, for at most `timeout_s`, for the
    freezes it owes to reach the store. Returns how many are still owed; those are lost with
    the process."""
    tasks = [task for task in _freeze_tasks.values() if not task.done()]
    for wake in _freeze_wake.values():
        wake.set()  # a task pausing before its next attempt tries now, not after the pause
    if tasks:
        await asyncio.wait(tasks, timeout=timeout_s)
    return sum(_freezes_due.values())


async def _freeze_once(org_id: int) -> bool:
    try:
        return await kv.store().allowance_freeze(org_id, ttl_ms=_COUNTER_TTL_MS) is not None
    except Exception:  # noqa: BLE001 - whatever the fault, the freeze is still owed
        return False


async def _deliver_freezes(org_id: int) -> None:
    """Send one team's freezes until the store has taken them all. A task per team: a team the
    store keeps refusing does not hold up another's, and a freeze asked while this one waits to
    try again is tried at once, so the settlement that asked waits for one attempt, not for
    the pause."""
    delay = _FREEZE_RETRY_S
    wake = _freeze_wake[org_id] = asyncio.Event()
    while org_id in _freezes_due:
        # One freeze stands for every settlement that asked before it was sent, and for none
        # that asks while it is on its way: that one committed after a refill may already
        # have read this freeze.
        owed, waiters = _freezes_due[org_id], _freeze_waiters.pop(org_id, [])
        wake.clear()
        taken = await _freeze_once(org_id)
        if taken and _freezes_due[org_id] == owed:
            del _freezes_due[org_id]
        elif taken:
            _freezes_due[org_id] -= owed
        for tried in waiters:
            if not tried.done():
                tried.set_result(taken)
        if taken:
            delay = _FREEZE_RETRY_S
            continue
        log.error("allowance_freeze_undelivered org_id=%s; it will be sent again", org_id)
        try:
            await asyncio.wait_for(wake.wait(), delay)
        except TimeoutError:
            pass
        delay = min(delay * 2, 30.0)
    _freeze_wake.pop(org_id, None)
    _freeze_tasks.pop(org_id, None)


def hold_mark(mark: str) -> None:
    """The charge writer renews `mark` (a keyed routed root's in-flight mark) from now until
    `release_mark` and until every charge handed over with it is written."""
    _charges.hold_mark(mark)


async def release_mark(mark: str) -> None:
    await _charges.release_mark(mark)


async def finish(org_id: int, call_id: str, epoch: int, reserved_micro: int, *,
                 endpoint_id: str, billable: bool, actual_micro: int | None = None,
                 meta: dict | None = None, archive_use: tuple[int, str] | None = None,
                 marked: bool = False, mark_id: str | None = None) -> int:
    """End one take, once: charge the call and give back the rest, or give back all of it.
    Returns what was charged. Never raises: the call already has its answer.

    `marked`: the call has an in-flight mark (a keyed call). A call charged nothing clears it
    here; a charge hands it to the writer, which clears it once the charge is written.
    `mark_id` names the mark when it is not the call's own (a routed root's, held by
    `hold_mark`): a call charged nothing then leaves it alone.

    The charge is queued before anything is awaited, so a cancel in the middle cannot lose it.
    A give-back that does not arrive leaves that money in the hold until a close returns it.
    A charge whose write is not confirmed is still reported as charged and its money is not
    given back: it may have committed, and it is written again until it has.
    """
    store = kv.store()
    _in_flight[0] = max(0, _in_flight[0] - 1)
    amount = 0  # what the team owes once the charge is queued; never answered as 0 after that
    try:
        if not billable:
            await store.allowance_give_back(org_id, epoch, reserved_micro)
            if marked and mark_id is None:
                await store.clear_call_in_flight(call_id)
            return 0
        charge = allowance.Charge(
            call_id, org_id, endpoint_id, actual_micro, meta, reserved_micro, epoch)
        written = _charges.submit(charge, archive_use, marked=marked, mark_id=mark_id)
        amount = allowance.charged_micro(charge)
        if amount < reserved_micro:
            await store.allowance_give_back(org_id, epoch, reserved_micro - amount)
        # Unconfirmed is not "not charged": the charge may have committed, and the writer keeps
        # trying until it has. Its money stays out of the counter either way. An overrun freezes
        # the counter, and the writer does that once the charge is committed.
        confirmed, took_from_balance = await written
        if not confirmed:
            return amount
        if amount <= reserved_micro and not took_from_balance:
            state = await store.allowance_state(org_id)
            if state is None or state[1] == state[2]:
                return amount
        # Frozen, or a charge written with this one took from the balance: some settlement of
        # the team took more than was set aside for it, and the team's credit may not cover
        # every charge. Apply the queue now and answer with what the
        # ledger took from the team for this call, less than the charge when the credit ran out.
        return await _settled(org_id, call_id, amount)
    except Exception:
        log.error("finishing allowance take %s failed", call_id, exc_info=True)
        return amount


async def _settled(org_id: int, call_id: str, amount: int) -> int:
    """What the call's settle entry took, folding the team's queue to get it written.

    One fold of the team at a time in this process: a call that finds one open waits for it,
    holding no connection, instead of starting its own. A call that finds another process
    folding reads the entry again shortly. Everything, waits for a pool connection included,
    shares one deadline, `_SETTLED_WAIT_S`, of which the last `_LAST_READ_S` is kept for one
    more read of the entry: a fold that runs out the time, or another process that settled the
    call meanwhile, still leaves the call that look. `amount`, the charge, when the entry is not
    there by then or cannot be read: the charge is written, and "nothing" would be the one
    answer known to be wrong."""
    budget, found, started = [_BACKLOG_BATCHES], [], []
    loop = asyncio.get_running_loop()
    end = loop.time() + _SETTLED_WAIT_S
    folding_until = end - min(_LAST_READ_S, _SETTLED_WAIT_S / 5)
    try:
        async with asyncio.timeout_at(folding_until):
            while True:
                outcome = await _fold_beside(org_id, budget, call_id, found, started,
                                             folding_until)
                if found:
                    return found[0]
                if outcome is _Fold.MORE:
                    # The batch's own transaction has just looked for the entry: fold on.
                    continue
                if outcome in (_Fold.GAVE_UP, _Fold.SHORT):
                    break
                if outcome is _Fold.HERE:
                    await _team_session_ended(org_id)
                # The timeout's cancel can be absorbed on the way out: a money session that
                # fails and is cancelled while it closes re-raises its own failure, which the
                # fold reports as giving up. So the deadline is looked at, not only awaited.
                if loop.time() >= folding_until:
                    break
                async with session_maker() as db:
                    settled = await allowance.settled_micro(db, org_id, call_id)
                if settled is not None:
                    return settled
                if outcome is _Fold.ELSEWHERE:
                    await asyncio.sleep(0.05)
    except TimeoutError:
        pass
    except Exception:
        log.error("reading the settle entry of allowance call %s failed", call_id, exc_info=True)
        return found[0] if found else amount
    if found:  # committed before the deadline cut the fold's cleanup short
        return found[0]
    if loop.time() < end:
        try:
            async with asyncio.timeout_at(end):
                settled = await _last_look(org_id, call_id, found, _team_fold(org_id, started))
            if settled is not None:
                return settled
        except TimeoutError:
            pass
        except Exception:
            log.error("reading the settle entry of allowance call %s failed", call_id,
                      exc_info=True)
    if found:  # a fold the call left running committed it while the call last looked
        return found[0]
    log.warning("allowance call %s was answered before its charge was applied", call_id)
    return amount
