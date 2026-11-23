"""The sole writer of callreview. The application owns the transaction."""
from datetime import datetime

from sqlalchemy import or_, update
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


async def record_privacy(db: AsyncSession, review_id: int, person_p: float, at: datetime) -> bool:
    """Jev's privacy judgement, written only onto a review not judged yet: a second run that asked
    too never replaces the first answer (which may already decide a public quote)."""
    result = await db.execute(update(CallReview).where(
        CallReview.id == review_id, CallReview.privacy_judged_at.is_(None),
    ).values(person_p=person_p, privacy_judged_at=at))
    return result.rowcount == 1


async def record_codes(db: AsyncSession, review_id: int, primary: str, secondary: str | None,
                       version: str, at: datetime) -> bool:
    """Codebook codes, written only over none or an older codebook version (versions are ISO dates,
    so they order as strings): a worker on an older build never downgrades a newer judgement."""
    result = await db.execute(update(CallReview).where(
        CallReview.id == review_id,
        or_(CallReview.codebook_version.is_(None), CallReview.codebook_version < version),
    ).values(primary_code=primary, secondary_code=secondary, codebook_version=version, code_judged_at=at))
    return result.rowcount == 1
