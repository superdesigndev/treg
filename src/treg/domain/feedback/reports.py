"""Durable feedback records. Caller-provided references are claims, not findings."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import Feedback


def add(db: AsyncSession, **values) -> Feedback:
    row = Feedback(**values)
    db.add(row)
    return row


async def get(db: AsyncSession, feedback_id: int, org_id: int) -> Feedback | None:
    return (await db.execute(select(Feedback).where(
        Feedback.id == feedback_id, Feedback.org_id == org_id,
    ))).scalar_one_or_none()


async def recent(
    db: AsyncSession, *, limit: int, before: int | None = None, category: str | None = None,
) -> list[Feedback]:
    query = select(Feedback)
    if before is not None:
        query = query.where(Feedback.id < before)
    if category is not None:
        query = query.where(Feedback.category == category)
    return list((await db.execute(query.order_by(Feedback.id.desc()).limit(limit))).scalars())
