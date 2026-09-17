"""The per-team daily allowance on a catalog endpoint served on treg's key.

Money is the brake on every paid platform call: the hold, the balance and the daily cap all bite.
At $0 none of them do, so a looping client can spend the shared vendor key's whole quota and every
other team's access with it. The allowance is the brake for that case, and for the paid route on a
vendor whose per-key daily quota one team could spend for everyone. It lives beside the price in
the endpoint's cost block (`calls_per_team_day`, catalog.md); a $0 block without one falls back to
`Settings.free_allowance_per_team_day`.

One conditional upsert of the (team, endpoint, UTC day) row: the WHERE is the check and the SET is
the count, so N concurrent calls cannot each read a compliant figure and together overshoot - the
same idiom as `money.reserve`, `usage.take_daily_slot` and `overflow_spend.reserve_in_transaction`.
A refused call writes nothing. Does not commit; the reservation transaction owns it.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import EndpointAllowance
from ...timeutil import utcnow_naive


def utc_day() -> str:
    return utcnow_naive().strftime("%Y-%m-%d")


def _insert_for(db: AsyncSession):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise RuntimeError(f"unsupported database dialect: {dialect}")


async def take_endpoint_allowance_slot(db: AsyncSession, org_id: int, endpoint_id: str, cap: int,
                                   *, day: str | None = None) -> int | None:
    """Admit one call against the team's allowance for this endpoint today, or say no.

    Returns the count admitted today INCLUDING this call, or None when the allowance is used up.
    A cap of 0 refuses even the day's first call without writing a row."""
    if cap <= 0:
        return None
    day = day or utc_day()
    now = utcnow_naive()
    statement = _insert_for(db)(EndpointAllowance).values(
        org_id=org_id, endpoint_id=endpoint_id, day=day, used=1, updated_at=now,
    ).on_conflict_do_update(
        index_elements=[EndpointAllowance.org_id, EndpointAllowance.endpoint_id, EndpointAllowance.day],
        set_={"used": EndpointAllowance.used + 1, "updated_at": now},
        where=EndpointAllowance.used < cap,
    ).returning(EndpointAllowance.used)
    return (await db.execute(statement)).scalar_one_or_none()


async def used_today(db: AsyncSession, org_id: int, endpoint_id: str, *, day: str | None = None) -> int:
    """What the counter says, for a refusal message. Not on the admit path."""
    row = await db.get(EndpointAllowance, (org_id, endpoint_id, day or utc_day()))
    return int(row.used) if row is not None else 0
