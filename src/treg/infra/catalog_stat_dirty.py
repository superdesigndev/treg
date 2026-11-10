"""The single writer of the lossy endpoint/day correction queue.

Audit marks corrected days; the catalog worker seeds, acknowledges, defers and prunes them.
Every operation stays inside its caller's transaction and never commits.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EndpointDayStat, EndpointStatDirty
from ..timeutil import utcnow_naive


def _insert(db: AsyncSession):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


async def mark_dirty(db: AsyncSession, keys: Collection[tuple[str, str]], *,
                     now: datetime | None = None) -> None:
    """Queue each distinct bucket once; a new correction preserves its age and retry budget.

    Use a fixed key order across audit batches so simultaneous writers acquire marker locks in
    the same order. The audit correction and these markers commit or roll back together; this
    helper never commits.
    """
    ordered = sorted(set(keys))
    if not ordered:
        return
    at = now or utcnow_naive()
    values = [dict(endpoint_id=endpoint_id, day=day, revision=1, queued_at=at, retry_at=at)
              for endpoint_id, day in ordered]
    stmt = _insert(db)(EndpointStatDirty).values(values)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=["endpoint_id", "day"],
        set_={"revision": EndpointStatDirty.revision + 1}))


async def seed_legacy(db: AsyncSession, mutable_ids: Collection[str], since_day: str,
                      now: datetime) -> None:
    """Queue old mutable buckets once, retaining any correction or backoff already present."""
    if not mutable_ids:
        return
    buckets = select(EndpointDayStat.endpoint_id, EndpointDayStat.day,
                     literal(1), literal(now), literal(now)).where(
        EndpointDayStat.endpoint_id.in_(mutable_ids), EndpointDayStat.day >= since_day,
        EndpointDayStat.terminal_reconciled.is_(False)).order_by(
            EndpointDayStat.endpoint_id, EndpointDayStat.day)
    stmt = _insert(db)(EndpointStatDirty).from_select(
        ["endpoint_id", "day", "revision", "queued_at", "retry_at"], buckets)
    await db.execute(stmt.on_conflict_do_nothing(index_elements=["endpoint_id", "day"]))


async def acknowledge(db: AsyncSession, endpoint_id: str, day: str, revision: int, *,
                      scanned_at: datetime) -> None:
    """Clear covered work, retaining newer corrections with the successful scan's age bound.

    A revision changed during the scan is still dirty, but the replacement covered all source
    evidence visible at scan start. Count its remaining wait from that conservative lower bound,
    never from completion, and never move a newly created marker's time backwards. The caller
    commits this only with a successful full replacement; failures retain their original age.
    """
    await db.execute(delete(EndpointStatDirty).where(
        EndpointStatDirty.endpoint_id == endpoint_id, EndpointStatDirty.day == day,
        EndpointStatDirty.revision == revision))
    await db.execute(update(EndpointStatDirty).where(
        EndpointStatDirty.endpoint_id == endpoint_id, EndpointStatDirty.day == day,
        EndpointStatDirty.revision > revision,
        EndpointStatDirty.queued_at < scanned_at).values(queued_at=scanned_at))


async def defer(db: AsyncSession, endpoint_id: str, day: str, retry_at: datetime) -> None:
    """Back off a failed repair without changing its age or any concurrent correction revision."""
    await db.execute(update(EndpointStatDirty).where(
        EndpointStatDirty.endpoint_id == endpoint_id, EndpointStatDirty.day == day)
        .values(retry_at=retry_at))


async def prune(db: AsyncSession, since_day: str) -> None:
    """Discard corrections for buckets which have aged out of the published window."""
    await db.execute(delete(EndpointStatDirty).where(EndpointStatDirty.day < since_day))
