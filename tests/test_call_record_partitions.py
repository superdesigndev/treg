"""The partitioned call record table is prepared beside the live one (alembic 0122 and 0123).

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
    """A database at 0123 with one team and 40 calls, one per hour, the newest a minute old."""
    if db._engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to("0123")
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
    assert seen == {"days": 31, "batch_rows": 5_000, "max_seconds": None, "pause": 0.5,
                    "max_replica_lag": None}
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



@pytest.fixture
async def at_head():
    """The database every later revision builds: a revision that changes `callrecord` alone
    fails the parity tests. Once `callrecord` is the partitioned table there is nothing to keep
    in step, and they skip."""
    if db._engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to("head")
        if await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "p":
            pytest.skip("callrecord is partitioned: nothing left to keep in step")
        yield
    finally:
        await _drop_everything()
        await db.reset_db()


async def test_the_staged_table_has_the_live_tables_columns(at_head):
    """The copy names the live table's columns, so a column added to `callrecord` before the swap
    must be added to `callrecord_partitioned` in the same migration; this fails until it is."""
    shape = text("""
        SELECT attname, format_type(atttypid, atttypmod), attnotnull FROM pg_attribute
        WHERE attrelid = to_regclass(:name) AND attnum > 0 AND NOT attisdropped ORDER BY attnum""")
    async with db.session_maker() as session:
        live = (await session.execute(shape, {"name": "callrecord"})).all()
        staged = (await session.execute(shape, {"name": "callrecord_partitioned"})).all()
    widened = {("id", "integer", True): ("id", "bigint", True)}
    assert [widened.get(tuple(c), tuple(c)) for c in live] == [tuple(c) for c in staged]


async def test_each_run_first_deletes_copies_of_deleted_teams(before_swap):
    """A team deleted while a batch copied its rows can leave copies; the next run removes them."""
    await partitions.backfill(31, batch_rows=10, pause=0)
    async with db.session_maker() as session:     # as if deleted mid-copy: the copies stay behind
        await session.execute(text("DELETE FROM callrecord WHERE org_id = :id"), {"id": before_swap})
        await session.execute(text("DELETE FROM org WHERE id = :id"), {"id": before_swap})
        await session.commit()
    assert await _scalar("SELECT count(*) FROM callrecord_partitioned") == 40
    out = await partitions.backfill(31, batch_rows=10, pause=0)
    assert out["deleted_orphans"] == 40
    async with db.session_maker() as session:
        session.add(Org(name="kept", slug="kept"))
        await session.commit()
    assert await partitions.delete_rows_of_deleted_teams(batch=15) == 0
    assert await _scalar("SELECT count(*) FROM callrecord_partitioned") == 0


async def test_the_backfill_waits_while_the_replica_lags(before_swap, monkeypatch):
    lags = iter([30.0, 30.0, 0.0])
    asked = []

    async def lag() -> float:
        asked.append(1)
        return next(lags, 0.0)
    monkeypatch.setattr(partitions, "replica_lag_seconds", lag)
    monkeypatch.setattr(partitions, "REPLICA_LAG_POLL_SECONDS", 0)
    out = await partitions.backfill(31, batch_rows=10, pause=0, max_replica_lag=5)
    assert out["replica_waits"] == 2 and out["done"] is True
    assert await _scalar("SELECT count(*) FROM callrecord_partitioned") == 40
    assert (await partitions.backfill(31, batch_rows=10, pause=0))["replica_waits"] == 0


async def test_the_staged_table_has_the_live_tables_indexes(at_head):
    """The swap gives the staged table the live table's name and index names, so every index the
    live table has (a migration adding one before the swap) must exist on the staged table too."""
    defs = text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :name")

    def normalized(rows, table):
        return {d.replace("ON ONLY ", "ON ").replace(f"{table}", "callrecord")
                for name, d in rows if not name.endswith("_pkey")}
    async with db.session_maker() as session:
        live = (await session.execute(defs, {"name": "callrecord"})).all()
        staged = (await session.execute(defs, {"name": "callrecord_partitioned"})).all()
    assert normalized(live, "callrecord") == normalized(staged, "callrecord_partitioned")
    assert len(live) > 10


async def test_each_run_resyncs_verdicts_that_landed_after_the_copy(before_swap):
    first = await partitions.backfill(31, batch_rows=10, pause=0)
    note = json.loads(await _scalar("SELECT obj_description('callrecord_partitioned'::regclass, 'pg_class')"))
    assert note[partitions.NOTE_RESYNCED_UPTO] == 40 and partitions.NOTE_RESYNC_PASS not in note
    assert first["done"]
    async with db.session_maker() as session:     # 20 hours old: inside the 25-hour look-back
        await session.execute(text("UPDATE callrecord SET hit = true, verdict = 'valid' WHERE id = 20"))
        await session.commit()
    again = await partitions.backfill(31, batch_rows=10, pause=0)
    assert again["resynced"] == 1
    assert await _scalar("SELECT verdict FROM callrecord_partitioned WHERE id = 20") == "valid"


async def test_a_resync_pass_ends_with_a_short_pass_to_the_current_top(before_swap, monkeypatch):
    """Rows copied while a pass runs (another copy, a catch-up) get their late values too."""
    await partitions.backfill(31, batch_rows=10, pause=0)
    real_note, injected = partitions._write_note, []

    async def copy_arrives_mid_pass(db_, note):
        await real_note(db_, note)
        if partitions.NOTE_RESYNC_PASS in note and not injected:
            injected.append(1)
            async with db.session_maker() as session:
                await session.execute(_ROWS, {"org_id": before_swap, "count": 1,
                                              "step": timedelta(seconds=1), "newest": utcnow_naive()})
                await session.execute(text(
                    "INSERT INTO callrecord_partitioned SELECT * FROM callrecord WHERE id = 41"))
                await session.execute(text("UPDATE callrecord SET verdict = 'valid' WHERE id = 41"))
                await session.commit()
    monkeypatch.setattr(partitions, "_write_note", copy_arrives_mid_pass)
    async with db.session_maker() as session:     # make the next run's pass have work to do
        await session.execute(text("UPDATE callrecord SET verdict = 'invalid' WHERE id = 39"))
        await session.commit()
    await partitions.backfill(31, batch_rows=10, pause=0)
    assert injected
    assert await _scalar("SELECT verdict FROM callrecord_partitioned WHERE id = 41") == "valid"
    note = json.loads(await _scalar("SELECT obj_description('callrecord_partitioned'::regclass, 'pg_class')"))
    assert note[partitions.NOTE_RESYNCED_UPTO] == 41


async def test_the_orphan_sweep_paces_between_delete_batches(before_swap):
    await partitions.backfill(31, batch_rows=10, pause=0)
    async with db.session_maker() as session:
        await session.execute(text("DELETE FROM callrecord WHERE org_id = :id"), {"id": before_swap})
        await session.execute(text("DELETE FROM org WHERE id = :id"), {"id": before_swap})
        await session.commit()
    paused = []

    async def between():
        paused.append(1)
    assert await partitions.delete_rows_of_deleted_teams(batch=10, between_batches=between) == 40
    assert len(paused) == 4


async def test_a_column_added_during_a_run_is_copied_from_the_next_batch_on(before_swap, monkeypatch):
    added = []

    async def lag() -> float:
        if not added:
            added.append(1)
            async with db.session_maker() as session:
                await session.execute(text("ALTER TABLE callrecord ADD COLUMN extra text DEFAULT 'x'"))
                await session.execute(text("ALTER TABLE callrecord_partitioned ADD COLUMN extra text"))
                await session.commit()
        return 0.0
    monkeypatch.setattr(partitions, "replica_lag_seconds", lag)
    out = await partitions.backfill(31, batch_rows=10, pause=0, max_replica_lag=5)
    assert out["done"] and added
    assert await _scalar("SELECT count(*) FROM callrecord_partitioned WHERE extra = 'x'") == 30


async def test_a_first_pass_split_over_two_runs_resyncs_from_the_first_run(before_swap, monkeypatch):
    """The first run copies the newest rows and stops on its time budget; a verdict lands on one
    of them; the second run, two days later, finishes the copy and the resync. With no completed
    pass yet, the resync looks back from when the first run began, not from the second run."""
    first = await partitions.backfill(5, batch_rows=4, max_seconds=0, pause=0)
    assert first["done"] is False
    assert await _scalar("SELECT min(id) FROM callrecord_partitioned") == 37
    async with db.session_maker() as session:
        await session.execute(text("UPDATE callrecord SET hit = true WHERE id = 38"))
        await session.commit()
    later = utcnow_naive() + timedelta(days=2)
    monkeypatch.setattr(partitions, "utcnow_naive", lambda: later)
    second = await partitions.backfill(5, batch_rows=4, pause=0)
    assert second["done"] is True
    assert await _scalar("SELECT hit FROM callrecord_partitioned WHERE id = 38") is True


async def test_a_second_backfill_while_one_runs_does_nothing(before_swap):
    async with db._engine.connect() as other:
        assert (await other.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                    {"k": partitions.BACKFILL_LOCK_KEY})).scalar_one()
        busy = await partitions.backfill(31, batch_rows=10, pause=0)
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": partitions.BACKFILL_LOCK_KEY})
    assert busy["busy"] is True and busy["copied"] == 0
    assert await _scalar("SELECT count(*) FROM callrecord_partitioned") == 0
    assert (await partitions.backfill(31, batch_rows=10, pause=0))["copied"] == 40


async def test_a_resync_batch_reads_only_its_id_range_of_the_staged_table(before_swap):
    """The resync joins the staged table to the live one by id; both sides carry the batch's
    bounds, so the staged side is probed within the range instead of read whole."""
    await partitions.backfill(31, batch_rows=10, pause=0)
    compiled = str(partitions._RESYNC.compile())
    async with db.session_maker() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join((await session.execute(
            text("EXPLAIN " + compiled.replace(":after", "10").replace(":upto", "20")))).scalars().all())
    lines = plan.splitlines()
    staged_conditions = [lines[i + 1] for i, line in enumerate(lines[:-1])
                         if "Scan" in line and " on callrecord_y" in line]
    assert staged_conditions and all("id > 10" in c for c in staged_conditions), plan


def test_a_busy_backfill_exits_nonzero(monkeypatch, capsys):
    async def busy(days, **options):
        return {"phase": None, "copied": 0, "done": False, "busy": True}
    monkeypatch.setattr(partitions, "backfill", busy)
    assert worker.main(["admin", "callrecord-backfill", "--days", "31"]) == 1
    assert "another backfill is running" in capsys.readouterr().err
