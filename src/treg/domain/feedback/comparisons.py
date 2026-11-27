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


async def take_attempt(db: AsyncSession, day: str, limit: int, *, price_micro: int = 0,
                       spend_limit_micro: int | None = None) -> str:
    """Count one second-vendor call and its price against the UTC day's call `limit` and spend
    limit with one conditional update (the day's first with an insert that yields to a concurrent
    one): `taken`, or `at_limit` / `over_spend` with nothing counted. Correct without a prior row
    lock, on SQLite too. The day's row is no team's, so deleting a team gives nothing back."""
    if limit < 1 or (spend_limit_micro is not None and price_micro > spend_limit_micro):
        return "at_limit" if limit < 1 else "over_spend"

    async def bump() -> bool:
        fits = [CompareAttemptDay.day == day, CompareAttemptDay.attempts < limit]
        if spend_limit_micro is not None:
            fits.append(CompareAttemptDay.spent_micro + price_micro <= spend_limit_micro)
        done = await db.execute(update(CompareAttemptDay).where(*fits).values(
            attempts=CompareAttemptDay.attempts + 1,
            spent_micro=CompareAttemptDay.spent_micro + price_micro,
        ).execution_options(synchronize_session=False))
        return done.rowcount == 1

    if await bump():
        return "taken"
    dialect = (await db.connection()).dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    created = await db.execute(insert(CompareAttemptDay).values(day=day, attempts=1, spent_micro=price_micro)
                               .on_conflict_do_nothing(index_elements=["day"]))
    if created.rowcount == 1 or await bump():
        return "taken"
    row = (await db.execute(select(CompareAttemptDay.attempts).where(CompareAttemptDay.day == day))).scalar()
    return "at_limit" if (row or 0) >= limit else "over_spend"


async def attempts(db: AsyncSession, day: str) -> int:
    row = await db.get(CompareAttemptDay, day)
    return row.attempts if row is not None else 0


async def spent(db: AsyncSession, day: str) -> int:
    """B's catalog prices of the UTC day's claims, failed and pending ones included."""
    row = await db.get(CompareAttemptDay, day)
    return row.spent_micro if row is not None else 0


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


async def tallies(db: AsyncSession, capabilities: list[str], since: datetime) -> dict[tuple[str, str, str], int]:
    """Every team's comparisons since `since` that are not failed, by (capability, A, B): what a
    run spreads its calls by. One grouped read through `created_at`."""
    rows = (await db.execute(select(
        CallComparison.capability, CallComparison.a_provider, CallComparison.b_provider, func.count(),
    ).where(CallComparison.created_at >= since, CallComparison.status != FAILED,
            CallComparison.capability.in_(capabilities))
        .group_by(CallComparison.capability, CallComparison.a_provider, CallComparison.b_provider))).all()
    return {(c, a, b): int(n) for c, a, b, n in rows}


async def failure_streaks(db: AsyncSession, capabilities: list[str], since: datetime,
                          ) -> dict[tuple[str, str], int]:
    """How many of each B's latest answers since `since` failed in a row, by (capability, B):
    a settled comparison (ready or decided) ends the streak. One read through `created_at`."""
    rows = (await db.execute(select(
        CallComparison.capability, CallComparison.b_provider, CallComparison.status,
    ).where(CallComparison.created_at >= since, CallComparison.capability.in_(capabilities),
            CallComparison.status != PENDING)
        .order_by(CallComparison.created_at.desc(), CallComparison.id.desc()))).all()
    streaks: dict[tuple[str, str], int] = {}
    ended: set[tuple[str, str]] = set()
    for capability, b, status in rows:
        key = (capability, b)
        if key in ended:
            continue
        if status == FAILED:
            streaks[key] = streaks.get(key, 0) + 1
        else:
            ended.add(key)
    return streaks


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


async def record_jev(db: AsyncSession, comparison_id: int, *, choice: str, orders: str, version: str,
                     at: datetime) -> bool:
    """Jev's blind verdict, written only over none or an older rubric version, and never into the
    agent's `choice` or `status`; any back-off ends."""
    result = await db.execute(update(CallComparison).where(
        CallComparison.id == comparison_id,
        or_(CallComparison.jev_version.is_(None), CallComparison.jev_version < version),
    ).values(jev_choice=choice, jev_orders=orders, jev_version=version, jev_judged_at=at,
             jev_retry_at=None, jev_attempts=None).execution_options(synchronize_session=False))
    return result.rowcount == 1


async def record_jev_retry(db: AsyncSession, comparison_id: int, version: str, seen: int | None,
                           retry_at: datetime, *, count: bool = True) -> bool:
    """Jev could not judge the pair now: not asked again before `retry_at`, and with `count` one
    more failed try. Only while it is still unjudged under `version` and holds the `seen` tries
    this run read, so two runs failing together count once."""
    seen_now = (CallComparison.jev_attempts.is_(None) if seen is None
                else CallComparison.jev_attempts == seen)
    result = await db.execute(update(CallComparison).where(
        CallComparison.id == comparison_id,
        or_(CallComparison.jev_version.is_(None), CallComparison.jev_version < version), seen_now,
    ).values(jev_attempts=(seen or 0) + 1 if count else seen, jev_retry_at=retry_at)
        .execution_options(synchronize_session=False))
    return result.rowcount == 1
