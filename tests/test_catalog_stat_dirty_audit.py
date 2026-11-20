"""Terminal audit corrections queue original-day recounts atomically, outside money."""

from datetime import datetime, timedelta
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from treg import audit
from treg.infra import db
from treg.infra.catalog_stat_dirty import mark_dirty
from treg.models import AsyncTaskRecord, CallRecord, EndpointDayStat, EndpointStatDirty, Org

NOW = datetime(2026, 1, 10, 12)
EP = "test.async.endpoint"


@pytest.fixture
async def clean_store():
    await audit.drain()
    await db.reset_db()
    async with db.session_maker() as session:
        session.add(Org(id=1, name="Synthetic catalog", slug="synthetic-catalog"))
        session.add(Org(id=2, name="Other catalog", slug="other-catalog"))
        await session.commit()
    yield
    await audit.drain()


def _fields(call_id="synthetic-call", *, endpoint=EP, org_id=1, at=NOW, **overrides):
    return dict(org_id=org_id, user_email="synthetic@example.test", tool_name=endpoint,
                method="POST", path="/synthetic", status_code=202, endpoint_id=endpoint,
                call_ref=call_id, created_at=at, **overrides)


def _correction(call_id="synthetic-call", *, endpoint=EP, org_id=1, hit=True):
    return (audit._AsyncHitUpdate,
            dict(call_id=call_id, endpoint_id=endpoint, org_id=org_id, hit=hit,
                 verdict="valid" if hit else "not_found"))


async def _seed(*fields):
    async with db.session_maker() as session:
        session.add_all([CallRecord(**values) for values in fields])
        await session.commit()


async def _markers():
    async with db.session_maker() as session:
        return (await session.execute(sa.select(EndpointStatDirty).order_by(
            EndpointStatDirty.endpoint_id, EndpointStatDirty.day))).scalars().all()


async def test_mark_dirty_coalesces_keys_in_order_without_committing(clean_store):
    keys = [("z.endpoint", "2026-01-09"), ("a.endpoint", "2026-01-10"),
            ("a.endpoint", "2026-01-09"), ("a.endpoint", "2026-01-09")]
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO endpointstatdirty"):
            statements.append(parameters)

    engine = db._engine.sync_engine
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        async with db.session_maker() as session:
            await mark_dirty(session, keys, now=NOW)
            rows = (await session.execute(sa.select(EndpointStatDirty).order_by(
                EndpointStatDirty.endpoint_id, EndpointStatDirty.day))).scalars().all()
            assert [(row.endpoint_id, row.day, row.revision) for row in rows] == [
                ("a.endpoint", "2026-01-09", 1), ("a.endpoint", "2026-01-10", 1),
                ("z.endpoint", "2026-01-09", 1),
            ]
            if session.get_bind().dialect.name == "sqlite":
                # Each VALUES row has endpoint, day, revision, queued_at, retry_at. Keeping the
                # insertion order stable avoids opposite marker-lock orders across batches.
                assert len(statements) == 1
                assert [(statements[0][i], statements[0][i + 1])
                        for i in range(0, len(set(keys)) * 5, 5)] == sorted(set(keys))
            await session.rollback()
        assert await _markers() == []
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)


async def test_new_correction_preserves_marker_age_and_worker_backoff(clean_store):
    key = (EP, "2026-01-09")
    retry = NOW + timedelta(hours=1)
    async with db.session_maker() as session:
        await mark_dirty(session, [key], now=NOW)
        await session.execute(sa.update(EndpointStatDirty).values(retry_at=retry))
        await session.commit()
    async with db.session_maker() as session:
        await mark_dirty(session, [key, key], now=NOW + timedelta(days=1))
        await session.commit()
    marker, = await _markers()
    assert (marker.revision, marker.queued_at, marker.retry_at) == (2, NOW, retry)


async def test_empty_keys_do_not_start_database_work():
    class NoDatabase:
        def get_bind(self):
            raise AssertionError("an empty correction must not inspect the database")

    await mark_dirty(NoDatabase(), [])


async def test_audit_marks_actual_original_days_and_respects_org_and_endpoint(clean_store):
    before_midnight = datetime(2026, 1, 8, 23, 59, 59)
    after_midnight = before_midnight + timedelta(seconds=2)
    await _seed(_fields(at=before_midnight), _fields(at=after_midnight),
                _fields(org_id=2, at=NOW), _fields(endpoint="other.endpoint", at=NOW))
    assert await audit._write_batch([_correction()])
    assert [(row.endpoint_id, row.day, row.revision) for row in await _markers()] == [
        (EP, "2026-01-08", 1), (EP, "2026-01-09", 1),
    ]
    async with db.session_maker() as session:
        rows = (await session.execute(sa.select(CallRecord))).scalars().all()
    assert all(row.hit is True and row.verdict == "valid"
               for row in rows if row.org_id == 1 and row.endpoint_id == EP)
    assert all(row.hit is None
               for row in rows if row.org_id != 1 or row.endpoint_id != EP)


async def test_repeated_terminal_updates_coalesce_within_and_across_batches(clean_store):
    await _seed(_fields())
    assert await audit._write_batch([_correction(), _correction()])
    first, = await _markers()
    assert first.revision == 1
    assert await audit._write_batch([_correction()])
    later, = await _markers()
    assert later.revision == 2
    assert (later.queued_at, later.retry_at) == (first.queued_at, first.retry_at)


async def test_failure_after_marking_rolls_back_both_correction_and_marker(clean_store, monkeypatch):
    await _seed(_fields())

    async def fail_after_marker(session, keys):
        await mark_dirty(session, keys, now=NOW)
        assert (await session.execute(sa.select(EndpointStatDirty))).scalar_one().revision == 1
        raise RuntimeError("synthetic failure before the audit commit")

    monkeypatch.setattr(audit, "mark_dirty", fail_after_marker)
    assert not await audit._write_batch([_correction()])
    assert await _markers() == []
    async with db.session_maker() as session:
        record = (await session.execute(sa.select(CallRecord))).scalar_one()
        assert record.hit is None and record.verdict is None


async def test_terminal_before_audit_insert_uses_durable_verdict_without_phantom_dirty(clean_store):
    async with db.session_maker() as session:
        session.add(AsyncTaskRecord(
            call_id="synthetic-call", org_id=1, provider="synthetic", endpoint_id=EP,
            reserved_micro=0, next_check_at=NOW, created_at=NOW, status="settled",
            completed_at=NOW, hit=True, verdict="valid"))
        await session.commit()
    assert await audit._write_batch([_correction()])
    assert await _markers() == []
    assert await audit._write_batch([(CallRecord, _fields(_async_submission=True))])
    async with db.session_maker() as session:
        record = (await session.execute(sa.select(CallRecord))).scalar_one()
        assert record.hit is True and record.verdict == "valid"
    assert await _markers() == []


async def test_insert_then_correction_in_one_batch_marks_inserted_row(clean_store):
    assert await audit._write_batch([(CallRecord, _fields()), _correction(hit=False)])
    marker, = await _markers()
    assert (marker.endpoint_id, marker.day, marker.revision) == (EP, "2026-01-10", 1)
    async with db.session_maker() as session:
        record = (await session.execute(sa.select(CallRecord))).scalar_one()
        assert record.hit is False and record.verdict == "not_found"


def test_additive_migration_preserves_legacy_buckets_and_never_requires_callrecord():
    migration = import_module("treg.alembic.versions.0074_endpoint_stat_dirty")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "CREATE TABLE endpointdaystat (endpoint_id VARCHAR NOT NULL, day VARCHAR NOT NULL, "
                "n INTEGER NOT NULL, PRIMARY KEY (endpoint_id, day))"))
            connection.execute(sa.text(
                "INSERT INTO endpointdaystat VALUES ('synthetic', '2026-01-09', 7)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            # There is deliberately no CallRecord table in this database: the upgrade must
            # add structure without reading or seeding from the large audit table.
            assert connection.execute(sa.text(
                "SELECT n, terminal_reconciled FROM endpointdaystat")).one() == (7, 0)
            assert connection.execute(sa.text("SELECT count(*) FROM endpointstatdirty")).scalar_one() == 0
            columns = {column["name"]: column for column in sa.inspect(connection).get_columns(
                "endpointstatdirty")}
            assert isinstance(columns["revision"]["type"], sa.BigInteger)
            assert not columns["revision"]["nullable"]
            assert sa.inspect(connection).get_pk_constraint("endpointstatdirty")[
                "constrained_columns"] == ["endpoint_id", "day"]
            index, = sa.inspect(connection).get_indexes("endpointstatdirty")
            assert index["column_names"] == ["retry_at", "endpoint_id", "day"]
            assert index["name"] in {index.name for index in EndpointStatDirty.__table__.indexes}
            connection.execute(sa.text(
                "INSERT INTO endpointstatdirty (endpoint_id, day, queued_at, retry_at) "
                "VALUES ('synthetic', '2026-01-09', :now, :now)"), {"now": NOW.isoformat(" ")})
            assert connection.execute(sa.text("SELECT revision FROM endpointstatdirty")).scalar_one() == 1
            assert EndpointDayStat(endpoint_id=EP, day="2026-01-09").terminal_reconciled is False
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert not sa.inspect(connection).has_table("endpointstatdirty")
            assert connection.execute(sa.text("SELECT n FROM endpointdaystat")).scalar_one() == 7
            assert "terminal_reconciled" not in {
                column["name"] for column in sa.inspect(connection).get_columns("endpointdaystat")}
    finally:
        engine.dispose()
