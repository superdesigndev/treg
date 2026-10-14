"""Per-aggregator daily overflow accounting (`OverflowSpend`).

The child cycle atomically reserves estimated spend before network I/O, then reconciles the row to
actual aggregator cost after the attempt. This makes the configured daily budget a concurrency-safe
admission cap rather than a read-before-write hint.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import OverflowSpend
from ...timeutil import utcnow_naive


def utc_day(now: datetime | None = None) -> str:
    return (now or utcnow_naive()).strftime("%Y-%m-%d")


def _insert_for(db: AsyncSession):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise RuntimeError(f"unsupported database dialect: {dialect}")


async def reserve_in_transaction(
    db: AsyncSession, aggregator: str, estimate_micro: int, cap_micro: int,
    *, day: str | None = None,
) -> OverflowSpend | None:
    """Atomically reserve estimated aggregator spend if it fits under the daily cap.

    The reservation does not count a call. The completed attempt reconciles estimate to actual and
    increments calls through `add_in_transaction`. Does NOT commit: the caller owns the short
    transaction. A process crash after its caller commits leaves the estimate in place, which is a
    conservative bias toward serving less rather than exceeding the prepaid-account guardrail.
    """
    estimate_micro = int(estimate_micro)
    cap_micro = int(cap_micro)
    if estimate_micro < 0 or cap_micro < 0:
        raise ValueError("overflow budget values must be non-negative micro-USD")
    if estimate_micro > cap_micro:
        return None
    day = day or utc_day()
    now = utcnow_naive()
    statement = _insert_for(db)(OverflowSpend).values(
        aggregator=aggregator,
        day=day,
        calls=0,
        cost_micro=estimate_micro,
        delta_micro=0,
        updated_at=now,
    ).on_conflict_do_update(
        index_elements=[OverflowSpend.aggregator, OverflowSpend.day],
        set_={
            "cost_micro": OverflowSpend.cost_micro + estimate_micro,
            "updated_at": now,
        },
        where=OverflowSpend.cost_micro + estimate_micro <= cap_micro,
    ).returning(OverflowSpend).execution_options(populate_existing=True)
    return (await db.execute(statement)).scalar_one_or_none()


async def release_reservation_in_transaction(
    db: AsyncSession, aggregator: str, estimate_micro: int, *, day: str | None = None,
) -> OverflowSpend | None:
    """Return an unconsumed estimate without counting a call. Does NOT commit."""
    statement = update(OverflowSpend).where(
        OverflowSpend.aggregator == aggregator,
        OverflowSpend.day == (day or utc_day()),
    ).values(
        cost_micro=OverflowSpend.cost_micro - int(estimate_micro),
        updated_at=utcnow_naive(),
    ).returning(OverflowSpend).execution_options(populate_existing=True)
    return (await db.execute(statement)).scalar_one_or_none()


async def add_in_transaction(db: AsyncSession, aggregator: str, cost_micro: int, delta_micro: int,
                             *, day: str | None = None) -> OverflowSpend:
    """Add one call to the day's row. Does NOT commit: the caller owns the transaction (the child
    settle, or the shadow probe's own short session)."""
    day = day or utc_day()
    now = utcnow_naive()
    statement = _insert_for(db)(OverflowSpend).values(
        aggregator=aggregator,
        day=day,
        calls=1,
        cost_micro=int(cost_micro),
        delta_micro=int(delta_micro),
        updated_at=now,
    ).on_conflict_do_update(
        index_elements=[OverflowSpend.aggregator, OverflowSpend.day],
        set_={
            "calls": OverflowSpend.calls + 1,
            "cost_micro": OverflowSpend.cost_micro + int(cost_micro),
            "delta_micro": OverflowSpend.delta_micro + int(delta_micro),
            "updated_at": now,
        },
    ).returning(OverflowSpend).execution_options(populate_existing=True)
    return (await db.execute(statement)).scalar_one()


async def spent_today(db: AsyncSession, aggregator: str, *, day: str | None = None) -> int:
    row = await db.get(OverflowSpend, (aggregator, day or utc_day()))
    return int(row.cost_micro) if row is not None else 0
