"""The sole writer of callmark. The application owns the transaction."""
import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import CallMark


def content_hash(call_id: str, payload: dict) -> str:
    """Identical retries of one mark collapse; any difference is a new, appended mark."""
    canonical = json.dumps({"call_id": call_id, **payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def add(db: AsyncSession, **values) -> CallMark:
    row = CallMark(**values)
    db.add(row)
    return row


async def by_hash(db: AsyncSession, org_id: int, digest: str) -> CallMark | None:
    return (await db.execute(select(CallMark).where(
        CallMark.org_id == org_id, CallMark.content_hash == digest,
    ))).scalar_one_or_none()


async def get(db: AsyncSession, mark_id: int, org_id: int, *predicates) -> CallMark | None:
    """`predicates` narrows the team scope further (a pinned reader's tags); the caller builds them."""
    return (await db.execute(select(CallMark).where(
        CallMark.id == mark_id, CallMark.org_id == org_id, *predicates,
    ))).scalar_one_or_none()


async def recent(
    db: AsyncSession, *, limit: int, before: int | None = None, endpoint_id: str | None = None,
) -> list[CallMark]:
    query = select(CallMark)
    if before is not None:
        query = query.where(CallMark.id < before)
    if endpoint_id is not None:
        query = query.where(CallMark.endpoint_id == endpoint_id)
    return list((await db.execute(query.order_by(CallMark.id.desc()).limit(limit))).scalars())


async def of_review(db: AsyncSession, org_id: int, review_id: int, *predicates) -> int | None:
    """The id of the mark a review's rows wrote, if any. `predicates` narrows the team scope
    further (a pinned reader's tags); the caller builds them."""
    return (await db.execute(select(CallMark.id).where(
        CallMark.org_id == org_id, CallMark.review_id == review_id, *predicates,
    ).order_by(CallMark.id.asc()).limit(1))).scalar_one_or_none()
