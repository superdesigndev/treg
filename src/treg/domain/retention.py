"""Retention purge for unbounded audit tables — `treg-worker retention purge`.

CallRecord and RunRecord grow without bound. Evidence columns already expire after 14 days
(see routers/admin.py `_purge_expired_error_evidence`), but the rows themselves are never deleted,
and TOAST-sized JSON doesn't reclaim space until the row is gone. SearchMiss is similar.

This module deletes rows older than the retention window in small batches to avoid long locks on
hot tables. After deletes, a non-blocking VACUUM marks dead space as reusable (the OS sees it free
only after VACUUM FULL, but VACUUM FULL takes an ACCESS EXCLUSIVE lock — never in prod from this
worker, see the PR description).

NEVER touches: Org, User, Membership, Secret, Tool, Bundle, CreditBlock, LedgerEntry, Hold,
TagSpend, TagBudget, CapabilityPin, DenyRule, AdConversion, or anything in domain/money.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import CallRecord, RunRecord, SearchMiss
from ..timeutil import utcnow_naive

RETENTION_DAYS_DEFAULT = 90
BATCH_SIZE_DEFAULT = 5000

_log = logging.getLogger("treg.retention")


@dataclass
class PurgeResult:
    """What one purge pass deleted."""
    table: str
    deleted: int
    cutoff: datetime
    batches: int


async def purge_table(
    db: AsyncSession,
    model: type,
    *,
    retention_days: int = RETENTION_DAYS_DEFAULT,
    batch_size: int = BATCH_SIZE_DEFAULT,
    dry_run: bool = False,
) -> PurgeResult:
    """Delete rows older than retention_days in batches. Returns the total deleted count.

    Uses a subquery-based DELETE with LIMIT to avoid scanning the whole table per batch.
    Each batch commits separately, so progress is durable and the table is never locked for long.
    """
    cutoff = utcnow_naive() - timedelta(days=retention_days)
    table_name = model.__tablename__
    total_deleted = 0
    batches = 0

    while True:
        if dry_run:
            count_q = select(func.count()).select_from(model).where(model.created_at < cutoff)
            count = (await db.execute(count_q)).scalar() or 0
            return PurgeResult(table=table_name, deleted=count, cutoff=cutoff, batches=0)

        id_subq = (
            select(model.id)
            .where(model.created_at < cutoff)
            .order_by(model.id)
            .limit(batch_size)
        ).subquery()
        stmt = delete(model).where(model.id.in_(select(id_subq.c.id)))
        result = await db.execute(stmt)
        deleted = result.rowcount or 0
        await db.commit()

        total_deleted += deleted
        batches += 1

        if deleted == 0:
            break
        _log.info("purge %s: batch %d deleted %d rows (total %d)", table_name, batches, deleted, total_deleted)

    return PurgeResult(table=table_name, deleted=total_deleted, cutoff=cutoff, batches=batches)


async def vacuum_table(table_name: str, *, analyze: bool = True) -> None:
    """Run a non-blocking VACUUM on the table. VACUUM cannot run inside a transaction, so we
    create a fresh connection with AUTOCOMMIT. This reclaims dead tuple space for reuse (but not
    to the OS — that requires VACUUM FULL which takes an ACCESS EXCLUSIVE lock, never here).
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from ..config import get_settings

    db_url = get_settings().database_url
    if "sqlite" in db_url:
        _log.debug("VACUUM skipped (SQLite)")
        return

    engine = create_async_engine(db_url, isolation_level="AUTOCOMMIT")
    try:
        cmd = f"VACUUM ANALYZE {table_name}" if analyze else f"VACUUM {table_name}"
        _log.info("running %s", cmd)
        async with engine.connect() as conn:
            await conn.execute(text(cmd))
    finally:
        await engine.dispose()


@dataclass
class RetentionPurgeResult:
    """Summary of a full retention purge run."""
    results: list[PurgeResult]
    vacuumed: list[str]

    @property
    def total_deleted(self) -> int:
        return sum(r.deleted for r in self.results)

    def summary(self) -> str:
        lines = ["Retention purge complete:"]
        for r in self.results:
            lines.append(f"  {r.table}: {r.deleted:,} rows deleted in {r.batches} batches (cutoff {r.cutoff.date()})")
        if self.vacuumed:
            lines.append(f"  VACUUM ANALYZE: {', '.join(self.vacuumed)}")
        lines.append(f"  Total: {self.total_deleted:,} rows")
        return "\n".join(lines)


PURGEABLE_TABLES: tuple[type, ...] = (CallRecord, RunRecord, SearchMiss)


async def run_retention_purge(
    db: AsyncSession,
    *,
    retention_days: int = RETENTION_DAYS_DEFAULT,
    batch_size: int = BATCH_SIZE_DEFAULT,
    vacuum: bool = True,
    dry_run: bool = False,
) -> RetentionPurgeResult:
    """Purge all audit tables and optionally VACUUM. The main entry point for the worker."""
    results: list[PurgeResult] = []
    vacuumed: list[str] = []

    for model in PURGEABLE_TABLES:
        result = await purge_table(
            db, model, retention_days=retention_days, batch_size=batch_size, dry_run=dry_run
        )
        results.append(result)
        _log.info(
            "purge %s: %d rows deleted (cutoff %s, %d batches)",
            result.table, result.deleted, result.cutoff.date(), result.batches,
        )

    if vacuum and not dry_run:
        for model in PURGEABLE_TABLES:
            table_name = model.__tablename__
            try:
                await vacuum_table(table_name, analyze=True)
                vacuumed.append(table_name)
            except Exception as exc:
                _log.warning("VACUUM %s failed: %s", table_name, exc)

    return RetentionPurgeResult(results=results, vacuumed=vacuumed)
