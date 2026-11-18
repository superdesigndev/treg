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
# Long enough that a counter outlives any pause in refills that still leaves its hold open.
_COUNTER_TTL_MS = 24 * 3600 * 1000
_refused_until: dict[int, float] = {}
_refills: dict[int, int] = {}


def refills(org_id: int) -> int:
    """How many refills this process has committed for the team: a caller compares it around a
    take to learn that the balance moved."""
    return _refills.get(org_id, 0)


def enabled_for(org_id: int) -> bool:
    return org_id in get_settings().money_allowance_org_ids and kv.configured()


def _refill_key(org_id: int) -> str:
    return f"treg:money-allowance-refill:v1:{org_id}"


async def take(org_id: int, amount_micro: int, *, daily_cap_micro: int = 0) -> int | None:
    """Reserve `amount_micro` from the team's allowance. The counter's epoch, or None when the
    call must use the exact path: the store cannot answer, another process holds the refill for
    too long, or the balance or the daily cap leaves no room to refill."""
    if _refused_until.get(org_id, 0.0) > time.monotonic():
        return None
    store = kv.store()
    epoch = await store.allowance_take(org_id, amount_micro)
    if epoch or epoch is None:
        return epoch
    key, token = _refill_key(org_id), uuid.uuid4().hex
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
    available = await store.allowance_available(org_id)
    if available is None:
        return False
    add = max(0, max(settings.money_allowance_chunk_micro, need_micro) - available)
    try:
        async with (
            money_admission.admit(
                money_admission.require_balance_orgs([org_id]), operation="reserve"),
            money_session(session_maker()) as db,
        ):
            await ledger.reap_stale_holds(db, org_id=org_id)
            result = await allowance.refill_allowance_in_transaction(
                db, org_id, add, daily_cap_micro=daily_cap_micro)
            await db.commit()
    except money_admission.ReserveAdmissionTimeout:
        return False
    _refills[org_id] = _refills.get(org_id, 0) + 1
    log.info("allowance_refill org_id=%s added=%s hold=%s counter_before=%s folded=%s duplicates=%s",
             org_id, result.added_micro, result.amount_micro, available,
             result.folded.calls, result.folded.duplicates)
    if result.epoch is None or result.over_cap or available + result.added_micro < need_micro:
        _refused_until[org_id] = time.monotonic() + _REFUSAL_COOLDOWN_S
        if result.epoch is not None:
            await _drain(org_id, "no_room_to_refill")
        return False
    added = await store.allowance_add(
        org_id, result.epoch, result.added_micro,
        fresh_ms=settings.money_allowance_fresh_s * 1000, ttl_ms=_COUNTER_TTL_MS)
    return isinstance(added, int)


async def _drain(org_id: int, reason: str) -> None:
    """Empty the counter and return what it held to the balance. The hold stays, with what calls
    already admitted may still charge."""
    store = kv.store()
    left = await store.allowance_drop(org_id)
    if not left:
        return
    async with (
        money_admission.admit(money_admission.require_balance_orgs([org_id]), operation="release"),
        money_session(session_maker()) as db,
    ):
        await allowance.close_allowance_in_transaction(
            db, org_id, reason=reason, release_micro=left)
        await db.commit()


class _ChargeWriter:
    """Queues finished calls' charges and inserts them a batch at a time.

    One insert per call would put a transaction back on every call. Calls that finish within
    the linger share one, and each still waits for its batch to commit before it answers.
    """

    def __init__(self) -> None:
        self._pending: list[tuple[allowance.Charge, tuple[int, str] | None, asyncio.Future]] = []
        self._task: asyncio.Task | None = None

    def submit(self, charge: allowance.Charge,
               archive_use: tuple[int, str] | None) -> asyncio.Future:
        """Queue one charge now, with no await: a call cancelled right after still pays. The
        future resolves True once the charge is committed, False if the write failed."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending.append((charge, archive_use, future))
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._task = loop.create_task(self._run(), name="treg-allowance-charges")
        return future

    async def _run(self) -> None:
        while self._pending:
            await asyncio.sleep(get_settings().money_allowance_linger_ms / 1000)
            batch = self._pending[:allowance.FOLD_BATCH]
            del self._pending[:len(batch)]
            written = True
            try:
                try:
                    await self._write(batch)
                except Exception:  # noqa: BLE001 - one bounded retry on a fresh transaction
                    await asyncio.sleep(0.2)
                    await self._write(batch)
            except Exception:
                written = False
                log.error("writing %d call charges failed; they are not charged", len(batch),
                          exc_info=True)
            for _, _, future in batch:
                if not future.done():
                    future.set_result(written)

    @staticmethod
    async def _write(batch: list) -> None:
        async with session_maker() as db:
            await allowance.record_charges(db, [charge for charge, _, _ in batch])
            uses = [use for _, use, _ in batch if use is not None]
            if uses:
                await archive.note_org_uses_in_transaction(db, uses)
            await db.commit()


_charges = _ChargeWriter()


async def finish(org_id: int, call_id: str, epoch: int, reserved_micro: int, *,
                 endpoint_id: str, billable: bool, actual_micro: int | None = None,
                 meta: dict | None = None, archive_use: tuple[int, str] | None = None) -> int:
    """End one take, once: charge the call and give back the rest, or give back all of it.
    Returns what was charged. Never raises: the call already has its answer.

    The charge is queued before anything is awaited, so a cancel in the middle cannot lose it.
    A give-back that does not arrive leaves that money in the hold until a close returns it.
    """
    store = kv.store()
    try:
        if not billable:
            await store.allowance_give_back(org_id, epoch, reserved_micro)
            return 0
        amount = reserved_micro if actual_micro is None else max(
            0, ledger.with_margin(int(actual_micro)))
        written = _charges.submit(allowance.Charge(
            call_id, org_id, endpoint_id, actual_micro, meta, reserved_micro), archive_use)
        if amount < reserved_micro:
            await store.allowance_give_back(org_id, epoch, reserved_micro - amount)
        elif amount > reserved_micro:
            # The call cost more than it set aside. Take the difference too, so the counter does
            # not go on offering money this charge will draw from the hold. Best effort.
            await store.allowance_take(org_id, amount - reserved_micro)
        if await written:
            return amount
        await store.allowance_give_back(org_id, epoch, min(amount, reserved_micro))
        return 0
    except Exception:
        log.error("finishing allowance take %s failed", call_id, exc_info=True)
        return 0
