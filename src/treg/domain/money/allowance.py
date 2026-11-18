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
    with_margin,
)

FOLD_BATCH = 500


def allowance_id(org_id: int) -> str:
    """The id of a team's one allowance hold."""
    return f"allowance:{org_id}"


class Charge(NamedTuple):
    """What one finished call cost. `actual_micro` is the raw provider cost; margin is applied
    here. `None` means the real cost was never learned and charges `reserved_micro`, what the call
    set aside with margin already in it: the rule `settle` follows.

    With `reserved_micro`, whatever the charge exceeds it by is the call's overrun: the hold
    never held that part for this call. Without it the whole charge counts as set aside."""

    call_id: str
    org_id: int
    endpoint_id: str
    actual_micro: int | None
    meta: dict | None = None
    reserved_micro: int | None = None


def charged_micro(charge: Charge) -> int:
    """What the team owes for `charge`, margin applied."""
    return max(0, int(charge.reserved_micro or 0) if charge.actual_micro is None
               else with_margin(int(charge.actual_micro)))


async def record_charges(db: AsyncSession, charges: Sequence[Charge]) -> set[str]:
    """Queue finished calls' costs in the caller-owned transaction; return the `call_id`s that
    were new.

    One statement for the whole batch. A `call_id` already queued is skipped, so recording a call
    twice charges it once.
    """
    if not charges:
        return set()
    margin = get_settings().platform_margin
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    rows = {}
    for charge in charges:
        amount = charged_micro(charge)
        rows[charge.call_id] = {
            "call_id": charge.call_id, "org_id": charge.org_id,
            "endpoint_id": charge.endpoint_id or "", "amount_micro": amount,
            "overrun_micro": (0 if charge.reserved_micro is None
                              else max(0, amount - int(charge.reserved_micro))),
            "meta": {**(charge.meta or {}), "margin": margin,
                     "observed_micro": (None if charge.actual_micro is None
                                        else int(charge.actual_micro))},
        }
    result = await db.execute(
        insert(CallCharge).values(list(rows.values()))
        .on_conflict_do_nothing(index_elements=[CallCharge.call_id])
        .returning(CallCharge.call_id))
    return set(result.scalars())


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
    queue row is deleted. What the calls set aside comes out of the allowance hold; their
    overruns, and whatever the hold cannot cover (no hold, or one already drained), come
    straight out of the balance, as an overrun does at `settle`. Must run before any other money write in the transaction, and
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
                       CallCharge.meta, CallCharge.created_at, CallCharge.overrun_micro))).all())


async def _claim_all_charges(db: AsyncSession, org_id: int) -> list:
    claimed: list = []
    while True:
        batch = await _claim_charges(db, org_id, FOLD_BATCH)
        claimed.extend(batch)
        if len(batch) < FOLD_BATCH:
            return claimed


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
    # The hold pays for what each call set aside and no more. A call's overrun comes out of the
    # balance, as it does at `settle`: the rest of the hold belongs to calls still in flight.
    set_aside = charged - sum(row.overrun_micro for row in charges)
    covered = min(set_aside, hold.amount_micro) if hold is not None else 0
    if covered:
        hold.amount_micro -= covered
    # The daily counter holds what settled today plus holds opened today, so the covered part
    # leaves it only when this hold was counted today.
    counted = covered if hold is not None and hold.created_at >= _day_start() else 0
    if covered != consumed or consumed != counted:
        await _add_balance(db, org_id, covered - consumed, spent_delta_micro=consumed - counted)
    return Folded(len(charges), charged, consumed, len(claimed) - len(charges))


async def settled_calls(db: AsyncSession, call_ids: Sequence[str]) -> set[str]:
    """Which of these calls already have a `settle` entry."""
    if not call_ids:
        return set()
    return set((await db.execute(select(LedgerEntry.call_id).where(
        LedgerEntry.call_id.in_(list(call_ids)), LedgerEntry.kind == "settle"))).scalars())


async def settled_micro(db: AsyncSession, org_id: int, call_id: str) -> int | None:
    """What a call's `settle` entry took from the team's credit, or None while it has none."""
    amount = (await db.execute(select(LedgerEntry.amount_micro).where(
        LedgerEntry.call_id == call_id, LedgerEntry.org_id == org_id,
        LedgerEntry.kind == "settle"))).scalars().first()
    return None if amount is None else -int(amount)


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
    # The balance is below zero after the fold (a call cost more than it set aside and the team
    # had no more): nothing was added, and the caller must stop offering the allowance so the
    # exact path's balance gate decides, as it does after an overrun at `settle`.
    overdrawn: bool = False
    # The instant written on the hold until which calls may be admitted against the allowance.
    # The caller publishes exactly this, however late it gets round to it.
    admit_until: datetime | None = None


def _epoch(hold: Hold) -> int:
    return (hold.created_at - datetime(1970, 1, 1)) // timedelta(microseconds=1)


async def committed_today(db: AsyncSession, org_id: int) -> int:
    """The most a team can turn out to have spent today from what is already committed: the
    number a daily cap is checked against. One statement.

    Today's counter (`spent_today`) holds what settled today and every hold opened today. Three
    things can still be spent today that it does not hold: an allowance hold opened on an
    earlier day, all of it; the overruns of queued charges, which no hold was ever set aside
    for; and what queued charges set aside beyond what the hold has left (no hold, or one already
    drained). All are read from the rows themselves, so the figure is right whether or not the
    team's allowance is switched on. A queued charge whose call already has a `settle` entry is
    left out, as a fold will drop it.
    """
    hold_id = allowance_id(org_id)
    unsettled = (CallCharge.org_id == org_id, ~select(LedgerEntry.id).where(
        LedgerEntry.call_id == CallCharge.call_id, LedgerEntry.org_id == org_id,
        LedgerEntry.kind == "settle").exists())
    row = (await db.execute(select(
        Org.spent_today_micro, Org.spent_today_day,
        select(Hold.amount_micro).where(Hold.id == hold_id).scalar_subquery(),
        select(Hold.created_at).where(Hold.id == hold_id).scalar_subquery(),
        select(func.coalesce(func.sum(CallCharge.amount_micro - CallCharge.overrun_micro), 0))
        .where(*unsettled).scalar_subquery(),
        select(func.coalesce(func.sum(CallCharge.overrun_micro), 0))
        .where(*unsettled).scalar_subquery(),
    ).where(Org.id == org_id))).one_or_none()
    if row is None:
        return 0
    spent = int(row[0]) if row[1] == _now().date() else 0
    held, set_aside, overruns = int(row[2] or 0), int(row[4] or 0), int(row[5] or 0)
    earlier_day = held if row[3] is not None and row[3] < _day_start() else 0
    return spent + earlier_day + overruns + max(0, set_aside - held)


async def refill_allowance_in_transaction(
    db: AsyncSession, org_id: int, add_micro: int, *, daily_cap_micro: int = 0,
    fold_limit: int = FOLD_BATCH, fold_all: bool = False,
) -> Allowance:
    """Fold a batch of queued charges, then move `add_micro` from the balance into the team's
    allowance hold, opening the hold if there is none. In the caller's transaction, before any
    other money write.

    The move is `reserve`'s conditional UPDATE and writes a `reserve` entry. A balance that
    cannot cover it adds nothing and raises nothing: the fold still stands, and `added_micro`
    of 0 tells the caller to stop spending from the allowance. `add_micro` is money, not a
    provider cost: no margin is applied. Writes on the hold (`heartbeat_at`) the instant until
    which calls may be admitted against it: now plus `money_allowance_fresh_s`.

    `fold_all` folds the whole queue instead of one batch. A caller that must know the balance
    with every queued charge applied (it is about to let calls spend again after an overrun)
    passes it: `overdrawn` is only as good as the charges that were folded before it was read.

    With `daily_cap_micro`, nothing is added that could take the day past the cap. The day's
    worst case (`committed_today`) is read AFTER the fold, under the hold's lock, and the amount
    asked for is added to it. `over_cap` reports that it exceeds the cap before adding anything.
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
    folded = (await _apply_charges(db, org_id, hold, await _claim_all_charges(db, org_id))
              if fold_all else await _fold(db, org_id, hold, fold_limit))
    over_cap = False
    if daily_cap_micro > 0:
        exposed = await committed_today(db, org_id)
        over_cap = exposed > daily_cap_micro
        if exposed + add > daily_cap_micro:
            add = 0
    balance = (await db.execute(select(Org.balance_micro).where(Org.id == org_id))).scalar() or 0
    overdrawn = balance < 0
    if overdrawn:
        add = 0
    if hold is None:
        return Allowance(None, 0, 0, folded, over_cap, overdrawn)
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
        return Allowance(None, 0, 0, folded, over_cap, overdrawn)
    # Never earlier than one already published: the store keeps the latest too, and a sweep
    # must be able to trust this row whatever freshness the process that wrote it was running.
    admit_until = _now() + timedelta(seconds=get_settings().money_allowance_fresh_s)
    hold.heartbeat_at = max(hold.heartbeat_at or admit_until, admit_until)
    return Allowance(_epoch(hold), hold.amount_micro, added, folded, over_cap, overdrawn,
                     hold.heartbeat_at)


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
    """Close a team's allowance in full if it stopped admitting calls before `stale_before`;
    None, and nothing touched, when there is no hold or it admitted calls later than that. The
    check is made under the hold's lock, so a refill that lands first keeps the hold. The
    caller picks `stale_before` at least one call's lifetime back, so no call admitted against
    the allowance can still be running.
    """
    mark_money(db, "close_allowance", org_id=org_id)
    hold = await _lock_allowance(db, org_id)
    if hold is None or (hold.heartbeat_at or hold.created_at) >= stale_before:
        return None
    return await _close(db, org_id, hold, reason, None)


async def stale_allowance_orgs(db: AsyncSession, stale_before: datetime, *,
                               limit: int = 50) -> list[int]:
    """Teams whose allowance hold stopped admitting calls before `stale_before`. A read; the
    close re-checks under the lock."""
    return list((await db.execute(
        select(Hold.org_id).where(
            Hold.allowance.is_(True),
            func.coalesce(Hold.heartbeat_at, Hold.created_at) < stale_before)
        .order_by(Hold.org_id).limit(limit))).scalars())


async def orgs_with_queued_charges(db: AsyncSession, queued_before: datetime, *,
                                   limit: int = 50) -> list[int]:
    """Teams with a charge queued since before `queued_before`: a queue nothing is folding."""
    return list((await db.execute(
        select(CallCharge.org_id).where(CallCharge.created_at < queued_before)
        .distinct().order_by(CallCharge.org_id).limit(limit))).scalars())


async def _close(db: AsyncSession, org_id: int, hold: Hold | None, reason: str,
                 release_micro: int | None) -> Closed:
    folded = await _apply_charges(db, org_id, hold, await _claim_all_charges(db, org_id))
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
