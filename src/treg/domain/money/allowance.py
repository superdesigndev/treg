"""A team's allowance: money set aside once for many calls, and the charges drawn against it.

The allowance is ONE `Hold` row per team, marked `allowance`. It is opened and topped up by
`reserve` entries and closed by a `release` entry, so the balance invariant counts it like any
other hold. A call that spends from it records a `CallCharge`; folding applies recorded charges
to the credit blocks and writes each call's `settle` entry. Nothing here commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import delete, func, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...config import get_settings
from ...infra.money_trace import mark_money, money_stage
from ...models import CallCharge, Hold, LedgerEntry, Org
from . import (
    _add_balance,
    _consume_blocks,
    _day_start,
    _entry,
    _lock_blocks,
    _now,
    _spent_today_values,
    spent_today,
    with_margin,
)

FOLD_BATCH = 500


def allowance_id(org_id: int) -> str:
    """The id of a team's one allowance hold."""
    return f"allowance:{org_id}"


class Charge(NamedTuple):
    """What one finished call cost. `actual_micro` is the raw provider cost; margin is applied
    here. `None` means the real cost was never learned and charges `reserved_micro`, what the call
    set aside with margin already in it: the rule `settle` follows."""

    call_id: str
    org_id: int
    endpoint_id: str
    actual_micro: int | None
    meta: dict | None = None
    reserved_micro: int = 0


async def record_charges(db: AsyncSession, charges: Sequence[Charge]) -> int:
    """Queue finished calls' costs in the caller-owned transaction; return how many were new.

    One statement for the whole batch. A `call_id` already queued is skipped, so recording a call
    twice charges it once.
    """
    if not charges:
        return 0
    margin = get_settings().platform_margin
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    rows = {charge.call_id: {
        "call_id": charge.call_id, "org_id": charge.org_id, "endpoint_id": charge.endpoint_id or "",
        "amount_micro": max(0, int(charge.reserved_micro) if charge.actual_micro is None
                            else with_margin(int(charge.actual_micro))),
        "meta": {**(charge.meta or {}), "margin": margin,
                 "observed_micro": None if charge.actual_micro is None else int(charge.actual_micro)},
    } for charge in charges}
    result = await db.execute(
        insert(CallCharge).values(list(rows.values()))
        .on_conflict_do_nothing(index_elements=[CallCharge.call_id]))
    return int(result.rowcount)


class Folded(NamedTuple):
    """One fold: `calls` settle entries written for `charged_micro`, `consumed_micro` of it drawn
    from blocks, and `duplicates` charges dropped because their call was already settled."""

    calls: int = 0
    charged_micro: int = 0
    consumed_micro: int = 0
    duplicates: int = 0


async def _lock_allowance(db: AsyncSession, org_id: int) -> Hold | None:
    # The first lock every allowance operation takes, before blocks and the org row: the order
    # settle uses, and what makes two folds of one team run one after the other.
    with money_stage(db, "lock_allowance"):
        return (await db.execute(
            select(Hold).where(Hold.id == allowance_id(org_id)).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()


async def fold_charges_in_transaction(db: AsyncSession, org_id: int, *,
                                      limit: int = FOLD_BATCH) -> Folded:
    """Apply up to `limit` of a team's queued charges to its blocks, in the caller's transaction.

    Each charge becomes that call's `settle` entry, stamped with the fold's instant, and its
    queue row is deleted. The charged total comes out of the allowance hold; whatever the hold
    cannot cover (no hold, or one already drained) comes straight out of the balance, as an
    overrun does at `settle`. Must run before any other money write in the transaction, and
    once per transaction: a second fold would lock the blocks again (see `_apply_charges`).
    """
    mark_money(db, "fold", org_id=org_id)
    return await _fold(db, org_id, await _lock_allowance(db, org_id), limit)


async def _claim_charges(db: AsyncSession, org_id: int, limit: int) -> list:
    with money_stage(db, "claim_charges"):
        return list((await db.execute(
            delete(CallCharge).where(CallCharge.call_id.in_(
                select(CallCharge.call_id).where(CallCharge.org_id == org_id).limit(limit)))
            .returning(CallCharge.call_id, CallCharge.endpoint_id, CallCharge.amount_micro,
                       CallCharge.meta, CallCharge.created_at))).all())


async def _fold(db: AsyncSession, org_id: int, hold: Hold | None, limit: int) -> Folded:
    return await _apply_charges(db, org_id, hold, await _claim_charges(db, org_id, limit))


async def _apply_charges(db: AsyncSession, org_id: int, hold: Hold | None, claimed: list) -> Folded:
    """Charge everything claimed, locking the team's blocks ONCE.

    A transaction that locked the blocks again later could meet one funded in between, out of
    primary-key order, and deadlock with a settle that already holds it. So a transaction
    applies charges once, whatever number of batches it claimed them in.
    """
    if not claimed:
        return Folded()
    # A queue row is gone once folded, so its primary key no longer stops the same call being
    # recorded again. The call's settle entry is the lasting record that it was charged.
    settled: set[str] = set()
    ids = [row.call_id for row in claimed]
    with money_stage(db, "settled_charges"):
        for start in range(0, len(ids), FOLD_BATCH):
            settled.update((await db.execute(select(LedgerEntry.call_id).where(
                LedgerEntry.call_id.in_(ids[start:start + FOLD_BATCH]),
                LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle"))).scalars())
    charges = sorted((row for row in claimed if row.call_id not in settled),
                     key=lambda row: (row.created_at, row.call_id))
    charged = sum(row.amount_micro for row in charges)
    blocks = await _lock_blocks(db, {org_id}) if charged > 0 else []
    folded_at = _now()
    consumed = 0
    for row in charges:
        drawn, shortfall = await _consume_blocks(
            db, org_id, row.amount_micro, row.call_id, row.endpoint_id, blocks=blocks)
        consumed += drawn
        await _entry(
            db, org_id=org_id, kind="settle", amount_micro=-drawn, call_id=row.call_id,
            endpoint_id=row.endpoint_id, created_at=folded_at,
            meta={**(row.meta or {}), "allowance": True, "settled_micro": row.amount_micro,
                  "consumed_micro": drawn, "block_shortfall_micro": shortfall,
                  "charged_at": row.created_at.isoformat()})
    covered = min(charged, hold.amount_micro) if hold is not None else 0
    if covered:
        hold.amount_micro -= covered
    # The daily counter holds what settled today plus holds opened today, so the covered part
    # leaves it only when this hold was counted today.
    counted = covered if hold is not None and hold.created_at >= _day_start() else 0
    if covered != consumed or consumed != counted:
        await _add_balance(db, org_id, covered - consumed, spent_delta_micro=consumed - counted)
    return Folded(len(charges), charged, consumed, len(claimed) - len(charges))


class Allowance(NamedTuple):
    """A team's allowance after a refill. `epoch` names this hold row for as long as it exists
    (None when the team has no allowance); a counter kept outside the database carries it, so
    one built for an earlier row is never mistaken for this one's."""

    epoch: int | None
    amount_micro: int
    added_micro: int
    folded: Folded
    # The team's daily cap is already reached by what it has spent today plus what this
    # allowance can still spend: the caller must stop offering the allowance.
    over_cap: bool = False


def _epoch(hold: Hold) -> int:
    return (hold.created_at - datetime(1970, 1, 1)) // timedelta(microseconds=1)


def _uncounted(hold: Hold | None) -> int:
    """What an allowance hold can still spend today without today's counter knowing: all of
    it when the hold was opened on an earlier day, since such a hold and its top-ups are not
    in the counter until a fold puts what was consumed there."""
    return hold.amount_micro if hold is not None and hold.created_at < _day_start() else 0


async def uncounted_allowance_micro(db: AsyncSession, org_id: int) -> int:
    """`_uncounted` for a team's allowance, for a daily-cap check made outside a refill."""
    row = (await db.execute(select(Hold.amount_micro, Hold.created_at).where(
        Hold.id == allowance_id(org_id)))).one_or_none()
    return int(row[0]) if row is not None and row[1] < _day_start() else 0


async def refill_allowance_in_transaction(
    db: AsyncSession, org_id: int, add_micro: int, *, daily_cap_micro: int = 0,
    fold_limit: int = FOLD_BATCH,
) -> Allowance:
    """Fold a batch of queued charges, then move `add_micro` from the balance into the team's
    allowance hold, opening the hold if there is none. In the caller's transaction, before any
    other money write.

    The move is `reserve`'s conditional UPDATE and writes a `reserve` entry. A balance that
    cannot cover it adds nothing and raises nothing: the fold still stands, and `added_micro`
    of 0 tells the caller to stop spending from the allowance. `add_micro` is money, not a
    provider cost: no margin is applied. Stamps the hold's heartbeat.

    With `daily_cap_micro`, nothing is added that could take the day past the cap. The day's
    worst case is read AFTER the fold, under the hold's lock: what today's counter holds, plus
    all of a hold opened on an earlier day (`_uncounted`), plus the amount asked for. `over_cap`
    reports that the first two alone already exceed the cap.
    """
    mark_money(db, "refill", org_id=org_id)
    add = max(0, int(add_micro))
    hold = await _lock_allowance(db, org_id)
    opened = False
    if hold is None and add:
        # Two callers can both find no row. The insert decides which one opens it, and the
        # other waits on the lock below and tops up the row the first one made.
        insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
        now = _now()
        opened = (await db.execute(insert(Hold).values(
            id=allowance_id(org_id), org_id=org_id, endpoint_id="", amount_micro=0,
            created_at=now, purchased_only=False, allowance=True, heartbeat_at=now,
        ).on_conflict_do_nothing(index_elements=[Hold.id]))).rowcount == 1
        hold = await _lock_allowance(db, org_id)
    folded = await _fold(db, org_id, hold, fold_limit)
    over_cap = False
    if daily_cap_micro > 0:
        exposed = await spent_today(db, org_id) + _uncounted(hold)
        over_cap = exposed > daily_cap_micro
        if exposed + add > daily_cap_micro:
            add = 0
    if hold is None:
        return Allowance(None, 0, 0, folded, over_cap)
    added = 0
    if add:
        counted = _spent_today_values(add) if hold.created_at >= _day_start() else {}
        with money_stage(db, "reserve_balance"):
            result = await db.execute(
                update(Org).where(Org.id == org_id, Org.balance_micro >= add)
                .values(balance_micro=Org.balance_micro - add, **counted))
        if result.rowcount == 1:
            added = add
            hold.amount_micro += add
            await _entry(db, org_id=org_id, kind="reserve", amount_micro=-add, call_id=hold.id,
                         meta={"allowance": True, "charged_micro": add,
                               "hold_micro": hold.amount_micro})
    if opened and not added:
        await db.delete(hold)
        return Allowance(None, 0, 0, folded, over_cap)
    hold.heartbeat_at = _now()
    return Allowance(_epoch(hold), hold.amount_micro, added, folded, over_cap)


class Closed(NamedTuple):
    """`released_micro` went back to the balance; `remaining_micro` is still in the hold (0 once
    the hold is gone). `epoch` names the hold that was closed, None when there was none."""

    released_micro: int
    remaining_micro: int
    folded: Folded
    epoch: int | None = None


async def close_allowance_in_transaction(
    db: AsyncSession, org_id: int, *, reason: str, release_micro: int | None = None,
) -> Closed:
    """Fold every queued charge, then return allowance money to the balance with a `release`
    entry. In the caller's transaction, before any other money write.

    `release_micro=None` returns all of it and deletes the hold. Charges recorded after that find
    no hold and come straight out of the balance, so use it only once no call can still be
    spending from the allowance. A number returns at most that much and keeps the hold: the
    way to stop new spending while calls already admitted finish against what is left.
    """
    mark_money(db, "close_allowance", org_id=org_id)
    return await _close(db, org_id, await _lock_allowance(db, org_id), reason, release_micro)


async def close_stale_allowance_in_transaction(
    db: AsyncSession, org_id: int, *, stale_before: datetime, reason: str,
) -> Closed | None:
    """Close a team's allowance in full if no refill has stamped it since `stale_before`; None,
    and nothing touched, when there is no hold or it is fresher. The check is made under the
    hold's lock, so a refill that lands first keeps the hold. The caller picks `stale_before`
    early enough that no call can still be spending from the allowance.
    """
    mark_money(db, "close_allowance", org_id=org_id)
    hold = await _lock_allowance(db, org_id)
    if hold is None or (hold.heartbeat_at or hold.created_at) >= stale_before:
        return None
    return await _close(db, org_id, hold, reason, None)


async def stale_allowance_orgs(db: AsyncSession, stale_before: datetime, *,
                               limit: int = 50) -> list[int]:
    """Teams whose allowance hold was last stamped before `stale_before`. A read; the close
    re-checks under the lock."""
    return list((await db.execute(
        select(Hold.org_id).where(
            Hold.allowance.is_(True),
            func.coalesce(Hold.heartbeat_at, Hold.created_at) < stale_before)
        .order_by(Hold.org_id).limit(limit))).scalars())


async def _close(db: AsyncSession, org_id: int, hold: Hold | None, reason: str,
                 release_micro: int | None) -> Closed:
    claimed: list = []
    while True:
        batch = await _claim_charges(db, org_id, FOLD_BATCH)
        claimed.extend(batch)
        if len(batch) < FOLD_BATCH:
            break
    folded = await _apply_charges(db, org_id, hold, claimed)
    if hold is None:
        return Closed(0, 0, folded)
    released = hold.amount_micro if release_micro is None else min(
        max(0, int(release_micro)), hold.amount_micro)
    if released:
        hold.amount_micro -= released
        counted = -released if hold.created_at >= _day_start() else 0
        await _add_balance(db, org_id, released, spent_delta_micro=counted)
        await _entry(db, org_id=org_id, kind="release", amount_micro=released, call_id=hold.id,
                     meta={"allowance": True, "reason": reason, "hold_micro": hold.amount_micro})
    remaining = hold.amount_micro
    if release_micro is None:
        await db.delete(hold)
    return Closed(released, remaining, folded, _epoch(hold))
