"""The sole writer of callcomparison and compareattemptday. The application owns the transaction.

A comparison is shown blinded: answer 1 and answer 2 in the stored `first` order, never a vendor
name. A verdict names 1, 2, tie or both_bad and is mapped back to a and b here."""
from datetime import datetime

from sqlalchemy import func, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import CallComparison, CompareAttemptDay

PENDING, READY, FAILED, DECIDED = "pending", "ready", "failed", "decided"
CHOICES = ("1", "2", "tie", "both_bad")


def add(db: AsyncSession, **values) -> CallComparison:
    row = CallComparison(**values)
    db.add(row)
    return row


async def take_attempt(db: AsyncSession, day: str, limit: int) -> bool:
    """Count one second-vendor call against the UTC day's `limit` with one conditional update
    (the day's first with an insert that yields to a concurrent one); False, nothing counted, at
    the limit. Correct without a prior row lock, on SQLite too."""
    if limit < 1:
        return False

    async def bump() -> bool:
        done = await db.execute(update(CompareAttemptDay).where(
            CompareAttemptDay.day == day, CompareAttemptDay.attempts < limit,
        ).values(attempts=CompareAttemptDay.attempts + 1).execution_options(synchronize_session=False))
        return done.rowcount == 1

    if await bump():
        return True
    dialect = (await db.connection()).dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    created = await db.execute(insert(CompareAttemptDay).values(day=day, attempts=1)
                               .on_conflict_do_nothing(index_elements=["day"]))
    return created.rowcount == 1 or await bump()


async def attempts(db: AsyncSession, day: str) -> int:
    row = await db.get(CompareAttemptDay, day)
    return row.attempts if row is not None else 0


async def finish(db: AsyncSession, call_ref: str, **values) -> bool:
    """Settle a claimed (pending) comparison; False when it is gone (its team was deleted)."""
    result = await db.execute(update(CallComparison).where(
        CallComparison.call_ref == call_ref, CallComparison.status == PENDING,
    ).values(**values).execution_options(synchronize_session=False))
    return result.rowcount == 1


async def by_call_ref(db: AsyncSession, call_ref: str) -> CallComparison | None:
    return (await db.execute(select(CallComparison).where(
        CallComparison.call_ref == call_ref))).scalar_one_or_none()


async def get(db: AsyncSession, comparison_id: int, org_id: int) -> CallComparison | None:
    return (await db.execute(select(CallComparison).where(
        CallComparison.id == comparison_id, CallComparison.org_id == org_id,
    ))).scalar_one_or_none()


async def next_ready(db: AsyncSession, org_id: int, since: datetime) -> CallComparison | None:
    """The team's oldest comparison still waiting for a verdict, created at or after `since`."""
    return (await db.execute(select(CallComparison).where(
        CallComparison.org_id == org_id, CallComparison.status == READY,
        CallComparison.created_at >= since,
    ).order_by(CallComparison.created_at.asc(), CallComparison.id.asc()).limit(1))).scalar_one_or_none()


async def counted(db: AsyncSession, org_id: int, capability: str, pair: tuple[str, str], *,
                  since: datetime, ready_since: datetime) -> int:
    """The team's comparisons of one capability and unordered pair that count toward its cap:
    decided ones since `since`, and ones in flight or waiting that were created since `ready_since`.
    Read through (org_id, capability, created_at)."""
    x, y = pair
    return int((await db.execute(select(func.count()).select_from(CallComparison).where(
        CallComparison.org_id == org_id, CallComparison.capability == capability,
        CallComparison.created_at >= since,
        or_((CallComparison.a_provider == x) & (CallComparison.b_provider == y),
            (CallComparison.a_provider == y) & (CallComparison.b_provider == x)),
        or_(CallComparison.status == DECIDED,
            CallComparison.status.in_((READY, PENDING)) & (CallComparison.created_at >= ready_since)),
    ))).scalar() or 0)


def shown(row: CallComparison) -> tuple[str, str]:
    """(answer 1, answer 2) in the order the agent sees them."""
    return (row.a_body, row.b_body) if row.first == "a" else (row.b_body, row.a_body)


def side(row: CallComparison, choice: str) -> str:
    """The stored verdict for a shown choice: 1/2 become a/b through `first`."""
    if choice == "1":
        return row.first
    if choice == "2":
        return "b" if row.first == "a" else "a"
    return choice


async def decide(db: AsyncSession, row: CallComparison, *, choice: str, reason: str | None,
                 client: str, at: datetime) -> bool:
    """Record the verdict on a waiting comparison; False when it already had one (kept)."""
    result = await db.execute(update(CallComparison).where(
        CallComparison.id == row.id, CallComparison.status == READY,
    ).values(choice=side(row, choice), reason=reason, client=client, status=DECIDED, decided_at=at)
        .execution_options(synchronize_session=False))
    return result.rowcount == 1


def shown_choice(row: CallComparison) -> str | None:
    """The stored verdict as the agent gave it (1, 2, tie, both_bad)."""
    if row.choice in ("a", "b"):
        return "1" if row.choice == row.first else "2"
    return row.choice
