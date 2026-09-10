"""The sole writer of callreview. The application owns the transaction."""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import CallReview


def add(db: AsyncSession, **values) -> CallReview:
    row = CallReview(**values)
    db.add(row)
    return row


async def get(db: AsyncSession, call_id: str, org_id: int) -> CallReview | None:
    return (await db.execute(select(CallReview).where(
        CallReview.call_id == call_id, CallReview.org_id == org_id,
    ))).scalar_one_or_none()


async def recent(
    db: AsyncSession, *, limit: int, before: int | None = None, endpoint_id: str | None = None,
) -> list[CallReview]:
    query = select(CallReview)
    if before is not None:
        query = query.where(CallReview.id < before)
    if endpoint_id is not None:
        query = query.where(CallReview.endpoint_id == endpoint_id)
    return list((await db.execute(query.order_by(CallReview.id.desc()).limit(limit))).scalars())
