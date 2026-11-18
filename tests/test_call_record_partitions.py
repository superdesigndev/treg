"""The partitioned call record table is prepared beside the live one (alembic 0115 and 0116).

The PostgreSQL cases run against a real server (`TREG_TEST_DB_URL`, the Postgres CI job): the
backfill copies history newest first into `callrecord_partitioned`, resumes, notes when it first
ran, and paces itself; team deletion clears the team's copied rows. Readers still read the plain
`callrecord`. On SQLite every step is a no-op.
"""
from __future__ import annotations

import argparse
import inspect
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from treg import audit, worker
from treg.application import call_record_partitions as partitions
from treg.domain.governance.teams import cascade_delete_org
from treg.infra import db
from treg.models import CallRecord, Org
from treg.timeutil import utcnow_naive
from tests.test_alembic_baseline import _drop_everything, _upgrade_to


real_backfill = partitions.backfill

_ROWS = text("""
    INSERT INTO callrecord (org_id, user_email, tool_name, method, path, status_code, kind, client,
                            call_ref, budget_dim, budget_val, created_at, cached)
    SELECT :org_id, 'member@example.test', 'echo', 'GET', '/', 200, 'call', '', 'ref-' || n, '', '',
           CAST(:newest AS timestamp) - (:count - n) * CAST(:step AS interval), false
    FROM generate_series(1, :count) AS n
""")


async def _scalar(sql: str, **params):
    async with db.session_maker() as session:
        return (await session.execute(text(sql), params)).scalar()


@pytest.fixture
async def before_swap(monkeypatch):
    """A database at 0116 with one team and 40 calls, one per hour, the newest a minute old."""
    if db._engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to("0116")
        async with db.session_maker() as session:
            session.add(org := Org(name="swap", slug="swap"))
            await session.flush()
            org_id = org.id
            await session.execute(_ROWS, {"org_id": org_id, "count": 40, "step": timedelta(hours=1),
                                          "newest": utcnow_naive() - timedelta(minutes=1)})
            await session.commit()
        yield org_id
    finally:
        await audit.drain()
        await _drop_everything()
        await db.reset_db()


async def test_the_copy_runs_newest_first_and_a_stopped_run_resumes(before_swap):
    first = await partitions.backfill(1, batch_rows=4, max_seconds=0, pause=0)
    assert (first["phase"], first["copied"], first["done"]) == ("before_swap", 4, False)
    assert await _scalar("SELECT min(id) FROM callrecord_partitioned") == 37

    # Rows are an hour apart, so one day of history is the newest 24: ids 17 to 40.
    rest = await partitions.backfill(1, batch_rows=4, pause=0)
    assert rest["done"] is True
    assert await _scalar("SELECT min(id) FROM callrecord_partitioned") == 17
    assert (await partitions.backfill(1, batch_rows=4, pause=0))["copied"] == 0
    # The live table is untouched: readers still read every row from it.
    assert await _scalar("SELECT count(*) FROM callrecord") == 40


async def test_the_backfill_notes_when_its_first_run_started(before_swap):
    """The swap resyncs late verdicts from the moment the copy began; later runs keep it."""
    started = utcnow_naive()
    await partitions.backfill(1, batch_rows=10, pause=0)
    note = json.loads(await _scalar("SELECT obj_description('callrecord_partitioned'::regclass, 'pg_class')"))
    first = note[partitions.NOTE_BACKFILL_STARTED]
    assert started - timedelta(seconds=5) <= datetime.fromisoformat(first) <= utcnow_naive()
    await partitions.backfill(2, batch_rows=10, pause=0)
    again = json.loads(await _scalar("SELECT obj_description('callrecord_partitioned'::regclass, 'pg_class')"))
    assert again[partitions.NOTE_BACKFILL_STARTED] == first


async def test_team_deletion_clears_the_team_rows_the_backfill_copied(before_swap):
    """The staged table has no foreign key to `org`, so deletion removes the copies itself."""
    await partitions.backfill(31, batch_rows=10, pause=0)
    assert await _scalar("SELECT count(*) FROM callrecord_partitioned") == 40
    async with db.session_maker() as session:
        org = await session.get(Org, before_swap)
        await cascade_delete_org(org, session)
        await session.commit()
    assert await _scalar("SELECT count(*) FROM callrecord_partitioned") == 0
    assert await _scalar("SELECT count(*) FROM callrecord") == 0


async def test_without_partitions_every_step_is_a_no_op():
    """SQLite, and a PostgreSQL database built from the models (as the other tests use)."""
    await db.reset_db()
    assert await partitions.maintain() == {"created": [], "dropped": []}
    assert await partitions.drop_expired(1) == []
    assert (await partitions.backfill(30))["copied"] == 0
    async with db.session_maker() as session:
        session.add(CallRecord(user_email="a@example.test", tool_name="echo", method="GET",
                               path="/", status_code=200))
        await session.commit()
        assert (await session.execute(select(func.count()).select_from(CallRecord))).scalar_one() == 1


def test_the_backfill_paces_itself_by_default(monkeypatch):
    seen = {}

    async def backfill(days, **options):
        seen.update(options, days=days)
        return {}
    monkeypatch.setattr(partitions, "backfill", backfill)
    assert worker.main(["admin", "callrecord-backfill", "--days", "31"]) == 0
    assert seen == {"days": 31, "batch_rows": 5_000, "max_seconds": None, "pause": 0.5}
    assert worker.main(["admin", "callrecord-backfill", "--days", "31", "--pause-seconds", "2"]) == 0
    assert seen["pause"] == 2
    defaults = inspect.signature(real_backfill).parameters
    assert (defaults["batch_rows"].default, defaults["pause"].default) == (5_000, 0.5)


async def test_a_failed_partition_upkeep_fails_the_rollup_run_after_the_fold(monkeypatch, caplog):
    """A missing month refuses every audit row, so an upkeep failure must reach an alert: the
    run logs it as an error and exits 1, and the fold still runs."""
    import treg.infra.db as infra_db
    from treg.application import call_usage_rollup

    async def verified():
        return None

    async def broken():
        raise RuntimeError("canceling statement due to lock timeout")
    folded = []

    async def refresh(**options):
        folded.append(options)
        return {"rows": 0}
    monkeypatch.setattr(infra_db, "verify_db", verified)
    monkeypatch.setattr(partitions, "maintain", broken)
    monkeypatch.setattr(call_usage_rollup, "refresh", refresh)

    with caplog.at_level("ERROR", logger="treg.worker"):
        assert await worker._usage_rollup(argparse.Namespace(max_rows=10)) == 1
    assert folded and "callrecord partition upkeep failed" in caplog.text

