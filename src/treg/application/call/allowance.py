"""Spending a metered call from the team's allowance instead of a hold of its own.

The allowance is one database hold per team (`domain/money/allowance.py`), mirrored as a counter in
the shared store. A call takes its reservation from the counter in one atomic step and, when it
ends, gives back what it did not use and queues its charge. The balance row is touched only by a
refill. Every failure here returns the call to the exact path or leaves money in the hold; none
spends money the hold does not have.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from datetime import datetime, timedelta

from ... import archive
from ...config import get_settings
from ...domain import money as ledger
from ...domain.money import allowance
from ...infra import kv, money_admission
from ...infra.db import session_maker
from ...infra.money_session import money_session

log = logging.getLogger("treg.money_allowance")

# A refill that could not make room is not retried per call: the team stays on the exact path.
_REFUSAL_COOLDOWN_S = 5.0
_REFILL_LEASE_MS = 5000
_REFILL_WAIT_S = 0.25
_SETTLED_TRIES = 20
# Long enough that a counter outlives any pause in refills that still leaves its hold open.
_COUNTER_TTL_MS = 24 * 3600 * 1000
_refused_until: dict[int, float] = {}
# Takes this process admitted and has not finished. Each will hand the charge writer one row at
# most, so admitting against the writer's room now is what keeps it from ever overflowing.
_in_flight = [0]
# The most charges this process may owe the database at once, in flight or waiting to be written.
_MAX_OWED = 20_000
_money_locks: dict[tuple[int, asyncio.AbstractEventLoop], asyncio.Lock] = {}


def money_lock(org_id: int) -> asyncio.Lock:
    """One allowance money session per team at a time in this process. PostgreSQL would make
    them wait on the hold's row lock anyway; SQLite has no row locks, and there two sessions
    that both read the hold before either wrote would lose one of the writes."""
    key = (org_id, asyncio.get_running_loop())
    lock = _money_locks.get(key)
    if lock is None:
        lock = _money_locks[key] = asyncio.Lock()
    return lock
_refills: dict[int, int] = {}


def refills(org_id: int) -> int:
    """How many refills this process has committed for the team: a caller compares it around a
    take to learn that the balance moved."""
    return _refills.get(org_id, 0)


def enabled_for(org_id: int) -> bool:
    return org_id in get_settings().money_allowance_org_ids and kv.configured()


def money_key(org_id: int) -> str:
    """The lease every allowance money session of a team holds: a refill, a drain, a fold, the
    sweep's close. PostgreSQL would make them wait on the hold's row lock anyway. SQLite has no
    row locks, so there this lease is what stops two processes from both reading the hold
    before either writes."""
    return f"treg:money-allowance-money:v1:{org_id}"


async def take(org_id: int, amount_micro: int, *, daily_cap_micro: int = 0) -> int | None:
    """Reserve `amount_micro` from the team's allowance. The counter's epoch, or None when the
    call must use the exact path: the store cannot answer, another process holds the refill for
    too long, or the balance or the daily cap leaves no room to refill."""
    if (_refused_until.get(org_id, 0.0) > time.monotonic() or _charges.behind()
            or org_id in _freezes_due or not _charges.has_room(_in_flight[0])):
        return None
    # The place is taken before anything is awaited, so calls arriving together cannot all pass
    # on the last one. `finish` gives it back, or hands it to the charge writer.
    _in_flight[0] += 1
    epoch = None
    try:
        epoch = await _take(org_id, amount_micro, daily_cap_micro)
    finally:
        if not epoch:
            _in_flight[0] -= 1
    return epoch


async def _take(org_id: int, amount_micro: int, daily_cap_micro: int) -> int | None:
    store = kv.store()
    epoch = await store.allowance_take(org_id, amount_micro)
    if epoch or epoch is None:
        return epoch
    key, token = money_key(org_id), uuid.uuid4().hex
    deadline = time.monotonic() + _REFILL_WAIT_S
    while True:
        acquired = await store.acquire_lease(key, token, _REFILL_LEASE_MS)
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
            return (await store.allowance_take(org_id, amount_micro) or None) if refilled else None
        # Another process is refilling: its add is what this call is waiting for.
        await asyncio.sleep(0.005 + random.random() * 0.005)
        epoch = await store.allowance_take(org_id, amount_micro)
        if epoch or epoch is None or time.monotonic() > deadline:
            return epoch or None


async def _refill(org_id: int, need_micro: int, daily_cap_micro: int) -> bool:
    """Fold queued charges and bring the counter up to a chunk. False when the counter still
    cannot cover `need_micro`; the allowance is then drained back to the balance so the exact
    path sees all of the team's money."""
    store, settings = kv.store(), get_settings()
    state = await store.allowance_state(org_id)
    if state is None:
        return False
    # Read BEFORE the transaction: the add below may thaw only the freezes counted here. One
    # that lands later was raised by a charge this transaction may not have folded.
    available, seen_frozen, thawed, counter_epoch = state
    target = max(settings.money_allowance_chunk_micro, need_micro)
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
                fold_all=seen_frozen != thawed,
                counter_epoch=counter_epoch, fresh_add_micro=target)
            await db.commit()
    except money_admission.ReserveAdmissionTimeout:
        return False
    if result.epoch != counter_epoch:
        # The counter read was built for another hold. Publishing this one replaces it, and
        # what it showed goes with it.
        available = 0
    _refills[org_id] = _refills.get(org_id, 0) + 1
    if result.folded.calls + result.folded.duplicates >= allowance.FOLD_BATCH:
        # A full batch: more is queued than one refill folds. A chunk can pay for more calls
        # than that, so without this the queue of a busy team would only grow.
        _fold_in_background(org_id)
    log.info("allowance_refill org_id=%s added=%s hold=%s counter_before=%s folded=%s duplicates=%s",
             org_id, result.added_micro, result.amount_micro, available,
             result.folded.calls, result.folded.duplicates)
    if (result.epoch is None or result.over_cap or result.overdrawn
            or available + result.added_micro < need_micro):
        _refused_until[org_id] = time.monotonic() + _REFUSAL_COOLDOWN_S
        if result.epoch is not None:
            await _drain(org_id, result.epoch, "no_room_to_refill")
        return False
    # The deadline is the one the refill wrote on the hold, not one counted from now: the idle
    # sweep reads the hold, so a refill published late must not admit calls past it.
    until_ms = (result.admit_until - datetime(1970, 1, 1)) // timedelta(milliseconds=1)
    added = await store.allowance_add(
        org_id, result.epoch, result.added_micro, until_ms=until_ms, ttl_ms=_COUNTER_TTL_MS,
        seen_frozen=seen_frozen)
    if not isinstance(added, int):
        return False
    if added > result.amount_micro:
        # The counter may never offer more than its hold has. It does only after a charge drew
        # from the hold what the counter was not told about.
        log.warning("allowance_counter_above_hold org_id=%s counter=%s hold=%s",
                    org_id, added, result.amount_micro)
        await store.allowance_take_up_to(org_id, result.epoch, added - result.amount_micro)
    return True


_folding: dict[int, asyncio.Task] = {}


def _fold_in_background(org_id: int) -> None:
    task = _folding.get(org_id)
    loop = asyncio.get_running_loop()
    if task is None or task.done() or task.get_loop() is not loop:
        _folding[org_id] = loop.create_task(_fold_backlog(org_id), name="treg-allowance-fold")


async def _fold_backlog(org_id: int) -> None:
    """Fold a team's queued charges, a batch per transaction, until the queue is short. One
    process at a time per team; calls keep taking from the counter meanwhile, since a fold
    moves only money their takes already removed from it."""
    store, key, token = kv.store(), money_key(org_id), uuid.uuid4().hex
    try:
        while True:
            # The lease is given up between batches, and a moment left for a refill to take it:
            # calls are waiting on a refill, nobody is waiting on this.
            await asyncio.sleep(0.01)
            if await store.acquire_lease(key, token, _REFILL_LEASE_MS) != "acquired":
                return
            try:
                async with (
                    money_lock(org_id),
                    money_admission.admit(
                        money_admission.require_balance_orgs([org_id]), operation="close"),
                    money_session(session_maker()) as db,
                ):
                    folded = await allowance.fold_charges_in_transaction(db, org_id)
                    await db.commit()
            finally:
                await store.release_lease(key, token)
            if folded.calls + folded.duplicates < allowance.FOLD_BATCH:
                return
    except Exception:
        log.error("folding the queued charges of org %s failed", org_id, exc_info=True)


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


class _ChargeWriter:
    """Queues finished calls' charges and inserts them a batch at a time.

    One insert per call would put a transaction back on every call. Calls that finish within
    the linger share one, and each still waits for its batch to commit before it answers.

    A write that raises proves nothing about whether it committed. Such a batch is reported
    unconfirmed, never as "not charged", and is written again until it lands: recording a call
    twice charges it once, so repeating is safe and giving up is not.

    Every row here belongs to a call that was admitted while there was room for it (`owed`
    counts each from `submit` until it is known written), so nothing is ever cut from a queue.

    The writer, not the call, freezes a team's counter for an overrun, and does it after the
    charge is committed and before the call is told: only then can a refill that reads the
    freeze have the charge in front of it, and a call cancelled while it waited changes nothing.
    """

    def __init__(self) -> None:
        self._pending: list[tuple[allowance.Charge, tuple[int, str] | None, asyncio.Future]] = []
        self._unconfirmed: list[tuple[allowance.Charge, tuple[int, str] | None]] = []
        self._task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._since_fold: dict[int, int] = {}
        self.owed = 0

    def submit(self, charge: allowance.Charge,
               archive_use: tuple[int, str] | None) -> asyncio.Future:
        """Queue one charge now, with no await: a call cancelled right after still pays. The
        future resolves True once the charge is known committed, False when that is not known
        yet and the writer keeps trying."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending.append((charge, archive_use, future))
        self.owed += 1
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
            confirmed = True
            try:
                try:
                    unbacked = await self._write(rows)
                except Exception:  # noqa: BLE001 - one bounded retry on a fresh transaction
                    await asyncio.sleep(0.2)
                    unbacked = await self._write(rows, again=True)
                await self._committed(rows, unbacked)
            except Exception:
                confirmed = False
                log.error("writing %d call charges is unconfirmed; retrying in the background",
                          len(rows), exc_info=True)
                self._unconfirmed.extend(rows)
                self._retry_task = self._ensure(
                    self._retry_task, self._retry, "treg-allowance-charge-retry")
            for _, _, future in batch:
                if not future.done():
                    future.set_result(confirmed)

    async def _retry(self) -> None:
        delay = 1.0
        while self._unconfirmed:
            await asyncio.sleep(delay)
            rows = self._unconfirmed[:allowance.FOLD_BATCH]
            try:
                await self._committed(rows, await self._write(rows, again=True))
            except Exception:  # noqa: BLE001 - whatever the fault, try again later
                delay = min(delay * 2, 60.0)
                continue
            del self._unconfirmed[:len(rows)]
            delay = 1.0
            log.info("%d unconfirmed call charges are now written", len(rows))

    def has_room(self, in_flight: int) -> bool:
        """Whether one more admitted call can still be given a place if every write fails."""
        return in_flight + self.owed < _MAX_OWED

    def behind(self) -> bool:
        """This process has charges it could not confirm written. Until it has, it admits no
        more calls to the allowance: each would add a charge the database is not taking."""
        return bool(self._unconfirmed)

    async def _committed(self, rows: list, unbacked: set[int]) -> None:
        """The rows are in the database. Freeze the counter of every team with a charge among
        them that takes from the balance (an overrun, or a charge its hold no longer backs:
        `unbacked`), then count the rows as written and start folds that are due.

        Raises when a freeze does not reach the store: the batch then stays unconfirmed and is
        gone through again, because such a charge committed without its freeze is a counter
        left open over a balance nobody has checked."""
        store = kv.store()
        owing = unbacked | {charge.org_id for charge, _ in rows if _overran(charge)}
        for org_id in sorted(owing):
            if (await store.allowance_freeze(org_id, ttl_ms=_COUNTER_TTL_MS) is None
                    and kv.configured()):
                raise ConnectionError("the store did not take a freeze")
        self.owed = max(0, self.owed - len(rows))
        for charge, _ in rows:
            count = self._since_fold.get(charge.org_id, 0) + 1
            if count >= allowance.FOLD_BATCH:
                # Refills fold too, but a chunk of cheap calls lasts a long time between
                # refills, and the queue must not wait for one.
                count = 0
                _fold_in_background(charge.org_id)
            self._since_fold[charge.org_id] = count

    @staticmethod
    async def _write(rows: list, *, again: bool = False) -> set[int]:
        """Write the rows; return the teams with a charge among them that its own hold no
        longer stands behind (closed since, with or without another in its place)."""
        teams = sorted({charge.org_id for charge, _ in rows if charge.epoch is not None})
        # Bounded like every money transaction: this one holds teams' hold rows, and a process
        # that stalled here would keep their refills, folds and sweeps waiting.
        async with money_session(session_maker()) as db:
            # PostgreSQL: the shared lock on the hold rows comes first, the order a team's
            # deletion uses too. SQLite has no row locks and begins its transaction only at
            # the first write, so there the insert comes first and the holds are read under
            # the write lock it took.
            row_locks = db.get_bind().dialect.name == "postgresql"
            holds = await allowance.holds_behind(db, teams) if row_locks else {}
            new = await allowance.record_charges(db, [charge for charge, _ in rows])
            if not row_locks:
                holds = await allowance.holds_behind(db, teams)
            # A row that was already there was committed by an earlier attempt, which marked its
            # paid question too. On a rewrite, so was a row that is new only because the earlier
            # attempt's row has been applied and deleted since: its call has a settle entry.
            if again and new:
                new -= await allowance.settled_calls(db, sorted(new))
            uses = [use for charge, use in rows if use is not None and charge.call_id in new]
            if uses:
                await archive.note_org_uses_in_transaction(db, uses)
            await db.commit()
        return {charge.org_id for charge, _ in rows
                if charge.epoch is not None and holds.get(charge.org_id) != charge.epoch}


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


async def finish(org_id: int, call_id: str, epoch: int, reserved_micro: int, *,
                 endpoint_id: str, billable: bool, actual_micro: int | None = None,
                 meta: dict | None = None, archive_use: tuple[int, str] | None = None) -> int:
    """End one take, once: charge the call and give back the rest, or give back all of it.
    Returns what was charged. Never raises: the call already has its answer.

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
            return 0
        charge = allowance.Charge(
            call_id, org_id, endpoint_id, actual_micro, meta, reserved_micro, epoch)
        written = _charges.submit(charge, archive_use)
        amount = allowance.charged_micro(charge)
        if amount < reserved_micro:
            await store.allowance_give_back(org_id, epoch, reserved_micro - amount)
        # Unconfirmed is not "not charged": the charge may have committed, and the writer keeps
        # trying until it has. Its money stays out of the counter either way. An overrun freezes
        # the counter, and the writer does that once the charge is committed.
        if not await written:
            return amount
        if amount <= reserved_micro:
            state = await store.allowance_state(org_id)
            if state is None or state[1] == state[2]:
                return amount
        # Frozen: some settlement of the team took more than was set aside for it, and the
        # team's credit may not cover every charge. Apply the queue now and answer with what the
        # ledger took from the team for this call, less than the charge when the credit ran out.
        return await _settled(org_id, call_id, amount)
    except Exception:
        log.error("finishing allowance take %s failed", call_id, exc_info=True)
        return amount


async def _settled(org_id: int, call_id: str, amount: int) -> int:
    """What the call's settle entry took, folding the team's queue to get it written. Another
    process may hold the team's lease; it is folding too, so wait for the entry a bounded time.
    `amount`, the charge, when the entry is not there by then or cannot be read: the charge is
    written, and "nothing" would be the one answer known to be wrong."""
    for _ in range(_SETTLED_TRIES):
        await _fold_backlog(org_id)
        try:
            async with session_maker() as db:
                settled = await allowance.settled_micro(db, org_id, call_id)
        except Exception:
            log.error("reading the settle entry of allowance call %s failed", call_id,
                      exc_info=True)
            return amount
        if settled is not None:
            return settled
        await asyncio.sleep(0.05)
    log.warning("allowance call %s was answered before its charge was applied", call_id)
    return amount
