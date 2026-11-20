"""Paid-question marks share the caller's transaction and count every billed use."""

from datetime import datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from treg import archive
from treg.application.call.settle import DeferredSettle, close_deferred
from treg.config import get_settings
from treg.domain import money as ledger
from treg.infra.db import reset_db, session_maker
from treg.models import ArchiveKeyOrg, Org


@pytest.fixture
async def archive_db():
    await reset_db()


async def test_batch_counts_repeated_questions_and_preserves_first_use(archive_db, monkeypatch):
    first = datetime(2026, 1, 1)
    latest = datetime(2026, 1, 2)
    async with session_maker() as db:
        db.add(ArchiveKeyOrg(org_id=1, key_hash="existing", calls=4,
                             first_call_at=first, last_call_at=first))
        await db.commit()
    monkeypatch.setattr(archive, "_utcnow", lambda: latest)
    async with session_maker() as db:
        await archive.note_org_uses_in_transaction(db, [
            (2, "new"), (1, "existing"), (1, "new"), (1, "existing"), (2, "new"),
        ])
        await db.commit()
    async with session_maker() as db:
        rows = (await db.execute(select(ArchiveKeyOrg))).scalars().all()
    assert {(row.org_id, row.key_hash): row.calls for row in rows} == {
        (1, "existing"): 6, (1, "new"): 1, (2, "new"): 2,
    }
    assert all(row.last_call_at == latest for row in rows)
    assert {(row.org_id, row.key_hash): row.first_call_at for row in rows} == {
        (1, "existing"): first, (1, "new"): latest, (2, "new"): latest,
    }


async def test_deferred_records_repeated_questions_in_one_round_trip(archive_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    async with session_maker() as db:
        org = Org(name="batch marks", slug="batch-marks")
        db.add(org)
        await db.flush()
        org_id = org.id
        await ledger.grant(db, org_id, amount_micro=1_000)
        await db.commit()
        holds = [await ledger.reserve(db, org_id, "test.archive", 100,
                                      call_id=f"batch-mark-{i}") for i in range(3)]
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if "ARCHIVEKEYORG" in statement.upper():
            statements.append(statement)

    engine = session_maker.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert await close_deferred([
            DeferredSettle(holds[0], True, 40, (org_id, "same"), "", {}, payer_org_id=org_id),
            DeferredSettle(holds[1], True, 50, (org_id, "other"), "", {}, payer_org_id=org_id),
            DeferredSettle(holds[2], True, 60, (org_id, "same"), "", {}, payer_org_id=org_id),
        ], charge=True) == 150
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    async with session_maker() as db:
        rows = (await db.execute(select(ArchiveKeyOrg))).scalars().all()
        assert {row.key_hash: row.calls for row in rows} == {"same": 2, "other": 1}
        assert await ledger.balance_of(db, org_id) == 850
        assert await ledger.open_holds_of(db, org_id) == []


async def test_single_and_batch_refresh_marks_already_loaded_by_the_caller(archive_db, monkeypatch):
    first = datetime(2026, 1, 1)
    latest = datetime(2026, 1, 2)
    monkeypatch.setattr(archive, "_utcnow", lambda: latest)
    async with session_maker() as db:
        row = ArchiveKeyOrg(org_id=1, key_hash="loaded", calls=4,
                            first_call_at=first, last_call_at=first)
        db.add(row)
        await db.commit()
        await archive.note_org_use_in_transaction(db, 1, "loaded")
        assert (row.calls, row.first_call_at, row.last_call_at) == (5, first, latest)
        await archive.note_org_uses_in_transaction(db, [(1, "loaded"), (1, "loaded")])
        assert (row.calls, row.first_call_at, row.last_call_at) == (7, first, latest)
        await db.commit()


async def test_empty_uses_do_not_start_a_database_transaction(archive_db):
    async with session_maker() as db:
        await archive.note_org_uses_in_transaction(db, iter(()))
        assert not db.in_transaction()


async def test_large_batch_counts_every_use_across_bounded_statements(archive_db):
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if "ARCHIVEKEYORG" in statement.upper():
            statements.append(statement)

    engine = session_maker.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        async with session_maker() as db:
            # Reversed input and a duplicate crossing the chunk boundary must still count once
            # per use, with a statement size bounded independently of caller batch size.
            await archive.note_org_uses_in_transaction(
                db, [(1, f"key-{i:03d}") for i in reversed(range(205))] + [(1, "key-100")])
            await db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 3
    async with session_maker() as db:
        rows = (await db.execute(select(ArchiveKeyOrg))).scalars().all()
    assert len(rows) == 205
    assert sum(row.calls for row in rows) == 206
    assert next(row.calls for row in rows if row.key_hash == "key-100") == 2


async def test_later_chunk_error_rolls_back_marks_and_other_caller_writes(archive_db):
    async with session_maker() as db:
        db.add(Org(name="rollback", slug="archive-rollback"))
        with pytest.raises(IntegrityError):
            # The final mark violates NOT NULL, not the handled (Org, key) unique constraint.
            # Earlier chunks and caller writes must remain uncommitted and roll back together.
            await archive.note_org_uses_in_transaction(
                db, [(1, f"key-{i:03d}") for i in range(105)] + [(2, None)])
        await db.rollback()
    async with session_maker() as db:
        assert (await db.execute(select(ArchiveKeyOrg))).scalars().all() == []
        assert (await db.execute(select(Org).where(Org.slug == "archive-rollback"))).scalars().all() == []


async def test_single_mark_does_not_swallow_unrelated_integrity_errors(archive_db):
    async with session_maker() as db:
        with pytest.raises(IntegrityError):
            await archive.note_org_use_in_transaction(db, 1, None)
        await db.rollback()
