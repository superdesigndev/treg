"""Archive row retention: whole questions no one asked for go, with their snapshots, marks and
orphaned objects; everything something still needs stays (docs/context/architecture/archive.md,
"Row retention")."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import time
from datetime import timedelta
from importlib.resources import files

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from httpx import AsyncClient
from sqlalchemy import select, text

from treg import archive, archive_bodies, bootstrap, worker
from treg.application import archive_retention
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import (
    ArchiveEndpointStat, ArchiveKey, ArchiveKeyOrg, ArchiveObjectPending, ArchiveRetentionCursor,
    ArchiveSnapshot,
    IdempotentCall,
)
from treg.timeutil import utcnow_naive
from tests.fake_object_store import MemoryObjectStore
from tests.test_archive import EP, platform_on, serve  # noqa: F401 - fixtures


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _key(name: str, *, age_days: float, asked_days: float | None = None,
               endpoint: str = EP, policy: str = "transient", req_url: str | None = None,
               bodies: tuple[tuple[str, str | None], ...] = (("a", None),)) -> int:
    """One key created `age_days` ago, last asked `asked_days` ago (default: when created), with
    one snapshot per `(body name, body_storage)`; a dedup chain like the recorder writes it."""
    now = utcnow_naive()
    created = now - timedelta(days=age_days)
    asked = now - timedelta(days=age_days if asked_days is None else asked_days)
    async with session_maker() as s:
        key = ArchiveKey(key_hash=_h(name), endpoint_id=endpoint, provider="tikhub", policy=policy,
                         fetched_at=created, created_at=created, last_requested_at=asked,
                         req_method="GET", req_url=req_url or f"https://api.tikhub.io/x?q={name}")
        s.add(key)
        await s.flush()
        carrier = None
        for version, (body, storage) in enumerate(bodies, start=1):
            raw = body.encode()
            snap = ArchiveSnapshot(key_id=key.id, version=version, content_hash=_h(body),
                                   size_bytes=len(raw), fetched_at=created, body_storage=storage)
            if storage in (None, "db", "both"):
                if carrier is not None and carrier[1] == _h(body):
                    snap.body_of = carrier[0]
                else:
                    snap.body = raw
            s.add(snap)
            await s.flush()
            if snap.body is not None:
                carrier = (snap.id, _h(body))
        key.result_state, key.result_snapshot_id = "found", snap.id
        await s.commit()
        return int(key.id)


async def _mark(name: str, *, org_id: int = 1, days_ago: float = 40) -> None:
    at = utcnow_naive() - timedelta(days=days_ago)
    async with session_maker() as s:
        s.add(ArchiveKeyOrg(org_id=org_id, key_hash=_h(name), first_call_at=at, last_call_at=at))
        await s.commit()


async def _left() -> tuple[set[str], int, int]:
    async with session_maker() as s:
        keys = set((await s.execute(select(ArchiveKey.key_hash))).scalars().all())
        snaps = len((await s.execute(select(ArchiveSnapshot.id))).all())
        marks = len((await s.execute(select(ArchiveKeyOrg.key_hash))).all())
    return keys, snaps, marks


@pytest.fixture
def retention_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_retention_enabled", True)
    monkeypatch.setattr(get_settings(), "archive_retention_pause_s", 0)
    monkeypatch.setattr(archive_retention, "MIN_OBJECT_GRACE_S", 0)   # tests make objects due at once


@pytest.fixture
def store():
    s = MemoryObjectStore()
    bootstrap.configure_archive_object_store(s)
    yield s
    bootstrap.configure_archive_object_store(None)


async def test_a_question_no_one_asked_for_goes_whole_and_everything_still_needed_stays(
        clients: AsyncClient, retention_on, monkeypatch):
    other_ep = next(e for e in catalog_store.load().by_id if e != EP)
    monkeypatch.setitem(catalog_store.load().by_id[other_ep], "cache", "archive")
    await _key("dormant", age_days=40, bodies=(("a", None), ("a", None), ("b", None)))
    await _mark("dormant", org_id=1)
    await _mark("dormant", org_id=2)
    await _key("never-asked", age_days=40, asked_days=None)
    async with session_maker() as s:            # NULL demand: a key only treg itself fetched
        key = (await s.execute(select(ArchiveKey).where(ArchiveKey.key_hash == _h("never-asked")))).scalar_one()
        key.last_requested_at = None
        await s.commit()
    await _key("asked-lately", age_days=40, asked_days=3)
    await _key("young", age_days=5)
    await _key("archive-recorded", age_days=40, policy="archive")
    await _key("archive-now", age_days=40, endpoint=other_ep)
    await _key("terminal", age_days=40, req_url="treg://asynctasks/call-1")
    await _key("paid-lately", age_days=40)
    await _mark("paid-lately", days_ago=2)
    await _key("retry-row", age_days=40)
    async with session_maker() as s:
        s.add(IdempotentCall(org_id=1, membership_id=1, key="k", status="done",
                             expires_at=utcnow_naive() + timedelta(hours=12),
                             archive_key_hash=_h("retry-row"), archive_content_hash=_h("a")))
        s.add(ArchiveEndpointStat(endpoint_id=EP, provider="tikhub", keys=9, snapshots=11,
                                  bodies_kept=11, kept_bytes=11))
        await s.commit()

    result = await archive_retention.run()

    assert (result["keys"], result["snapshots"], result["marks"]) == (2, 4, 2)
    assert result["pass_complete"] and result["stopped"] == "pass_complete"
    keys, snaps, marks = await _left()
    assert keys == {_h(n) for n in ("asked-lately", "young", "archive-recorded", "archive-now",
                                    "terminal", "paid-lately", "retry-row")}
    assert (snaps, marks) == (7, 1)
    async with session_maker() as s:
        stat = await s.get(ArchiveEndpointStat, EP)
        # 2 keys, 4 snapshots; 3 of them kept bytes (one is a dedup reference), 1+1+1 bytes
        assert (stat.keys, stat.snapshots, stat.bodies_kept, stat.kept_bytes) == (7, 7, 8, 8)
        # Every pointer a surviving key or snapshot holds still names a row.
        ids = set((await s.execute(select(ArchiveSnapshot.id))).scalars().all())
        pointers = set((await s.execute(select(ArchiveKey.result_snapshot_id))).scalars().all())
        carriers = set((await s.execute(select(ArchiveSnapshot.body_of)
                                        .where(ArchiveSnapshot.body_of.is_not(None)))).scalars().all())
        assert pointers - {None} <= ids and carriers <= ids
    # The team-facing read of a deleted question answers cleanly: no longer on file.
    assert await archive.resolve_result(_h("dormant"), _h("a")) is None
    assert await archive.resolve_result(_h("asked-lately"), _h("a")) is not None


async def test_off_by_default_and_a_dry_run_counts_without_deleting(clients: AsyncClient, monkeypatch):
    await _key("dormant", age_days=40, bodies=(("a", "r2"), ("b", None)))
    await _mark("dormant")
    assert get_settings().archive_retention_enabled is False
    off = await archive_retention.run()
    assert off["stopped"] == "disabled" and off["keys"] == 0
    dry = await archive_retention.run(dry_run=True, pause_s=0)
    assert (dry["keys"], dry["snapshots"], dry["marks"], dry["objects"]) == (1, 2, 1, 1)
    assert (await _left())[0] == {_h("dormant")}
    async with session_maker() as s:
        assert await s.get(ArchiveRetentionCursor, archive_retention.CURSOR_ID) is None


async def test_orphaned_objects_go_after_the_grace_period_and_shared_ones_stay(
        clients: AsyncClient, retention_on, store):
    for body in ("only-here", "shared"):
        store.objects[_h(body)] = body.encode()
    await _key("dormant", age_days=40, bodies=(("only-here", "r2"), ("shared", "both")))
    await _key("kept", age_days=40, asked_days=1, bodies=(("shared", "r2"),))
    result = await archive_retention.run()
    assert (result["keys"], result["objects_pending"], result["objects"]) == (1, 1, 0)
    assert set(store.objects) == {_h("only-here"), _h("shared")}    # rows first, bytes later
    early = await archive_retention.run()                           # inside the grace period
    assert early["objects"] == 0 and _h("only-here") in store.objects
    later = await archive_retention.run(object_grace_s=0)
    assert later["objects"] == 1 and set(store.objects) == {_h("shared")}
    async with session_maker() as s:
        rows = (await s.execute(select(ArchiveObjectPending))).scalars().all()
    assert [(r.content_hash, r.deleting, r.deleted_at is not None) for r in rows] \
        == [(_h("only-here"), True, True)]                              # the tombstone


async def test_a_failed_or_impossible_object_delete_waits_for_the_next_run(
        clients: AsyncClient, retention_on, store):
    await _key("dormant", age_days=40, bodies=(("a", "r2"),))
    store.objects[_h("a")] = b"a"
    assert (await archive_retention.run())["keys"] == 1                 # rows go first

    real_delete = store.delete

    async def broken(_hash):
        raise RuntimeError("store down")
    store.delete = broken
    failed = await archive_retention.run(object_grace_s=0)
    assert failed["objects"] == 0 and _h("a") in store.objects

    bootstrap.configure_archive_object_store(None)
    missing = await archive_retention.run(object_grace_s=0)
    assert missing["stopped"] == "error" and "object store" in missing["error"]

    store.delete = real_delete
    bootstrap.configure_archive_object_store(store)
    assert (await archive_retention.run(object_grace_s=0))["objects"] == 1
    assert store.objects == {}


async def test_a_run_stops_at_its_time_budget_and_the_next_resumes(clients: AsyncClient, retention_on):
    ids = [await _key(f"dormant-{i}", age_days=40) for i in range(3)]
    ticks = iter([0, 0, 0, 0, 10, 10])     # start, object deletes, batch 1, batch 2, then over budget
    first = await archive_retention.run(batch=1, max_seconds=5, after=ids[0] - 1,
                                        clock=lambda: next(ticks))
    assert (first["keys"], first["stopped"], first["next_after"]) == (2, "time_budget", ids[1])
    async with session_maker() as s:
        assert (await s.get(ArchiveRetentionCursor, archive_retention.CURSOR_ID)).after_id == ids[1]
    second = await archive_retention.run(batch=1)
    assert (second["start_after"], second["keys"], second["pass_complete"]) == (ids[1], 1, True)
    async with session_maker() as s:
        cursor = await s.get(ArchiveRetentionCursor, archive_retention.CURSOR_ID)
        assert (cursor.after_id, cursor.passes) == (0, 1)


async def test_a_recording_whose_key_went_meanwhile_records_it_again(clients: AsyncClient, serve,
                                                                      monkeypatch):
    """The recorder reads the key unlocked, then locks it; retention may delete it in between."""
    from sqlalchemy.exc import NoResultFound

    url = "https://api.tikhub.io/x?aweme_id=9"
    await archive._store(method="GET", endpoint_id=EP, provider="tikhub", url=url, caller_body=b"",
                         headers={}, status_code=200, media_type="application/json", body=b'{"n":1}')
    real, calls = archive._lock_archive_key, []

    async def deleted_then_real(s, key_id):
        calls.append(key_id)
        if len(calls) == 1:
            async with session_maker() as other:
                from treg.domain.governance.teams import erase_archive_keys
                await erase_archive_keys(other, [key_id])
                await other.commit()
            raise NoResultFound()
        return await real(s, key_id)
    monkeypatch.setattr(archive, "_lock_archive_key", deleted_then_real)
    await archive._store(method="GET", endpoint_id=EP, provider="tikhub", url=url, caller_body=b"",
                         headers={}, status_code=200, media_type="application/json", body=b'{"n":2}')
    async with session_maker() as s:
        keys = (await s.execute(select(ArchiveKey))).scalars().all()
        snaps = (await s.execute(select(ArchiveSnapshot))).scalars().all()
    assert len(keys) == 1 and [x.version for x in snaps] == [1] and len(calls) == 2


async def test_the_worker_command(clients: AsyncClient, monkeypatch, capsys):
    await _key("dormant", age_days=40)
    args = argparse.Namespace(dry_run=True, days=None, batch_size=None, pause_seconds=0,
                              max_seconds=None, max_keys=None, after=None)
    assert await worker._archive_retain(args) == 0
    assert json.loads(capsys.readouterr().out)["keys"] == 1
    args.dry_run = False
    assert await worker._archive_retain(args) == 0
    assert json.loads(capsys.readouterr().out)["stopped"] == "disabled"
    monkeypatch.setattr(get_settings(), "archive_retention_enabled", True)
    assert await worker._archive_retain(args) == 0
    assert json.loads(capsys.readouterr().out)["keys"] == 1
    assert (await _left())[0] == set()


async def test_every_statement_is_capped_on_postgres(clients: AsyncClient, retention_on):
    from sqlalchemy import event
    await _key("dormant", age_days=40)
    engine = session_maker.kw["bind"].sync_engine
    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        seen.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert (await archive_retention.run())["keys"] == 1
    finally:
        event.remove(engine, "before_cursor_execute", record)
    caps = [s for s in seen if s.startswith("SET LOCAL statement_timeout")]
    if engine.dialect.name != "postgresql":
        assert caps == []
        return
    deletes = [s for s in seen if s.lstrip().upper().startswith("DELETE")]
    assert deletes and len(caps) >= 3
    assert not any("NOT IN (SELECT" in s.upper() for s in seen)


def test_a_batch_too_large_to_bind_is_refused_up_front():
    with pytest.raises(ValueError, match="batch 1..10000"):
        asyncio.run(archive_retention.run(dry_run=True, batch=archive_retention.MAX_BATCH + 1))


async def test_a_payment_on_a_mark_being_deleted_does_not_wait_behind_the_job(
        clients: AsyncClient, retention_on, monkeypatch):
    """The job's slow deletes run before it touches any mark, so a settle upserting a mark (under
    its team's org row lock) never queues behind them; a mark paid for meanwhile stays."""
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("row locks are PostgreSQL's")
    await _key("dormant", age_days=40)
    await _mark("dormant", org_id=1)
    real_erase_keys = archive_retention.erase_archive_keys
    waited: list[float] = []

    async def pay_while_the_job_deletes(db, key_ids, **options):
        gone = await real_erase_keys(db, key_ids, **options)     # the slow statements, still uncommitted
        started = time.monotonic()
        async with session_maker() as payment:
            await payment.execute(text("SET LOCAL lock_timeout = '2s'"))
            await archive.note_org_use_in_transaction(payment, 1, _h("dormant"))
            await payment.commit()
        waited.append(time.monotonic() - started)
        return gone
    monkeypatch.setattr(archive_retention, "erase_archive_keys", pay_while_the_job_deletes)

    result = await asyncio.wait_for(archive_retention.run(), timeout=10)

    assert waited and waited[0] < 1.0
    assert (result["keys"], result["marks"]) == (1, 0)
    async with session_maker() as s:
        mark = (await s.execute(select(ArchiveKeyOrg))).scalar_one()
        assert mark.calls == 2 and mark.last_call_at > utcnow_naive() - timedelta(minutes=1)


async def test_the_live_retry_check_reads_the_partial_index(clients: AsyncClient):
    """Both reads of a batch ask per candidate whether a live retry row replays from it; on
    PostgreSQL that is an index probe, not a scan of the whole idempotentcall table."""
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("the plan is PostgreSQL's")
    now = utcnow_naive()
    query = (select(ArchiveKey.id).where(ArchiveKey.id > 0, ArchiveKey.id <= 5000,
                                         *archive_retention.dormant(now - timedelta(days=30), now)))
    async with session_maker() as s:
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        compiled = query.compile(s.get_bind(), compile_kwargs={"literal_binds": True})
        plan = "\n".join((await s.execute(text(f"EXPLAIN {compiled}"))).scalars().all())
    # The planner may probe the new index per candidate or hash the live trimmed rows read
    # through the other partial index; either way it never reads the whole table.
    assert "ix_idempotentcall_archive_key_hash" in plan or "ix_idempotentcall_archive_live" in plan
    assert "Seq Scan on idempotentcall" not in plan
    async with session_maker() as s:
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        probe = "\n".join((await s.execute(text(
            "EXPLAIN SELECT 1 FROM idempotentcall WHERE archive_key_hash = 'k' "
            "AND archive_content_hash IS NOT NULL"))).scalars().all())
    assert "ix_idempotentcall_archive_key_hash" in probe


def _revision(prefix: str):
    path = next(files("treg").joinpath("alembic", "versions").glob(f"{prefix}_*.py"))
    spec = importlib.util.spec_from_file_location(f"revision_{prefix}_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_revision(module) -> None:
    def run(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            module.upgrade()
    async with session_maker.kw["bind"].connect() as connection:
        await connection.run_sync(run)
        await connection.commit()


async def test_a_large_idempotentcall_is_refused_and_the_manual_build_finishes_it(clients: AsyncClient):
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("the concurrent build is PostgreSQL's")
    module = _revision("0123")
    async with session_maker() as s:
        await s.execute(text(f"DROP INDEX IF EXISTS {module.NAME}"))
        for i in range(3):
            s.add(IdempotentCall(org_id=1, membership_id=1, key=f"k{i}", status="done",
                                 expires_at=utcnow_naive() + timedelta(hours=1)))
        await s.commit()
    module.BUILD_IN_MIGRATION_MAX_ROWS = 2

    with pytest.raises(RuntimeError, match="CREATE INDEX CONCURRENTLY") as refused:
        await _run_revision(module)
    validity = text("SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                    "WHERE c.relname = :name")
    async with session_maker() as s:
        assert (await s.execute(validity, {"name": module.NAME})).scalar() is None

    statements = [line.strip().rstrip(";") for line in str(refused.value).splitlines()[1:]
                  if line.strip() and not line.strip().startswith("SELECT")]
    async with session_maker.kw["bind"].connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for statement in statements:
            await connection.execute(text(statement))
    await _run_revision(module)        # a valid index: nothing to build, no refusal
    async with session_maker() as s:
        assert (await s.execute(validity, {"name": module.NAME})).scalar() is True


async def test_a_run_stops_after_max_keys_and_the_next_resumes(clients: AsyncClient, retention_on):
    for i in range(3):
        await _key(f"dormant-{i}", age_days=40)
    first = await archive_retention.run(batch=1, max_keys=2, after=0)
    assert (first["keys"], first["stopped"]) == (2, "max_keys")
    second = await archive_retention.run(batch=1, max_keys=2)
    assert (second["keys"], second["pass_complete"]) == (1, True)
    assert (await _left())[0] == set()


async def test_a_dry_run_reads_through_the_read_session_maker(clients: AsyncClient, monkeypatch, capsys):
    import treg.infra.db as infra_db

    opened = []

    def replica():
        opened.append(1)
        return session_maker()
    monkeypatch.setattr(infra_db, "read_session_maker", replica)
    await _key("dormant", age_days=40)
    args = argparse.Namespace(dry_run=True, days=None, batch_size=None, pause_seconds=0,
                              max_seconds=None, max_keys=None, after=None)
    assert await worker._archive_retain(args) == 0
    assert json.loads(capsys.readouterr().out)["keys"] == 1
    assert opened


async def test_a_second_run_while_one_works_answers_busy(clients: AsyncClient, retention_on):
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("advisory locks are PostgreSQL's")
    await _key("dormant", age_days=40)
    async with session_maker.kw["bind"].connect() as other:
        assert (await other.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                    {"k": archive_retention.RUN_LOCK_KEY})).scalar_one()
        busy = await archive_retention.run()
        assert (busy["stopped"], busy["keys"]) == ("busy", 0)
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": archive_retention.RUN_LOCK_KEY})
    assert (await archive_retention.run())["keys"] == 1


async def test_a_mark_a_settle_holds_is_skipped_not_waited_for(clients: AsyncClient, retention_on):
    """A settle that already upserted a mark (holding its row lock, uncommitted) must not make
    the job's mark delete wait: the job skips that mark and still deletes the key."""
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("row locks are PostgreSQL's")
    await _key("dormant", age_days=40)
    await _mark("dormant", org_id=1)
    await _mark("dormant", org_id=2)
    async with session_maker() as settle:
        await archive.note_org_use_in_transaction(settle, 1, _h("dormant"))   # holds the row lock
        result = await asyncio.wait_for(archive_retention.run(), timeout=10)
        await settle.commit()
    assert (result["keys"], result["marks"], result["skipped_batches"]) == (1, 1, 0)
    async with session_maker() as s:
        marks = (await s.execute(select(ArchiveKeyOrg))).scalars().all()
    assert [(m.org_id, m.calls) for m in marks] == [(1, 2)]


async def test_a_rolled_back_batch_deletes_no_object(clients: AsyncClient, retention_on, store,
                                                     monkeypatch):
    """The reviewer's lock-timeout case: another key records X after the candidate read and a
    session holds rows the batch needs, so the batch rolls back. Nothing in object storage was
    touched before the rows committed, so X stays and every snapshot naming it stays honest."""
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("row locks are PostgreSQL's")
    store.objects[_h("x")] = b"x"
    await _key("dormant-a", age_days=40, bodies=(("x", "r2"),))
    real_erase, held = archive_retention.erase_archive_keys, {}

    async def record_lock_then_erase(db, key_ids, **options):
        await _key("young-b", age_days=0.01, bodies=(("x", "r2"),))
        lock = session_maker()
        await lock.__aenter__()
        await lock.execute(text("SELECT id FROM archivesnapshot WHERE key_id = ANY(:ids) FOR UPDATE"),
                           {"ids": key_ids})
        held["lock"] = lock
        return await real_erase(db, key_ids, **options)
    monkeypatch.setattr(archive_retention, "erase_archive_keys", record_lock_then_erase)
    first = await archive_retention.run(object_grace_s=0)
    await held["lock"].rollback()
    await held["lock"].__aexit__(None, None, None)
    monkeypatch.setattr(archive_retention, "erase_archive_keys", real_erase)
    second = await archive_retention.run(object_grace_s=0)
    third = await archive_retention.run(object_grace_s=0)

    assert first["skipped_batches"] == 1 and second["keys"] == 1
    assert first["objects"] == second["objects"] == third["objects"] == 0
    async with session_maker() as s:
        on_file = await archive.bytes_on_file(s, {(_h("young-b"), _h("x"))})
    assert _h("x") in store.objects and on_file == {(_h("young-b"), _h("x"))}


async def test_a_recorder_that_saw_the_object_and_commits_later_keeps_its_bytes(
        clients: AsyncClient, retention_on, store, monkeypatch):
    """The reviewer's recorder case: a recorder found X in object storage (so it skipped the
    upload) and commits its snapshot only after the batch committed. The batch records X as
    pending; the object is deleted only after the grace period and only if still unreferenced,
    re-checked right before the delete, so X stays."""
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("concurrent sessions are PostgreSQL's")
    store.objects[_h("x")] = b"x"
    await _key("dormant-a", age_days=40, bodies=(("x", "r2"),))
    real_erase, held = archive_retention.erase_archive_keys, {}

    async def recorder_in_flight_then_erase(db, key_ids, **options):
        rec = session_maker()
        await rec.__aenter__()
        key = ArchiveKey(key_hash=_h("young-b"), endpoint_id=EP, provider="tikhub",
                         policy="transient", req_method="GET", req_url="https://x/y?q=b")
        rec.add(key)
        await rec.flush()
        rec.add(ArchiveSnapshot(key_id=key.id, version=1, content_hash=_h("x"), size_bytes=1,
                                body_storage="r2"))
        await rec.flush()                      # uncommitted: the batch cannot see it
        held["rec"] = rec
        return await real_erase(db, key_ids, **options)
    monkeypatch.setattr(archive_retention, "erase_archive_keys", recorder_in_flight_then_erase)

    result = await archive_retention.run()
    assert (result["keys"], result["objects_pending"], result["objects"]) == (1, 1, 0)
    await held["rec"].commit()
    await held["rec"].__aexit__(None, None, None)
    monkeypatch.setattr(archive_retention, "erase_archive_keys", real_erase)
    later = await archive_retention.run(object_grace_s=0)

    assert (later["objects"], later["objects_kept"]) == (0, 1)
    async with session_maker() as s:
        on_file = await archive.bytes_on_file(s, {(_h("young-b"), _h("x"))})
        assert (await s.execute(select(ArchiveObjectPending))).scalars().all() == []
    assert _h("x") in store.objects and on_file == {(_h("young-b"), _h("x"))}


async def _pending_hashes() -> set[str]:
    async with session_maker() as s:
        return set((await s.execute(select(ArchiveObjectPending.content_hash)
                                    .where(ArchiveObjectPending.deleted_at.is_(None)))).scalars().all())


async def test_a_recorder_that_confirmed_the_object_before_the_delete_keeps_the_bytes(
        clients: AsyncClient, serve, retention_on, store, monkeypatch):
    """A recorder confirms the object (so it skips the upload) after the batch committed, the due
    delete then takes the object, and the recorder commits last. It sees the `deleting` mark (the
    tombstone stays one grace period) and keeps the bytes in its row: the answer stays readable."""
    body = b'{"n":1}'
    ch = archive.content_hash(body)
    store.objects[ch] = body
    await _key("dormant-a", age_days=40, bodies=((body.decode(), "r2"),))
    assert (await archive_retention.run())["objects_pending"] == 1
    real_prepare = archive_bodies.prepare

    async def confirmed_then_deleted(*args, **kwargs):
        plan = await real_prepare(*args, **kwargs)        # the object is there: no upload
        assert plan.storage == "r2"
        later = await archive_retention.run(object_grace_s=0)
        assert later["objects"] == 1 and ch not in store.objects
        return plan
    monkeypatch.setattr(archive_bodies, "prepare", confirmed_then_deleted)
    await archive._store(method="GET", endpoint_id=EP, provider="tikhub",
                         url="https://api.tikhub.io/x?aweme_id=late", caller_body=b"", headers={},
                         status_code=200, media_type="application/json", body=body,
                         plan=archive_bodies.WritePlan("r2"))
    async with session_maker() as s:
        late = (await s.execute(select(ArchiveKey).where(ArchiveKey.key_hash != _h("dormant-a")))).scalar_one()
        snap = (await s.execute(select(ArchiveSnapshot).where(ArchiveSnapshot.key_id == late.id))).scalar_one()
    assert snap.body_storage == "db"
    assert await archive.answer_bytes(late.key_hash, ch) == body


async def test_more_than_a_page_of_due_objects_all_go_in_one_run(clients: AsyncClient, retention_on, store):
    now = utcnow_naive()
    for i in range(1200):
        store.objects[_h(f"b{i}")] = b"."
    async with session_maker() as s:
        s.add_all([ArchiveObjectPending(content_hash=_h(f"b{i}"), recorded_at=now - timedelta(hours=1))
                   for i in range(1200)])
        await s.commit()
    result = await archive_retention.run(object_grace_s=0, max_seconds=3600)
    assert result["objects"] == 1200 and store.objects == {} and await _pending_hashes() == set()


async def test_failing_objects_move_back_and_do_not_block_the_rest(clients: AsyncClient, retention_on,
                                                                   store):
    now = utcnow_naive()
    for i in range(3):
        store.objects[_h(f"b{i}")] = b"."
    async with session_maker() as s:
        s.add_all([ArchiveObjectPending(content_hash=_h(f"b{i}"), recorded_at=now - timedelta(hours=3 - i))
                   for i in range(3)])
        await s.commit()
    real = store.delete

    async def fail_two_oldest(content_hash):
        if content_hash in (_h("b0"), _h("b1")):
            raise RuntimeError("permanent 403")
        return await real(content_hash)
    store.delete = fail_two_oldest
    result = archive_retention.RetentionResult(enabled=True, dry_run=False)
    await archive_retention._delete_due_objects(result, grace_s=60, session_factory=session_maker,
                                                spent=lambda: False, page=2)
    assert (result.objects, result.objects_failed) == (1, 2)
    assert _h("b2") not in store.objects
    async with session_maker() as s:
        rows = {r.content_hash: r for r in (await s.execute(select(ArchiveObjectPending))).scalars().all()}
    assert {rows[_h("b0")].attempts, rows[_h("b1")].attempts} == {1}
    assert rows[_h("b0")].recorded_at > now - timedelta(minutes=1)      # back of the queue
    assert rows[_h("b2")].deleted_at is not None                         # the tombstone


async def test_a_trim_racing_retention_never_leaves_a_paid_replay_without_its_answer(
        clients: AsyncClient, platform_on, retention_on, store, monkeypatch):
    """Trim checks the answer is on file, retention deletes that key, trim would then clear the
    row's own copy: the key lock and the re-check in the trim's UPDATE keep the copy."""
    from treg.application.call import idempotency
    from treg.models import CallRecord
    from tests.test_marketplace_call import _seed_answer

    body = b'{"rows":[1]}'
    row_id = await _seed_answer(clients, "idem-1", body=body)
    await _key("k1", age_days=40, bodies=((body.decode(), "db"),))
    async with session_maker() as s:
        row = await s.get(IdempotentCall, row_id)
        row.call_ref = "call-1"
        row.created_at = utcnow_naive() - timedelta(hours=1)
        s.add(CallRecord(org_id=row.org_id, user_email="t@x.dev", tool_name="t", call_ref="call-1",
                         endpoint_id=EP, method="GET", path="/x", status_code=200,
                         archive_key_hash=_h("k1"), archive_content_hash=archive.content_hash(body)))
        await s.commit()
    real = archive.bytes_on_file

    async def on_file_then_retention(db_, pairs):
        found = await real(db_, pairs)
        assert (await archive_retention.run())["keys"] == 1      # another worker, in between
        return found
    monkeypatch.setattr(archive, "bytes_on_file", on_file_then_retention)
    trimmed = await idempotency.trim_archived_answers(pause_s=0)
    async with session_maker() as s:
        row = await s.get(IdempotentCall, row_id)
    assert trimmed.trimmed == 0 and row.response_body == body and row.archive_content_hash is None


async def test_the_trim_holds_keys_only_for_its_updates(clients: AsyncClient, platform_on, monkeypatch):
    """The trim reads and hashes the bodies first and locks the keys only for the updates, so a
    recorder (which locks its key) never waits behind a slow page."""
    from treg.application.call import idempotency
    from treg.models import CallRecord
    from tests.test_marketplace_call import _seed_answer

    body = b'{"rows":[2]}'
    row_id = await _seed_answer(clients, "idem-2", body=body)
    await _key("k2", age_days=1, bodies=((body.decode(), "db"),))
    async with session_maker() as s:
        row = await s.get(IdempotentCall, row_id)
        row.call_ref = "call-2"
        row.created_at = utcnow_naive() - timedelta(hours=1)
        s.add(CallRecord(org_id=row.org_id, user_email="t@x.dev", tool_name="t", call_ref="call-2",
                         endpoint_id=EP, method="GET", path="/x", status_code=200,
                         archive_key_hash=_h("k2"), archive_content_hash=archive.content_hash(body)))
        await s.commit()
    real_hash, real_lock, steps = archive.content_hash, archive.lock_keys_for_share, []

    def hash_(raw):
        steps.append("hash")
        return real_hash(raw)

    async def lock(db_, hashes):
        steps.append("lock")
        return await real_lock(db_, hashes)
    monkeypatch.setattr(archive, "content_hash", hash_)
    monkeypatch.setattr(archive, "lock_keys_for_share", lock)
    trimmed = await idempotency.trim_archived_answers(pause_s=0)
    assert trimmed.trimmed == 1
    first_lock = steps.index("lock")
    assert "hash" in steps[:first_lock] and "hash" not in steps[first_lock:]


async def test_a_held_pending_row_never_stops_the_run(clients: AsyncClient, retention_on, store):
    """Another session holds one pending row: marking it times out, and so does moving it back.
    The run logs it and deletes the other objects."""
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("row locks are PostgreSQL's")
    now = utcnow_naive()
    for name in ("held", "free"):
        store.objects[_h(name)] = b"."
    async with session_maker() as s:
        s.add_all([ArchiveObjectPending(content_hash=_h(name), recorded_at=now - timedelta(hours=1))
                   for name in ("held", "free")])
        await s.commit()
    async with session_maker() as holder:
        await holder.execute(text("SELECT 1 FROM archiveobjectpending WHERE content_hash = :h FOR UPDATE"),
                             {"h": _h("held")})
        result = await archive_retention.run(object_grace_s=0)
        await holder.rollback()
    assert result["stopped"] == "pass_complete" and result["objects"] == 1
    assert result["objects_failed"] == 1
    assert _h("held") in store.objects and _h("free") not in store.objects


async def test_a_grace_period_shorter_than_a_recording_is_refused(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_retention_enabled", True)
    with pytest.raises(ValueError, match="grace period must be at least"):
        await archive_retention.run(object_grace_s=archive_retention.MIN_OBJECT_GRACE_S - 1)
    assert (await archive_retention.run(dry_run=True, object_grace_s=0))["dry_run"] is True
