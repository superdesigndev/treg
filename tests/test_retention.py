"""Retention purge: CallRecord/RunRecord/SearchMiss aged out, money tables untouched."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from treg.domain.retention import (
    BATCH_SIZE_DEFAULT,
    PURGEABLE_TABLES,
    RETENTION_DAYS_DEFAULT,
    purge_table,
    run_retention_purge,
)
from treg.infra.db import session_maker
from treg.models import (
    CallRecord,
    CreditBlock,
    LedgerEntry,
    Org,
    RunRecord,
    SearchMiss,
    TagSpend,
    User,
)
from treg.timeutil import utcnow_naive


@pytest.fixture
async def seeded_db(clients):
    """Seed the database with test rows for retention purge testing."""
    now = utcnow_naive()
    old = now - timedelta(days=RETENTION_DAYS_DEFAULT + 10)
    recent = now - timedelta(days=RETENTION_DAYS_DEFAULT - 10)

    async with session_maker() as db:
        org = (await db.execute(select(Org).limit(1))).scalar_one()

        for i in range(5):
            db.add(CallRecord(
                org_id=org.id,
                user_email=f"old-{i}@test.com",
                tool_name="test.tool",
                method="GET",
                path="/test",
                status_code=200,
                created_at=old,
            ))
        for i in range(3):
            db.add(CallRecord(
                org_id=org.id,
                user_email=f"recent-{i}@test.com",
                tool_name="test.tool",
                method="GET",
                path="/test",
                status_code=200,
                created_at=recent,
            ))

        for i in range(4):
            db.add(RunRecord(
                org_id=org.id,
                user_email=f"old-run-{i}@test.com",
                bundle_name="test.bundle",
                argv=[],
                exit_code=0,
                duration_ms=100,
                created_at=old,
            ))
        for i in range(2):
            db.add(RunRecord(
                org_id=org.id,
                user_email=f"recent-run-{i}@test.com",
                bundle_name="test.bundle",
                argv=[],
                exit_code=0,
                duration_ms=100,
                created_at=recent,
            ))

        for i in range(6):
            db.add(SearchMiss(query=f"old query {i}", source="api", created_at=old))
        for i in range(2):
            db.add(SearchMiss(query=f"recent query {i}", source="api", created_at=recent))

        await db.commit()

    return {"old_calls": 5, "recent_calls": 3, "old_runs": 4, "recent_runs": 2,
            "old_misses": 6, "recent_misses": 2}


async def test_purge_deletes_old_callrecords(seeded_db):
    """CallRecord rows older than retention window are deleted."""
    async with session_maker() as db:
        result = await purge_table(db, CallRecord, retention_days=RETENTION_DAYS_DEFAULT)

    assert result.deleted == seeded_db["old_calls"]
    assert result.table == "callrecord"

    async with session_maker() as db:
        remaining = (await db.execute(select(func.count()).select_from(CallRecord))).scalar()
        assert remaining == seeded_db["recent_calls"]


async def test_purge_deletes_old_runrecords(seeded_db):
    """RunRecord rows older than retention window are deleted."""
    async with session_maker() as db:
        result = await purge_table(db, RunRecord, retention_days=RETENTION_DAYS_DEFAULT)

    assert result.deleted == seeded_db["old_runs"]
    assert result.table == "runrecord"

    async with session_maker() as db:
        remaining = (await db.execute(select(func.count()).select_from(RunRecord))).scalar()
        assert remaining == seeded_db["recent_runs"]


async def test_purge_deletes_old_searchmiss(seeded_db):
    """SearchMiss rows older than retention window are deleted."""
    async with session_maker() as db:
        result = await purge_table(db, SearchMiss, retention_days=RETENTION_DAYS_DEFAULT)

    assert result.deleted == seeded_db["old_misses"]
    assert result.table == "searchmiss"

    async with session_maker() as db:
        remaining = (await db.execute(select(func.count()).select_from(SearchMiss))).scalar()
        assert remaining == seeded_db["recent_misses"]


async def test_purge_respects_custom_retention_days(seeded_db):
    """A shorter retention window deletes more rows."""
    short_window = RETENTION_DAYS_DEFAULT - 20

    async with session_maker() as db:
        result = await purge_table(db, CallRecord, retention_days=short_window)

    total_calls = seeded_db["old_calls"] + seeded_db["recent_calls"]
    assert result.deleted == total_calls

    async with session_maker() as db:
        remaining = (await db.execute(select(func.count()).select_from(CallRecord))).scalar()
        assert remaining == 0


async def test_purge_dry_run_deletes_nothing(seeded_db):
    """Dry run reports count but deletes nothing."""
    async with session_maker() as db:
        result = await purge_table(db, CallRecord, retention_days=RETENTION_DAYS_DEFAULT, dry_run=True)

    assert result.deleted == seeded_db["old_calls"]
    assert result.batches == 0

    async with session_maker() as db:
        remaining = (await db.execute(select(func.count()).select_from(CallRecord))).scalar()
        assert remaining == seeded_db["old_calls"] + seeded_db["recent_calls"]


async def test_purge_batch_size(seeded_db):
    """Batching deletes rows in chunks."""
    async with session_maker() as db:
        result = await purge_table(db, CallRecord, retention_days=RETENTION_DAYS_DEFAULT, batch_size=2)

    assert result.deleted == seeded_db["old_calls"]
    assert result.batches >= 3


async def test_run_retention_purge_all_tables(seeded_db):
    """Full purge run deletes from all purgeable tables."""
    async with session_maker() as db:
        result = await run_retention_purge(db, retention_days=RETENTION_DAYS_DEFAULT, vacuum=False)

    assert result.total_deleted == (
        seeded_db["old_calls"] + seeded_db["old_runs"] + seeded_db["old_misses"]
    )
    assert len(result.results) == len(PURGEABLE_TABLES)

    async with session_maker() as db:
        calls = (await db.execute(select(func.count()).select_from(CallRecord))).scalar()
        runs = (await db.execute(select(func.count()).select_from(RunRecord))).scalar()
        misses = (await db.execute(select(func.count()).select_from(SearchMiss))).scalar()

    assert calls == seeded_db["recent_calls"]
    assert runs == seeded_db["recent_runs"]
    assert misses == seeded_db["recent_misses"]


async def test_money_tables_never_touched(clients):
    """CreditBlock, LedgerEntry, TagSpend, Org must NEVER be purged."""
    from uuid import uuid4
    now = utcnow_naive()
    old = now - timedelta(days=RETENTION_DAYS_DEFAULT + 100)

    async with session_maker() as db:
        org = (await db.execute(select(Org).limit(1))).scalar_one()
        user = (await db.execute(select(User).limit(1))).scalar_one()

        block_id = uuid4().hex
        db.add(CreditBlock(
            id=block_id,
            org_id=org.id,
            kind="promotional",
            amount_micro=1_000_000,
            remaining_micro=1_000_000,
            created_at=old,
        ))

        entry_id = uuid4().hex
        db.add(LedgerEntry(
            id=entry_id,
            org_id=org.id,
            block_id=block_id,
            kind="grant",
            amount_micro=1_000_000,
            created_at=old,
        ))

        db.add(TagSpend(
            org_id=org.id,
            dim="customer",
            val="cust_123",
            hold_id=uuid4().hex,
            settled=True,
            amount_micro=100,
            created_at=old,
        ))

        await db.commit()

    async with session_maker() as db:
        result = await run_retention_purge(db, retention_days=RETENTION_DAYS_DEFAULT, vacuum=False)

    assert result.total_deleted == 0

    async with session_maker() as db:
        blocks = (await db.execute(select(func.count()).select_from(CreditBlock))).scalar()
        entries = (await db.execute(select(func.count()).select_from(LedgerEntry))).scalar()
        tags = (await db.execute(select(func.count()).select_from(TagSpend))).scalar()
        orgs = (await db.execute(select(func.count()).select_from(Org))).scalar()

    assert blocks >= 1
    assert entries >= 1
    assert tags >= 1
    assert orgs >= 1


async def test_purgeable_tables_are_exactly_audit_tables():
    """The PURGEABLE_TABLES constant contains exactly the right models."""
    assert set(PURGEABLE_TABLES) == {CallRecord, RunRecord, SearchMiss}


async def test_purge_empty_table(clients):
    """Purging an already-empty table is a no-op."""
    async with session_maker() as db:
        result = await purge_table(db, SearchMiss, retention_days=1)

    assert result.deleted == 0
    assert result.batches == 1


async def test_retention_default_is_90_days():
    """The default retention window is 90 days."""
    assert RETENTION_DAYS_DEFAULT == 90


async def test_batch_default_is_5000():
    """The default batch size is 5000 rows."""
    assert BATCH_SIZE_DEFAULT == 5000


async def test_purge_result_summary(seeded_db):
    """The summary method produces readable output."""
    async with session_maker() as db:
        result = await run_retention_purge(db, retention_days=RETENTION_DAYS_DEFAULT, vacuum=False)

    summary = result.summary()
    assert "Retention purge complete:" in summary
    assert "callrecord:" in summary
    assert "runrecord:" in summary
    assert "searchmiss:" in summary
    assert "Total:" in summary
