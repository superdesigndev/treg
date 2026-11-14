"""The sole writer of callreview. The application owns the transaction."""
from typing import Annotated

from pydantic import StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...feedback_contract import REVIEW_REASON_MAX
from ...models import CallReview

# Trimmed before it is measured, as the CLI does; published in the MCP schema as minLength/maxLength.
ReviewReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                max_length=REVIEW_REASON_MAX)]


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
