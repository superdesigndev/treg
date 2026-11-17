"""A team's allowance: money set aside once for many calls, and the charges drawn against it.

The allowance is ONE `Hold` row per team, marked `allowance`. It is opened and topped up by
`reserve` entries and closed by a `release` entry, so the balance invariant counts it like any
other hold. A call that spends from it records a `CallCharge`; folding applies recorded charges
to the credit blocks and writes each call's `settle` entry. Nothing here commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from sqlalchemy import delete
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...config import get_settings
from ...infra.money_trace import mark_money, money_stage
from ...models import CallCharge, Hold, LedgerEntry
from . import _add_balance, _consume_blocks, _day_start, _entry, _lock_blocks, _now, with_margin

FOLD_BATCH = 500


def allowance_id(org_id: int) -> str:
    """The id of a team's one allowance hold."""
    return f"allowance:{org_id}"


class Charge(NamedTuple):
    """What one finished call cost. `actual_micro` is the raw provider cost; margin is applied here."""

    call_id: str
    org_id: int
    endpoint_id: str
    actual_micro: int
    meta: dict | None = None


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
        "amount_micro": max(0, with_margin(int(charge.actual_micro))),
        "meta": {**(charge.meta or {}), "observed_micro": int(charge.actual_micro), "margin": margin},
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
    overrun does at `settle`. Must run before any other money write in the transaction.
    """
    mark_money(db, "fold", org_id=org_id)
    return await _fold(db, org_id, await _lock_allowance(db, org_id), limit)


async def _fold(db: AsyncSession, org_id: int, hold: Hold | None, limit: int) -> Folded:
    with money_stage(db, "claim_charges"):
        claimed = (await db.execute(
            delete(CallCharge).where(CallCharge.call_id.in_(
                select(CallCharge.call_id).where(CallCharge.org_id == org_id).limit(limit)))
            .returning(CallCharge.call_id, CallCharge.endpoint_id, CallCharge.amount_micro,
                       CallCharge.meta, CallCharge.created_at))).all()
    if not claimed:
        return Folded()
    # A queue row is gone once folded, so its primary key no longer stops the same call being
    # recorded again. The call's settle entry is the lasting record that it was charged.
    with money_stage(db, "settled_charges"):
        settled = set((await db.execute(select(LedgerEntry.call_id).where(
            LedgerEntry.call_id.in_([row.call_id for row in claimed]),
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
