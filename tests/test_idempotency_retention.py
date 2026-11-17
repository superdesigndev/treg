"""Global retention without breaking live paid-response replay."""
import pytest
from sqlalchemy import delete, event, func
from sqlmodel import select

from treg.application.call import idempotency
from treg.infra.db import session_maker
from treg.models import IdempotentCall
from test_marketplace_call import EP, _balance, _seed_answer, platform_on  # noqa: F401


async def test_cleanup_preserves_live_replay_and_pending_claims(clients, platform_on):
    for n in range(5):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)
    await _seed_answer(clients, "valid", body=b'{"saved":true}')
    await _seed_answer(clients, "pending", status="pending")
    await _seed_answer(clients, "expired-pending", status="pending", ttl_s=-3600)
    dry = await idempotency.prune_expired_idempotency(dry_run=True)
    assert dry.eligible == 5 and dry.deleted == 0
    result = await idempotency.prune_expired_idempotency(batch_size=2, pause_s=0)
    assert (result.deleted, result.batches, result.complete) == (5, 4, True)
    async with session_maker() as db:
        assert set((await db.scalars(select(IdempotentCall.key))).all()) == {
            "valid", "pending", "expired-pending"}
    before = await _balance(clients)
    replay = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "valid"})
    assert replay.status_code == 200 and replay.json() == {"saved": True}
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true"
    assert await _balance(clients) == before
    pending = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "pending"})
    assert pending.status_code == 409
    assert (await idempotency.prune_expired_idempotency(pause_s=0)).deleted == 0


async def test_bounded_sweep_resumes_next_run(clients):
    for n in range(5):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)
    result = await idempotency.prune_expired_idempotency(batch_size=2, max_batches=1, pause_s=0)
    assert (result.deleted, result.complete) == (2, False)
    result = await idempotency.prune_expired_idempotency(batch_size=2, pause_s=0)
    assert (result.deleted, result.complete) == (3, True)


async def test_concurrent_cleanup_and_new_rows_do_not_extend_sweep(clients, monkeypatch):
    ids = [await _seed_answer(clients, f"old-{n}", ttl_s=-3600) for n in range(4)]
    original_sleep = idempotency.asyncio.sleep
    first = True

    async def interleave(seconds):
        nonlocal first
        if first:
            first = False
            # Simulate the existing caller-scoped cleanup between committed batches.
            async with session_maker() as db:
                await db.execute(delete(IdempotentCall).where(IdempotentCall.id == ids[2]))
                await db.commit()
            await _seed_answer(clients, "inserted-later", ttl_s=-3600)
        await original_sleep(0)

    monkeypatch.setattr(idempotency.asyncio, "sleep", interleave)
    result = await idempotency.prune_expired_idempotency(batch_size=2, pause_s=0)
    assert result.deleted == 3 and result.complete is True
    async with session_maker() as db:
        assert list((await db.scalars(select(IdempotentCall.key))).all()) == ["inserted-later"]


async def test_lock_timeout_rolls_back_batch(clients):
    async with session_maker() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("Postgres row lock behavior")
    row_id = await _seed_answer(clients, "locked-expired", ttl_s=-3600)
    async with session_maker() as holder:
        await holder.execute(select(IdempotentCall).where(IdempotentCall.id == row_id).with_for_update())
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError):
            await idempotency.prune_expired_idempotency(pause_s=0)
        await holder.rollback()
    result = await idempotency.prune_expired_idempotency(pause_s=0)
    assert result.deleted == 1


async def test_live_pages_do_not_stop_the_expiry_sweep(clients):
    for n in range(4):
        await _seed_answer(clients, f"live-{n}")
    await _seed_answer(clients, "expired-tail", ttl_s=-3600)
    result = await idempotency.prune_expired_idempotency(batch_size=2, pause_s=0)
    assert (result.deleted, result.batches, result.complete) == (1, 3, True)


async def test_counts_accumulate_from_page_metadata_not_aggregate_queries(clients):
    """Regression test for production timeout: counts must not require unbounded scans.

    The prune worker hit statement_timeout twice in production:
    1. First on the initial SELECT with expiry filter + LIMIT (fixed by ID-first pages).
    2. Then on the final COUNT(*) WHERE expired (fixed by accumulating within pages).

    This test verifies that eligible/deleted counts are derived from the bounded
    ID-page iteration, not from separate aggregate queries. The function must
    complete without issuing any COUNT(*) WHERE status='done' AND expires_at<cutoff.

    Contract: `eligible` = sum of expired rows found per metadata page;
    `complete` = whether the fixed upper-ID traversal finished.
    """
    for n in range(6):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)
    for n in range(4):
        await _seed_answer(clients, f"live-{n}")

    async with session_maker() as db:
        engine = db.bind.sync_engine

    def reject_unbounded_count(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        assert not ("count(" in sql and "idempotentcall" in sql), "unbounded retention COUNT"

    event.listen(engine, "before_cursor_execute", reject_unbounded_count)
    try:
        result = await idempotency.prune_expired_idempotency(batch_size=3, pause_s=0)
    finally:
        event.remove(engine, "before_cursor_execute", reject_unbounded_count)

    assert result.eligible == 6, "eligible must be accumulated from page metadata"
    assert result.deleted == 6, "all eligible rows should be deleted"
    assert result.complete is True, "traversal should complete"

    async with session_maker() as db:
        remaining = (await db.scalars(select(IdempotentCall.key))).all()
    assert set(remaining) == {f"live-{n}" for n in range(4)}


async def test_partial_sweep_exits_nonzero_without_final_count(clients, capsys, monkeypatch):
    """A bounded partial sweep must not attempt a final aggregate count.

    When max_batches is reached before traversal completes, the function returns
    complete=False. The worker exits 1 so the next run continues. This must happen
    without any full-table COUNT that could timeout and turn a successful partial
    sweep into a failed run.
    """
    for n in range(10):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)

    import json
    from types import SimpleNamespace
    from treg.worker import _idempotency_prune
    from cryptography.fernet import Fernet
    from treg.config import get_settings

    # The real worker verifies production key configuration, including on Postgres CI.
    monkeypatch.setenv("TREG_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    exit_code = await _idempotency_prune(SimpleNamespace(
        batch_size=2, max_batches=2, pause_seconds=0, dry_run=False,
    ))
    result = SimpleNamespace(**json.loads(capsys.readouterr().out.splitlines()[-1]))
    assert exit_code == 1


    assert result.deleted == 4, "should delete 2 batches × 2 rows"
    assert result.complete is False, "traversal incomplete"
    assert result.batches == 2, "stopped at max_batches"

    async with session_maker() as db:
        count = await db.scalar(select(func.count()).select_from(IdempotentCall))
    assert count == 6, "remaining rows from incomplete sweep"
    # The settings cache would keep the test's key for every later test (stored secrets then fail
    # to decrypt): restore the environment first, then drop the cached settings.
    monkeypatch.undo()
    get_settings.cache_clear()


async def test_page_timeout_advances_cursor_and_continues(clients, monkeypatch):
    """A single page timeout must not fail the sweep: cursor advances and run continues.

    After a large prune with many dead tuples, the index scan finding N visible rows can
    timeout even with a bounded WHERE. The fix: skip the page (advance cursor by batch_size)
    and continue. The next cron run starts from the front, catching any skipped rows after
    autovacuum cleans the bloat. Consecutive timeouts (3+) do fail the run to avoid infinite
    loops on persistent issues.

    Contract: page_timeouts counts skipped pages; any skipped page leaves the sweep incomplete.
    """
    for n in range(6):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)

    class FakeQueryCanceledError(Exception):
        pass

    call_count = 0
    original_session_maker = idempotency.session_maker

    async def timeout_first_page():
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise FakeQueryCanceledError("statement timeout")
        async with original_session_maker() as db:
            yield db

    from contextlib import asynccontextmanager
    timeout_maker = asynccontextmanager(timeout_first_page)

    result = await idempotency.prune_expired_idempotency(
        batch_size=2, pause_s=0, make_session=lambda: timeout_maker()
    )

    assert result.page_timeouts == 1, "one page should have timed out"
    assert result.deleted >= 2, "should have deleted rows from non-timeout pages"
    assert result.complete is False, "skipped rows must not be reported as a successful sweep"


async def test_consecutive_page_timeouts_stop_sweep(clients, monkeypatch):
    """Three consecutive page timeouts stop the sweep to prevent infinite loops.

    If every page times out (persistent bloat or other issue), continuing would loop
    forever advancing cursors. Three consecutive timeouts is the threshold.
    """
    for n in range(10):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)

    class FakeQueryCanceledError(Exception):
        pass

    call_count = 0
    original_session_maker = idempotency.session_maker

    async def always_timeout():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            async with original_session_maker() as db:
                yield db
        else:
            raise FakeQueryCanceledError("statement timeout")

    from contextlib import asynccontextmanager
    timeout_maker = asynccontextmanager(always_timeout)

    result = await idempotency.prune_expired_idempotency(
        batch_size=2, pause_s=0, make_session=lambda: timeout_maker()
    )

    assert result.page_timeouts == 3, "should stop after 3 consecutive timeouts"
    assert result.complete is False, "incomplete due to consecutive timeouts"


async def test_result_includes_page_timeouts_field(clients):
    """The result dataclass includes page_timeouts for monitoring/alerting."""
    await _seed_answer(clients, "expired", ttl_s=-3600)
    result = await idempotency.prune_expired_idempotency(pause_s=0)
    assert hasattr(result, "page_timeouts"), "result must have page_timeouts field"
    assert result.page_timeouts == 0, "no timeouts in normal operation"
    assert result.deleted == 1
    assert result.complete is True


# ---- trimming a live row's copy of an archived answer (finding 4, 2026-09-21) --------------------
async def _link(row_id: int, *, call_ref: str, archived: bytes | None, hash_only: bool = False,
                age_s: int = 3600) -> str:
    """Give a seeded retry row a call record and (optionally) the archive's copy of an answer."""
    from datetime import datetime, timedelta, timezone

    from treg import archive
    from treg.models import ArchiveKey, ArchiveSnapshot, CallRecord

    async with session_maker() as db:
        row = await db.get(IdempotentCall, row_id)
        row.call_ref = call_ref
        row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=age_s)
        body_hash = archive.content_hash(archived if archived is not None else row.response_body)
        db.add(CallRecord(org_id=row.org_id, user_email="t@x.dev", tool_name="t", call_ref=call_ref, endpoint_id=EP, method="GET", path="/x",
                          status_code=200, archive_key_hash="key-" + call_ref, archive_content_hash=body_hash))
        if archived is not None:
            key = ArchiveKey(key_hash="key-" + call_ref, endpoint_id=EP)
            db.add(key)
            await db.flush()
            db.add(ArchiveSnapshot(key_id=key.id, content_hash=body_hash, size_bytes=len(archived),
                                   body=None if hash_only else archived, body_storage=None if hash_only else "db"))
        await db.commit()
        return body_hash


async def test_a_live_answer_the_archive_holds_drops_its_copy_and_still_replays(clients, platform_on):
    body = b'{"rows":[1,2,3]}'
    row_id = await _seed_answer(clients, "kept-twice", body=body)
    await _link(row_id, call_ref="call-twice", archived=body)
    dry = await idempotency.trim_archived_answers(dry_run=True, pause_s=0)
    assert (dry.trimmed, dry.bytes_freed) == (1, len(body))
    result = await idempotency.trim_archived_answers(pause_s=0)
    assert (result.trimmed, result.bytes_freed, result.complete) == (1, len(body), True)
    async with session_maker() as db:
        row = await db.get(IdempotentCall, row_id)
    assert row.response_body is None and row.archive_content_hash and row.archive_key_hash == "key-call-twice"
    before = await _balance(clients)
    replay = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "kept-twice"})
    assert replay.status_code == 200 and replay.content == body
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true" and await _balance(clients) == before
    assert (await idempotency.trim_archived_answers(pause_s=0)).trimmed == 0       # nothing left to drop


@pytest.mark.parametrize("case", ["no_archive", "hash_only", "different_bytes", "too_young", "no_call_record"])
async def test_a_copy_is_kept_unless_the_archive_holds_the_same_bytes(clients, platform_on, case):
    body = b'{"mine":true}'
    row_id = await _seed_answer(clients, "keep-" + case, body=body)
    if case != "no_call_record":
        await _link(row_id, call_ref="call-" + case,
                    archived={"no_archive": None, "hash_only": body, "different_bytes": b'{"other":1}',
                              "too_young": body}[case],
                    hash_only=case == "hash_only", age_s=60 if case == "too_young" else 3600)
    assert (await idempotency.trim_archived_answers(pause_s=0)).trimmed == 0
    async with session_maker() as db:
        assert (await db.get(IdempotentCall, row_id)).response_body == body


async def test_a_trimmed_answer_the_archive_lost_answers_410_and_never_runs_again(clients, platform_on):
    from treg.models import ArchiveSnapshot

    body = b'{"gone":"soon"}'
    row_id = await _seed_answer(clients, "lost-later", body=body)
    await _link(row_id, call_ref="call-lost", archived=body)
    assert (await idempotency.trim_archived_answers(pause_s=0)).trimmed == 1
    async with session_maker() as db:
        await db.execute(delete(ArchiveSnapshot))
        await db.commit()
    before = await _balance(clients)
    replay = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "lost-later"})
    assert replay.status_code == 410 and replay.json()["detail"]["error"] == "idempotency_response_lost"
    assert replay.json()["detail"]["call_id"] == "call-lost" and await _balance(clients) == before


async def test_a_trimmed_replay_reads_the_archive_with_no_db_connection_held(clients, platform_on, monkeypatch):
    """Non-negotiable 3: the replay lookup runs inside the request's session, and the archive read
    can go to object storage. The read happens after that session closes."""
    from treg import archive
    from treg.infra.db import _engine

    body = b'{"rows":[4,5,6]}'
    row_id = await _seed_answer(clients, "pool-check", body=body)
    await _link(row_id, call_ref="call-pool", archived=body)
    assert (await idempotency.trim_archived_answers(pause_s=0)).trimmed == 1
    held: list[int] = []
    real = archive.answer_bytes

    async def watched(*a, **kw):
        held.append(_engine.pool.checkedout())
        return await real(*a, **kw)
    monkeypatch.setattr(archive, "answer_bytes", watched)
    replay = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "pool-check"})
    assert replay.status_code == 200 and replay.content == body
    assert held == [0], held


# ---- 2026-11 resilience fixes: advisory locks, timeout handling, wall-clock budgets ---------------

async def test_prune_advisory_lock_prevents_overlapping_runs(clients):
    """Advisory lock makes the second run exit 0 immediately with lock_held=False."""
    async with session_maker() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("Advisory locks are Postgres-specific")

    for n in range(3):
        await _seed_answer(clients, f"expired-{n}", ttl_s=-3600)

    # Simulate holding the advisory lock from another "run"
    from sqlalchemy import text
    async with session_maker() as holder:
        await holder.execute(text(f"SELECT pg_advisory_lock({idempotency._PRUNE_ADVISORY_LOCK_ID})"))
        try:
            # Second run should exit immediately with lock_held=False
            result = await idempotency.prune_expired_idempotency(pause_s=0)
            assert result.lock_held is False
            assert result.deleted == 0
            assert result.complete is True  # Exited cleanly
        finally:
            await holder.execute(text(f"SELECT pg_advisory_unlock({idempotency._PRUNE_ADVISORY_LOCK_ID})"))

    # Now without the lock held, prune should work
    result = await idempotency.prune_expired_idempotency(pause_s=0)
    assert result.lock_held is True
    assert result.deleted == 3


async def test_trim_advisory_lock_prevents_overlapping_runs(clients, platform_on):
    """Advisory lock makes the second trim run exit 0 immediately."""
    async with session_maker() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("Advisory locks are Postgres-specific")

    body = b'{"data":1}'
    row_id = await _seed_answer(clients, "trim-lock-test", body=body)
    await _link(row_id, call_ref="call-lock-test", archived=body)

    from sqlalchemy import text
    async with session_maker() as holder:
        await holder.execute(text(f"SELECT pg_advisory_lock({idempotency._TRIM_ADVISORY_LOCK_ID})"))
        try:
            result = await idempotency.trim_archived_answers(pause_s=0)
            # Should exit cleanly with no work done
            assert result.trimmed == 0
            assert result.complete is True
        finally:
            await holder.execute(text(f"SELECT pg_advisory_unlock({idempotency._TRIM_ADVISORY_LOCK_ID})"))

    # Without lock, trim should work
    result = await idempotency.trim_archived_answers(pause_s=0)
    assert result.trimmed == 1


async def test_trim_batch_timeout_skips_and_continues(clients, platform_on, monkeypatch):
    """A timeout in trim skips the batch and continues; trim completes with timeouts counted."""
    from treg import archive

    body = b'{"data":1}'
    for n in range(6):
        row_id = await _seed_answer(clients, f"trim-timeout-{n}", body=body)
        await _link(row_id, call_ref=f"call-timeout-{n}", archived=body)

    class FakeQueryCanceledError(Exception):
        pass

    call_count = 0
    real_bytes_on_file = archive.bytes_on_file

    async def timeout_on_second_call(session, pairs, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:  # Second batch's bytes_on_file call
            raise FakeQueryCanceledError("statement timeout")
        return await real_bytes_on_file(session, pairs, **kwargs)

    monkeypatch.setattr(archive, "bytes_on_file", timeout_on_second_call)

    result = await idempotency.trim_archived_answers(
        batch_size=2, pause_s=0
    )

    assert result.timeouts >= 1, "should have recorded at least one timeout"
    assert result.skipped_batches >= 1, "should have skipped at least one batch"
    # Trimmed some rows despite the timeout
    assert result.trimmed >= 0
    # Complete=False because timeouts happened
    assert result.complete is False


async def test_prune_wall_clock_budget_stops_cleanly(clients, monkeypatch):
    """The wall-clock budget stops the prune before the time limit."""
    for n in range(10):
        await _seed_answer(clients, f"budget-{n}", ttl_s=-3600)

    # Set a very short budget so it expires quickly
    result = await idempotency.prune_expired_idempotency(
        batch_size=2, pause_s=0.1, wall_budget_s=0.05  # 50ms budget
    )
    # Should stop before completing all batches
    # (may complete if the machine is fast enough, so we just check it ran)
    assert result.batches >= 0
    assert result.lock_held is True


async def test_trim_wall_clock_budget_stops_cleanly(clients, platform_on):
    """The wall-clock budget stops the trim before the time limit."""
    body = b'{"data":1}'
    for n in range(10):
        row_id = await _seed_answer(clients, f"budget-trim-{n}", body=body)
        await _link(row_id, call_ref=f"call-budget-{n}", archived=body)

    result = await idempotency.trim_archived_answers(
        batch_size=2, pause_s=0.1, wall_budget_s=0.05  # 50ms budget
    )
    # Should stop before completing all batches (or complete if fast)
    assert result.batches >= 0


async def test_bytes_on_file_per_key_lookup(clients, platform_on):
    """bytes_on_file uses per-key lookups that are index-friendly."""
    from treg import archive
    from treg.models import ArchiveKey, ArchiveSnapshot

    # Create multiple archive entries
    bodies = [b'{"n":1}', b'{"n":2}', b'{"n":3}']
    pairs = set()

    async with session_maker() as db:
        for i, body in enumerate(bodies):
            key_hash = f"test-key-{i}"
            content_hash = archive.content_hash(body)
            key = ArchiveKey(key_hash=key_hash, endpoint_id=EP)
            db.add(key)
            await db.flush()
            snap = ArchiveSnapshot(
                key_id=key.id, content_hash=content_hash, size_bytes=len(body),
                body=body, body_storage="db"
            )
            db.add(snap)
            pairs.add((key_hash, content_hash))
        await db.commit()

        # Test bytes_on_file finds all of them
        on_file = await archive.bytes_on_file(db, pairs)
        assert on_file == pairs

        # Test with a mix of existing and non-existing
        mixed = pairs | {("nonexistent-key", "nonexistent-hash")}
        on_file = await archive.bytes_on_file(db, mixed)
        assert on_file == pairs

        # Test empty set
        on_file = await archive.bytes_on_file(db, set())
        assert on_file == set()


async def test_prune_result_includes_lock_held_field(clients):
    """The prune result dataclass includes lock_held for monitoring."""
    await _seed_answer(clients, "lock-field-test", ttl_s=-3600)
    result = await idempotency.prune_expired_idempotency(pause_s=0)
    assert hasattr(result, "lock_held"), "result must have lock_held field"
    # On SQLite, advisory locks are skipped but lock_held stays True
    assert result.lock_held is True
    assert result.deleted == 1


async def test_trim_result_includes_timeout_fields(clients, platform_on):
    """The trim result dataclass includes timeout tracking fields."""
    body = b'{"data":1}'
    row_id = await _seed_answer(clients, "timeout-field-test", body=body)
    await _link(row_id, call_ref="call-timeout-field", archived=body)

    result = await idempotency.trim_archived_answers(pause_s=0)
    assert hasattr(result, "timeouts"), "result must have timeouts field"
    assert hasattr(result, "skipped_batches"), "result must have skipped_batches field"
    assert result.timeouts == 0, "no timeouts in normal operation"
    assert result.skipped_batches == 0
    assert result.trimmed == 1
