"""The swap: the partitioned table takes the name `callrecord` (alembic 0142).

The PostgreSQL cases run against a real server (`TREG_TEST_DB_URL`, the Postgres CI job): the swap
keeps ids, cursors, grants and concurrent audit writes; a large table is swapped only after its
history was copied, recently; the lock waits for long statements without queuing inserts; month
partitions are created without waiting for a writer and dropped by the retention setting.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import timedelta
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from treg import audit
from treg.application import call_record_partitions as partitions
from treg.config import get_settings
from treg.domain.governance.teams import cascade_delete_org
from treg.infra import db
from treg.models import CallRecord, CallUsageCursor, Org
from treg.timeutil import utcnow_naive
from tests.test_alembic_baseline import _drop_everything, _upgrade_to



swap = import_module("treg.alembic.versions.0142_call_record_swap")
INT4_MAX = 2_147_483_647

_ROWS = text("""
    INSERT INTO callrecord (org_id, user_email, tool_name, method, path, status_code, kind, client,
                            call_ref, budget_dim, budget_val, created_at, cached)
    SELECT :org_id, 'member@example.test', 'echo', 'GET', '/', 200, 'call', '', 'ref-' || n, '', '',
           CAST(:newest AS timestamp) - (:count - n) * CAST(:step AS interval), false
    FROM generate_series(1, :count) AS n
""")


async def _swap_in_process() -> None:
    """Run 0142 here, not through Alembic's own loader, so a test can change its constants."""
    def run(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            swap.upgrade()

    await _upgrade_to("0141")
    async with db._engine.connect() as connection:
        await connection.run_sync(run)
        await connection.commit()
    await db._engine.dispose()   # as after a deploy: no connection outlives the swap


async def _scalar(sql: str, **params):
    async with db.session_maker() as session:
        return (await session.execute(text(sql), params)).scalar()


@pytest.fixture
async def before_swap(monkeypatch):
    """A database at 0140 with one team and 40 calls, one per hour, the newest a minute old."""
    if db._engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to("0140")
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


async def test_swap_keeps_every_id_and_lets_ids_pass_32_bits(before_swap):
    async with db.session_maker() as session:
        await session.execute(text("DROP ROLE IF EXISTS callrecord_reader"))
        await session.execute(text("CREATE ROLE callrecord_reader"))
        await session.execute(text("GRANT SELECT (id, endpoint_id) ON callrecord TO callrecord_reader"))
        await session.commit()
    try:
        await _upgrade_to("head")

        assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "p"
        assert await _scalar("SELECT count(*) FROM callrecord_legacy") == 40
        async with db.session_maker() as session:
            assert (await session.execute(select(CallRecord.id).order_by(CallRecord.id))).scalars().all() \
                == list(range(1, 41))
            names = set((await session.execute(text(
                "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = 'callrecord'::regclass"))).scalars())
            assert names == {index.name for index in CallRecord.__table__.indexes} | {"callrecord_pkey"}
            assert (await session.execute(text(
                "SELECT has_column_privilege('callrecord_reader', 'callrecord', 'endpoint_id', 'SELECT')"
                " AND NOT has_column_privilege('callrecord_reader', 'callrecord', 'user_email', 'SELECT')"
            ))).scalar_one()

            # The id continues from the old table's sequence, then passes the 32-bit range.
            session.add(next_row := CallRecord(user_email="a@example.test", tool_name="echo",
                                               method="GET", path="/", status_code=200))
            await session.flush()
            assert next_row.id == 41
            await session.execute(text(
                "SELECT setval(pg_get_serial_sequence('callrecord', 'id'), :value)"), {"value": INT4_MAX})
            session.add(wide := CallRecord(user_email="a@example.test", tool_name="echo",
                                           method="GET", path="/", status_code=200))
            session.add(CallUsageCursor(id="callrecord", cursor_id=INT4_MAX + 1, history_from="2026-01-01"))
            await session.commit()
            assert wide.id == INT4_MAX + 1
            assert (await session.get(CallRecord, INT4_MAX + 1)).tool_name == "echo"
    finally:
        async with db.session_maker() as session:
            await session.execute(text("DROP OWNED BY callrecord_reader"))
            await session.execute(text("DROP ROLE callrecord_reader"))
            await session.commit()


async def test_audit_rows_written_during_the_swap_all_land_in_the_new_table(before_swap):
    """The writer's connection prepared its INSERT against the old table. The INSERT returns
    nothing, so the id's change of type under it cannot refuse it, and every row lands."""
    written = 0
    upgrade = asyncio.create_task(_upgrade_to("head"))
    while not upgrade.done():
        audit.record_call(org_id=before_swap, user_email="w@example.test", tool_name="during-swap",
                          method="GET", path="/", status_code=200)
        written += 1
        await asyncio.sleep(0.002)
    await upgrade
    for _ in range(50):
        audit.record_call(org_id=before_swap, user_email="w@example.test", tool_name="during-swap",
                          method="GET", path="/", status_code=200)
        written += 1
    await audit.drain()

    assert await _scalar("SELECT count(*) FROM callrecord WHERE tool_name = 'during-swap'") == written
    assert await _scalar("SELECT count(*) FROM callrecord") == 40 + written


async def test_every_pooled_writer_connection_lands_its_first_row_after_the_swap(before_swap, caplog):
    """Each pooled background connection prepared the audit INSERT against the old table. Behind a
    transaction pooler that statement lives on, one per query text, after the swap; it returns
    nothing, so the id's change of type cannot refuse it. Writers on several such connections at
    once land every row, and none is logged as dropped."""
    writers = 6
    fields = dict(org_id=before_swap, user_email="w@example.test", tool_name="stale-plan",
                  method="GET", path="/", status_code=200)

    async def open_connections() -> None:
        # Each session holds its connection until all are open, so the pool holds `writers`.
        opened = asyncio.Barrier(writers)

        async def hold() -> None:
            async with db.background_session_maker() as session:
                await session.execute(text("SELECT 1"))
                await opened.wait()

        await asyncio.gather(*(hold() for _ in range(writers)))

    async def write_on_held_connections() -> list[bool]:
        await open_connections()
        # All at once: each writer takes one of the idle connections, none waits for another.
        return await asyncio.gather(*(audit._write_batch([(CallRecord, dict(fields))])
                                      for _ in range(writers)))

    # Before the swap: each of the pool's connections prepares the INSERT.
    assert await write_on_held_connections() == [True] * writers
    assert db._background_engine.pool.checkedin() >= writers

    await _swap_in_process()
    assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "p"

    caplog.clear()
    assert await write_on_held_connections() == [True] * writers
    assert "audit write dropped" not in caplog.text
    assert await _scalar("SELECT count(*) FROM callrecord WHERE tool_name = 'stale-plan'") == 2 * writers


async def test_a_large_table_is_not_swapped_until_its_history_was_copied(before_swap, monkeypatch):
    """Readers count "today" and rebuild recent windows from call records, so a large table must
    not start with only its newest rows. Nothing changes until the copy reaches back far enough."""
    monkeypatch.setattr(swap, "_INLINE_IDS", 10)
    with pytest.raises(RuntimeError, match="callrecord-backfill"):
        await _swap_in_process()
    assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "r"

    # One day of 40 hourly rows is copied; the swap asks for more than that.
    copied = await partitions.backfill(1, batch_rows=10, pause=0)
    assert copied["phase"] == "before_swap" and copied["done"] is True
    with pytest.raises(RuntimeError, match="the copy reaches back to"):
        await _swap_in_process()

    # Reaching the first row is enough, however young that row is.
    await partitions.backfill(31, batch_rows=10, pause=0)
    assert await _scalar("SELECT count(*) FROM callrecord_partitioned") == 40
    await _swap_in_process()
    assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "p"
    assert await _scalar("SELECT count(*) FROM callrecord") == 40


async def test_rows_written_or_changed_after_the_copy_reach_the_new_table(before_swap, monkeypatch):
    """Between the copy and the swap the old table is still live: new calls arrive, an async call
    gets its verdict, and a team is deleted."""
    monkeypatch.setattr(swap, "_INLINE_IDS", 10)
    monkeypatch.setattr(swap, "_COMMIT_MARGIN", 2)
    monkeypatch.setattr(partitions, "COMMIT_MARGIN", 2)
    await _upgrade_to("0141")
    assert (await partitions.backfill(31, batch_rows=10, pause=0))["copied"] == 40

    async with db.session_maker() as session:
        session.add(gone := Org(name="gone", slug="gone"))
        await session.flush()
        await session.execute(_ROWS, {"org_id": gone.id, "count": 3, "step": timedelta(seconds=1),
                                      "newest": utcnow_naive()})
        await session.commit()
        gone_id = gone.id
    # A second run copies only what was written since the first.
    assert (await partitions.backfill(31, batch_rows=10, pause=0))["copied"] == 3

    async with db.session_maker() as session:
        await session.execute(text("UPDATE callrecord SET hit = true, verdict = 'valid' WHERE id = 40"))
        await session.execute(text("DELETE FROM callrecord WHERE org_id = :id"), {"id": gone_id})
        await session.execute(text("DELETE FROM org WHERE id = :id"), {"id": gone_id})
        await session.execute(_ROWS, {"org_id": before_swap, "count": 2, "step": timedelta(seconds=1),
                                      "newest": utcnow_naive()})
        await session.commit()

    # Run once more just before the deploy: it clears the deleted team's copies and resyncs the
    # late verdict; the swap then covers only what came after.
    assert (await partitions.backfill(31, batch_rows=10, pause=0))["deleted_orphans"] == 3
    await _swap_in_process()
    async with db.session_maker() as session:
        assert (await session.execute(select(CallRecord.id).order_by(CallRecord.id))).scalars().all() \
            == [*range(1, 41), 44, 45]
        late = await session.get(CallRecord, 40)
        assert (late.hit, late.verdict) == (True, "valid")


async def test_downgrade_puts_a_plain_table_back_with_every_row(before_swap):
    from alembic import command
    from treg.maintenance import _alembic_config

    await _upgrade_to("head")
    async with db.session_maker() as session:
        session.add(CallRecord(user_email="a@example.test", tool_name="after-swap", method="GET",
                               path="/", status_code=200))
        await session.commit()
    await db._engine.dispose()
    await asyncio.to_thread(command.downgrade, _alembic_config(), "0140")

    assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "r"
    assert await _scalar("SELECT count(*) FROM callrecord") == 41
    assert await _scalar("SELECT to_regclass('callrecord_partitioned')") is None
    assert await _scalar("INSERT INTO callrecord (user_email, tool_name, method, path, status_code, "
                         "kind, client, call_ref, budget_dim, budget_val, created_at, cached) VALUES "
                         "('a', 'b', 'GET', '/', 200, 'call', '', '', '', '', now(), false) RETURNING id") == 42


async def test_older_history_can_follow_after_the_swap(before_swap, monkeypatch):
    monkeypatch.setattr(swap, "_INLINE_IDS", 10)
    monkeypatch.setattr(swap, "_MIN_HISTORY_DAYS", 1)
    await _upgrade_to("0141")
    await partitions.backfill(1, batch_rows=100, pause=0)
    await _swap_in_process()
    assert await _scalar("SELECT count(*) FROM callrecord") == 40   # one batch, taken whole

    async with db.session_maker() as session:
        await session.execute(text("DELETE FROM callrecord WHERE id <= 30"))
        await session.commit()
    more = await partitions.backfill(90, batch_rows=7, pause=0)
    assert (more["phase"], more["copied"], more["done"]) == ("after_swap", 30, True)
    assert await _scalar("SELECT count(*) FROM callrecord") == 40


async def test_team_deletion_clears_the_legacy_rows_and_the_table_can_be_dropped(before_swap):
    await _upgrade_to("head")
    async with db.session_maker() as session:
        org = await session.get(Org, before_swap)
        await cascade_delete_org(org, session)
        await session.commit()
    assert await _scalar("SELECT count(*) FROM callrecord_legacy") == 0
    assert await _scalar("SELECT count(*) FROM callrecord") == 0

    assert await partitions.drop_legacy() == {"dropped": True, "deleted_rows": 0, "error": None}
    assert await partitions.drop_legacy() == {"dropped": False, "deleted_rows": 0, "error": None}
    # The id sequence belongs to the new table and outlives the legacy one.
    assert await _scalar("SELECT nextval(pg_get_serial_sequence('callrecord', 'id'))") == 41


async def test_month_partitions_are_created_ahead_and_dropped_by_retention(before_swap, monkeypatch):
    await _upgrade_to("head")
    today = utcnow_naive().date()
    ahead = {partitions._name(partitions._month(today, offset))
             for offset in range(partitions.MONTHS_AHEAD + 1)}
    async with db.session_maker() as session:
        assert ahead <= {name for name, _, _ in (await partitions._month_tables(session)).values()}

    old = partitions._month(today, -5)
    assert await partitions.ensure([old]) == [partitions._name(old)]
    assert await partitions.ensure([old]) == []
    async with db.session_maker() as session:
        session.add(CallRecord(user_email="a@example.test", tool_name="old", method="GET", path="/",
                               status_code=200, created_at=utcnow_naive().replace(
                                   year=old.year, month=old.month, day=2)))
        await session.commit()

    assert {k: v for k, v in (await partitions.maintain()).items() if k != "analyzed"} \
        == {"created": [], "dropped": []}   # retention is off
    monkeypatch.setattr(get_settings(), "callrecord_retention_months", 2)
    kept = await _scalar("SELECT count(*) FROM callrecord WHERE tool_name <> 'old'")
    assert (await partitions.maintain())["dropped"] == [partitions._name(old)]
    assert await _scalar("SELECT to_regclass(:name)", name=partitions._name(old)) is None
    assert await _scalar("SELECT count(*) FROM callrecord") == kept


async def test_a_new_partition_does_not_wait_for_an_open_audit_insert(before_swap):
    await _upgrade_to("head")
    far = partitions._month(utcnow_naive().date(), 24)
    async with db.session_maker() as writer:
        writer.add(CallRecord(user_email="a@example.test", tool_name="open", method="GET", path="/",
                              status_code=200))
        await writer.flush()   # holds its lock on `callrecord` until the block ends
        assert await asyncio.wait_for(partitions.ensure([far]), timeout=2) == [partitions._name(far)]
        await writer.commit()


async def test_rows_written_during_the_resync_are_copied_before_the_lock(before_swap, monkeypatch):
    """The resync of late verdicts reads days of rows while audit inserts continue. What they
    wrote meanwhile is copied after it, without a lock, so the swap copies at most `_SWAP_ROWS`
    new rows under its ACCESS EXCLUSIVE lock however long the resync took."""
    monkeypatch.setattr(swap, "_SWAP_ROWS", 5)
    monkeypatch.setattr(swap, "_COMMIT_MARGIN", 2)
    real_resync, real_swap, left_under_lock = swap._resync_verdicts, swap._swap, []

    arrived = []

    def resync_while_calls_arrive(bind, reached):
        real_resync(bind, reached)
        if not arrived:                    # during the first resync, the long one
            arrived.append(1)
            bind.execute(_ROWS, {"org_id": before_swap, "count": 60, "step": timedelta(milliseconds=1),
                                 "newest": utcnow_naive()})

    def swap_and_measure(bind, columns, reached):
        left_under_lock.append(bind.execute(text("SELECT max(id) FROM callrecord")).scalar() - reached)
        real_swap(bind, columns, reached)
    monkeypatch.setattr(swap, "_resync_verdicts", resync_while_calls_arrive)
    monkeypatch.setattr(swap, "_swap", swap_and_measure)

    await _swap_in_process()

    assert left_under_lock and left_under_lock[0] <= swap._SWAP_ROWS
    assert await _scalar("SELECT count(*) FROM callrecord") == 100


async def test_a_stale_backfill_is_refused_until_it_runs_again(before_swap, monkeypatch):
    """More than `_MAX_CATCH_UP_IDS` ids written since the last backfill run: the swap refuses
    at once, changing nothing, and asks for the backfill again."""
    monkeypatch.setattr(swap, "_INLINE_IDS", 10)
    monkeypatch.setattr(swap, "_MAX_CATCH_UP_IDS", 5)
    await _upgrade_to("0141")
    await partitions.backfill(31, batch_rows=10, pause=0)
    async with db.session_maker() as session:
        await session.execute(_ROWS, {"org_id": before_swap, "count": 10, "step": timedelta(seconds=1),
                                      "newest": utcnow_naive()})
        await session.commit()
    with pytest.raises(RuntimeError, match="callrecord-backfill --days N` again"):
        await _swap_in_process()
    assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "r"

    await partitions.backfill(31, batch_rows=10, pause=0)
    await _swap_in_process()
    assert await _scalar("SELECT count(*) FROM callrecord") == 50


async def test_rows_of_teams_deleted_during_the_copy_go_in_committed_batches(before_swap):
    """Not in the upgrade: a large deleted team must never keep 0142 running after its swap."""
    async with db.session_maker() as session:
        session.add(gone := Org(name="gone", slug="gone"))
        await session.flush()
        gone_id = gone.id
        await session.execute(_ROWS, {"org_id": gone_id, "count": 25, "step": timedelta(seconds=1),
                                      "newest": utcnow_naive()})
        await session.commit()
    await _upgrade_to("head")
    async with db.session_maker() as session:     # deleted outside `cascade_delete_org`
        await session.execute(text("DELETE FROM callrecord_legacy WHERE org_id = :id"), {"id": gone_id})
        await session.execute(text("DELETE FROM org WHERE id = :id"), {"id": gone_id})
        await session.commit()
    assert await _scalar("SELECT count(*) FROM callrecord WHERE org_id = :id", id=gone_id) == 25

    assert await partitions.delete_rows_of_deleted_teams(batch=10) == 25
    assert await _scalar("SELECT count(*) FROM callrecord WHERE org_id = :id", id=gone_id) == 0
    assert await _scalar("SELECT count(*) FROM callrecord") == 40


async def test_the_admin_total_sums_the_partitions_and_the_legacy_table(before_swap):
    from treg.routers.admin import admin_stats

    await _upgrade_to("head")
    async with db.session_maker() as session:
        await session.execute(text("ANALYZE callrecord"))
        await session.execute(text("ANALYZE callrecord_legacy"))
        await session.commit()
    async with db.session_maker() as session:
        stats = await admin_stats("admin", session)
    # 40 rows in the partitions, and the same 40 still in the legacy table.
    assert stats["totals"]["calls"] == 80
    assert (await partitions.drop_legacy())["dropped"] is True
    async with db.session_maker() as session:
        assert (await admin_stats("admin", session))["totals"]["calls"] == 40


async def test_a_stopped_upgrade_resumes_the_resync_where_it_stopped(before_swap, monkeypatch):
    """The resync notes `resynced_upto` after each batch, so an upgrade killed half way does not
    read the rows it already resynced again."""
    monkeypatch.setattr(swap, "_BATCH_ROWS", 5)
    real_note, notes = swap._write_note, []

    def killed_after_three(bind, note):
        real_note(bind, note)
        notes.append(note["resynced_upto"])
        if len(notes) == 3:
            raise RuntimeError("killed")
    monkeypatch.setattr(swap, "_write_note", killed_after_three)
    with pytest.raises(RuntimeError, match="killed"):
        await _swap_in_process()
    assert notes == [20, 25, 30]           # the last 25 hours of hourly rows: ids 16 to 40

    resyncs: list[str] = []

    def count(conn, cursor, statement, *args):
        if statement.lstrip().startswith("UPDATE callrecord_partitioned"):
            resyncs.append(statement)
    monkeypatch.setattr(swap, "_write_note", real_note)
    sqlalchemy_event.listen(db._engine.sync_engine, "before_cursor_execute", count)
    try:
        await _swap_in_process()
    finally:
        sqlalchemy_event.remove(db._engine.sync_engine, "before_cursor_execute", count)
    assert len(resyncs) == 3            # ids 31 to 40 in two batches, not 16 to 40 again, then
                                        # the locked copy's range under the lock
    assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "p"
    assert await _scalar("SELECT obj_description('callrecord'::regclass, 'pg_class')") is None


async def test_the_backfill_resyncs_late_verdicts_and_the_swap_only_what_came_after(before_swap, monkeypatch):
    """A verdict that lands after its row was copied reaches the copy at the next backfill run;
    the swap resyncs only above where the last run got, and refuses when that is too far."""
    monkeypatch.setattr(swap, "_INLINE_IDS", 10)
    await _upgrade_to("0141")
    first = await partitions.backfill(31, batch_rows=10, pause=0)
    note = json.loads(await _scalar("SELECT obj_description('callrecord_partitioned'::regclass, 'pg_class')"))
    assert note["resynced_upto"] == 40 and "resync_pass" not in note and first["done"]
    async with db.session_maker() as session:
        await session.execute(text("UPDATE callrecord SET hit = true, verdict = 'valid' WHERE id = 20"))
        await session.commit()
    again = await partitions.backfill(31, batch_rows=10, pause=0)
    assert again["resynced"] == 1
    assert await _scalar("SELECT verdict FROM callrecord_partitioned WHERE id = 20") == "valid"

    monkeypatch.setattr(swap, "_MAX_RESYNC_IDS", 5)
    async with db.session_maker() as session:
        await session.execute(_ROWS, {"org_id": before_swap, "count": 6, "step": timedelta(seconds=1),
                                      "newest": utcnow_naive()})
        await session.commit()
    with pytest.raises(RuntimeError, match="above the backfill's last late-verdict resync"):
        await _swap_in_process()
    await partitions.backfill(31, batch_rows=10, pause=0)
    await _swap_in_process()
    assert await _scalar("SELECT count(*) FROM callrecord") == 46


async def test_an_unfinished_backfill_resync_is_refused(before_swap, monkeypatch):
    monkeypatch.setattr(swap, "_INLINE_IDS", 10)
    await _upgrade_to("0141")
    assert (await partitions.backfill(31, batch_rows=10, pause=0))["done"] is True
    async with db.session_maker() as session:     # as if the run stopped inside its resync
        await session.execute(text(
            "COMMENT ON TABLE callrecord_partitioned IS "
            "'{\"resync_pass\": {\"began\": \"2026-01-01T00:00:00\", \"cursor\": 10}}'"))
        await session.commit()
    with pytest.raises(RuntimeError, match="resync finished"):
        await _swap_in_process()


async def test_the_lock_never_stalls_inserts_behind_reports_or_a_long_transaction(before_swap):
    """A report runs 3 s at a time with short gaps, and a transaction keeps reading for 20 s.
    The swap waits for quiet without queuing, then tries its locks 50 ms at a time: inserts never
    stall for longer than about 0.2 s, and every one lands."""
    stop = asyncio.Event()

    async def reports():
        async with db._engine.connect() as connection:
            while not stop.is_set():
                await connection.execute(text(
                    "SELECT pg_sleep(3) FROM (SELECT 1 FROM callrecord LIMIT 1) AS one"))
                await connection.commit()
                await asyncio.sleep(0.05)

    async def long_transaction():
        async with db._engine.connect() as connection:
            ends = time.monotonic() + 20
            while time.monotonic() < ends:
                await connection.execute(text("SELECT count(*) FROM callrecord WHERE id < 5"))
                await asyncio.sleep(0.2)   # each statement short, the transaction long
            await connection.commit()

    readers = [asyncio.create_task(reports()), asyncio.create_task(long_transaction())]
    await asyncio.sleep(1.0)
    upgrade = asyncio.create_task(_upgrade_to("head"))
    slowest, written = 0.0, 0
    try:
        while not upgrade.done():
            started = time.monotonic()
            for attempt in (1, 2):   # a statement prepared before the swap fails once, as documented
                try:
                    async with db.session_maker() as session:
                        session.add(CallRecord(org_id=before_swap, user_email="w@example.test",
                                               tool_name="during", method="GET", path="/", status_code=200))
                        await session.commit()
                    break
                except DBAPIError:
                    if attempt == 2:
                        raise
            slowest, written = max(slowest, time.monotonic() - started), written + 1
            await asyncio.sleep(0.02)
        await upgrade
    finally:
        stop.set()
        await asyncio.gather(*readers, return_exceptions=True)

    # Each lock attempt waits at most 50 ms; the rest of the bound is the locked step itself and a
    # busy CI machine. A lock request that queued behind the reports would stall for seconds.
    assert slowest < 1.0 and written > 200
    assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "p"
    assert await _scalar("SELECT count(*) FROM callrecord WHERE tool_name = 'during'") == written


async def test_the_locked_copy_stays_small_after_a_long_quiet_wait(before_swap, monkeypatch):
    """Rows written while the swap waits for quiet are copied before the lock, not under it."""
    monkeypatch.setattr(swap, "_SWAP_ROWS", 5)
    monkeypatch.setattr(swap, "_COMMIT_MARGIN", 2)
    real_wait, real_copy, left_under_lock = swap._wait_until_quiet, swap._copy_under_lock, []

    def calls_arrive_while_waiting(bind, table, deadline):
        bind.execute(_ROWS, {"org_id": before_swap, "count": 300, "step": timedelta(milliseconds=1),
                             "newest": utcnow_naive()})
        real_wait(bind, table, deadline)

    def copy_and_measure(bind, columns, reached):
        left_under_lock.append(bind.execute(text("SELECT max(id) FROM callrecord")).scalar() - reached)
        real_copy(bind, columns, reached)
    monkeypatch.setattr(swap, "_wait_until_quiet", calls_arrive_while_waiting)
    monkeypatch.setattr(swap, "_copy_under_lock", copy_and_measure)

    await _swap_in_process()

    assert left_under_lock and left_under_lock[0] <= swap._SWAP_ROWS
    assert await _scalar("SELECT count(*) FROM callrecord") == 340


async def test_drop_legacy_holds_no_lock_on_org_while_the_table_goes(before_swap):
    """The foreign key to `org` goes first, in its own short transaction; the table drop then
    needs no lock on `org`, so a transaction writing an org row does not hold it up."""
    await _upgrade_to("head")
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement.strip().split("\\n")[0])
    sqlalchemy_event.listen(db._engine.sync_engine, "before_cursor_execute", record)
    try:
        async with db.session_maker() as session:
            await partitions._drop_legacy_foreign_keys(session)
    finally:
        sqlalchemy_event.remove(db._engine.sync_engine, "before_cursor_execute", record)
    assert any(s.startswith("ALTER TABLE callrecord_legacy DROP CONSTRAINT") for s in statements)
    assert await _scalar("SELECT count(*) FROM pg_constraint WHERE conrelid = 'callrecord_legacy'::regclass "
                         "AND contype = 'f'") == 0

    async with db.session_maker() as writer:       # an org write, open throughout the drop
        await writer.execute(text("UPDATE org SET name = name WHERE id = :id"), {"id": before_swap})
        result = await asyncio.wait_for(partitions.drop_legacy(), timeout=5)
        org_locks = await _scalar(
            "SELECT count(*) FROM pg_locks WHERE relation = 'org'::regclass AND NOT granted")
        await writer.commit()
    assert result["dropped"] is True and org_locks == 0


async def test_the_upkeep_analyzes_the_partitioned_table_when_its_estimate_falls_behind(before_swap):
    await _upgrade_to("head")
    async with db.session_maker() as session:
        await session.execute(_ROWS, {"org_id": before_swap, "count": 100, "step": timedelta(seconds=1),
                                      "newest": utcnow_naive()})
        await session.commit()
        for name in (await session.execute(text(
                "SELECT inhrelid::regclass::text FROM pg_inherits "
                "WHERE inhparent = 'callrecord'::regclass"))).scalars().all():
            await session.execute(text(f"ANALYZE {name}"))
        await session.commit()
    assert (await partitions.maintain())["analyzed"] == ["callrecord"]
    assert (await partitions.maintain())["analyzed"] == []


async def test_the_swap_finishes_while_readers_never_pause(before_swap):
    """Readers run back-to-back transactions of up to 0.3 s on `callrecord`. Each poll for quiet
    must see the sessions as they are now (the activity snapshot is cleared), so the swap finds
    its moment instead of waiting out its window."""
    stop = asyncio.Event()

    async def reader(seed: int):
        rng = random.Random(seed)
        async with db._engine.connect() as connection:
            while not stop.is_set():
                await connection.execute(text("SELECT count(*) FROM callrecord WHERE id < 5"))
                await connection.execute(text(f"SELECT pg_sleep({rng.uniform(0, 0.3):.3f})"))
                await connection.commit()

    readers = [asyncio.create_task(reader(seed)) for seed in (1, 2, 3, 4)]
    await asyncio.sleep(0.5)
    started = time.monotonic()
    try:
        await asyncio.wait_for(_upgrade_to("head"), timeout=90)
    finally:
        stop.set()
        await asyncio.gather(*readers, return_exceptions=True)
    assert time.monotonic() - started < 60
    assert await _scalar("SELECT relkind::text FROM pg_class WHERE oid = 'callrecord'::regclass") == "p"


async def test_the_foreign_key_drop_never_stalls_org_reads(before_swap):
    """A transaction holds `org` for 25 s while short calls keep reading it. The foreign key drop
    waits for quiet and then tries 50 ms at a time, so an `org` read never waits for long."""
    await _upgrade_to("head")
    stop = asyncio.Event()
    slowest = [0.0]

    async def long_holder():
        async with db._engine.connect() as connection:
            await connection.execute(text("SELECT id FROM org WHERE id = :id"), {"id": before_swap})
            await asyncio.sleep(25)
            await connection.commit()

    async def org_reads():
        async with db._engine.connect() as connection:
            while not stop.is_set():
                started = time.monotonic()
                await connection.execute(text("SELECT name FROM org WHERE id = :id"), {"id": before_swap})
                await connection.commit()
                slowest[0] = max(slowest[0], time.monotonic() - started)
                await asyncio.sleep(0.005)

    tasks = [asyncio.create_task(long_holder()), asyncio.create_task(org_reads())]
    await asyncio.sleep(0.5)
    try:
        result = await asyncio.wait_for(partitions.drop_legacy(), timeout=90)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert result["dropped"] is True
    # Each attempt waits at most 50 ms (plus a busy CI machine); queuing behind the 25 s holder
    # would stall for its whole lock wait.
    assert slowest[0] < 0.5


async def test_partitions_cover_the_oldest_row_to_copy_whatever_its_id(before_swap):
    """An id is handed out before its row commits, and a row may carry an older timestamp than
    rows with lower ids: the partitions start at the oldest `created_at` still to copy."""
    async with db.session_maker() as session:
        await session.execute(_ROWS, {"org_id": before_swap, "count": 1, "step": timedelta(seconds=1),
                                      "newest": utcnow_naive() - timedelta(days=95)})
        await session.commit()
    await _swap_in_process()
    assert await _scalar("SELECT count(*) FROM callrecord") == 41


async def test_late_values_landing_during_the_quiet_wait_are_resynced_before_the_lock(before_swap,
                                                                                      monkeypatch):
    """A row copied after the first resync gets its verdict while the swap waits for quiet; the
    resync after the wait takes it, so the locked copy (which skips rows already copied) does not
    leave it stale."""
    real_wait, done = swap._wait_until_quiet, []

    def verdict_lands_while_waiting(bind, table, deadline):
        if not done:
            done.append(1)
            bind.execute(_ROWS, {"org_id": before_swap, "count": 1, "step": timedelta(seconds=1),
                                 "newest": utcnow_naive()})
            bind.execute(text("INSERT INTO callrecord_partitioned SELECT * FROM callrecord WHERE id = 41"))
            bind.execute(text("UPDATE callrecord SET hit = true, verdict = 'valid' WHERE id = 41"))
        real_wait(bind, table, deadline)
    monkeypatch.setattr(swap, "_wait_until_quiet", verdict_lands_while_waiting)

    await _swap_in_process()

    async with db.session_maker() as session:
        row = await session.get(CallRecord, 41)
        assert (row.hit, row.verdict) == (True, "valid")


async def test_a_late_charge_is_resynced_by_the_swap_like_a_late_verdict(before_swap, monkeypatch):
    """An async call's settled charge lands on its record late, as its verdict does; the swap's
    own resync carries it to the new table."""
    real_wait, done = swap._wait_until_quiet, []

    def charge_lands_while_waiting(bind, table, deadline):
        if not done:
            done.append(1)
            bind.execute(_ROWS, {"org_id": before_swap, "count": 1, "step": timedelta(seconds=1),
                                 "newest": utcnow_naive()})
            bind.execute(text("INSERT INTO callrecord_partitioned SELECT * FROM callrecord WHERE id = 41"))
            bind.execute(text("UPDATE callrecord SET cost_charged_micro = 1234 WHERE id = 41"))
        real_wait(bind, table, deadline)
    monkeypatch.setattr(swap, "_wait_until_quiet", charge_lands_while_waiting)

    await _swap_in_process()

    async with db.session_maker() as session:
        assert (await session.get(CallRecord, 41)).cost_charged_micro == 1234


async def test_late_values_landing_just_before_the_lock_reach_the_new_table(before_swap, monkeypatch):
    """A row already in the new table gets its late verdict and charge after the last resync and
    before the lock is granted. The locked copy skips rows already copied, so the swap resyncs the
    range it copies, under the lock."""
    real_try, done = swap._try_locks, []

    def values_land_before_the_lock(bind):
        if not done:
            done.append(1)
            bind.execute(text("UPDATE callrecord SET hit = true, verdict = 'valid', "
                              "cost_charged_micro = 77 WHERE id = 40"))
        return real_try(bind)
    monkeypatch.setattr(swap, "_try_locks", values_land_before_the_lock)

    await _swap_in_process()

    async with db.session_maker() as session:
        row = await session.get(CallRecord, 40)
        assert (row.hit, row.verdict, row.cost_charged_micro) == (True, "valid", 77)


def test_a_refusal_names_a_vacuum_that_holds_the_table():
    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class Bind:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, statement, params):
            return Rows(self.rows)

    assert swap._vacuum_note(Bind([]), "callrecord") == ""
    note = swap._vacuum_note(Bind([(4242, "scanning heap")]), "callrecord")
    assert "VACUUM of callrecord is running (pid 4242 (scanning heap))" in note
    assert "pg_stat_progress_vacuum" in note


async def test_the_upkeep_leaves_the_staged_table_to_the_swap(before_swap):
    """Before the swap the staged table is analyzed by 0142 itself; the cron analyzes only the
    live `callrecord` once it is partitioned."""
    await _upgrade_to("0141")
    await partitions.backfill(31, batch_rows=10, pause=0)
    async with db.session_maker() as session:
        for name in (await session.execute(text(
                "SELECT inhrelid::regclass::text FROM pg_inherits "
                "WHERE inhparent = 'callrecord_partitioned'::regclass"))).scalars().all():
            await session.execute(text(f"ANALYZE {name}"))
        await session.commit()
    assert await partitions.analyze_if_stale() == []


def test_drop_legacy_takes_a_window(monkeypatch):
    from treg import worker
    import treg.infra.db as infra_db

    seen = {}

    async def verified():
        return None

    async def drop(**options):
        seen.update(options)
        return {"dropped": False, "deleted_rows": 0, "error": "window"}
    monkeypatch.setattr(infra_db, "verify_db", verified)
    monkeypatch.setattr(partitions, "drop_legacy", drop)
    assert worker.main(["admin", "callrecord-drop-legacy"]) == 1
    assert seen == {"window_s": 120.0}
    worker.main(["admin", "callrecord-drop-legacy", "--window-s", "600"])
    assert seen == {"window_s": 600.0}


async def test_the_swap_resync_reads_only_its_id_range_of_the_new_table(before_swap):
    await _upgrade_to("0141")
    await partitions.backfill(31, batch_rows=10, pause=0)
    compiled = str(swap._RESYNC.compile())
    async with db.session_maker() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join((await session.execute(
            text("EXPLAIN " + compiled.replace(":after", "10").replace(":upto", "20")))).scalars().all())
    lines = plan.splitlines()
    staged_conditions = [lines[i + 1] for i, line in enumerate(lines[:-1])
                         if "Scan" in line and " on callrecord_y" in line]
    assert staged_conditions and all("id > 10" in c for c in staged_conditions), plan
