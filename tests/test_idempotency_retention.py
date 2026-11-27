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


async def test_a_trimmed_replay_reads_the_archive_with_no_db_connection_held(
        clients, platform_on, monkeypatch, connections_held_here):
    """Non-negotiable 3: the replay lookup runs inside the request's session, and the archive read
    can go to object storage. The read happens after that session closes."""
    from treg import archive

    body = b'{"rows":[4,5,6]}'
    row_id = await _seed_answer(clients, "pool-check", body=body)
    await _link(row_id, call_ref="call-pool", archived=body)
    assert (await idempotency.trim_archived_answers(pause_s=0)).trimmed == 1
    held: list[int] = []
    real = archive.answer_bytes

    async def watched(*a, **kw):
        held.append(connections_held_here())
        return await real(*a, **kw)
    monkeypatch.setattr(archive, "answer_bytes", watched)
    replay = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Idempotency-Key": "pool-check"})
    assert replay.status_code == 200 and replay.content == body
    assert held == [0], held


async def test_the_trim_resumes_after_the_rows_it_decided_and_stops_at_young_ones(clients, platform_on):
    old_a = await _seed_answer(clients, "resume-a", body=b'{"a":1}')
    await _link(old_a, call_ref="call-resume-a", archived=b'{"a":1}')
    young = await _seed_answer(clients, "resume-young", body=b'{"y":1}')
    await _link(young, call_ref="call-resume-young", archived=b'{"y":1}', age_s=60)
    old_c = await _seed_answer(clients, "resume-c", body=b'{"c":1}')
    await _link(old_c, call_ref="call-resume-c", archived=b'{"c":1}')
    first = await idempotency.trim_archived_answers(pause_s=0)
    assert (first.trimmed, first.resume_after) == (2, old_a)   # c is trimmed, but the young row pins the place
    again = await idempotency.trim_archived_answers(pause_s=0, after=first.resume_after)
    assert (again.start_after, again.examined, again.trimmed) == (old_a, 0, 0)
    assert (await idempotency.trim_archived_answers(pause_s=0, after=young)).stopped == "end"


async def test_a_trim_with_only_young_rows_left_stops_without_reading_them(clients, platform_on):
    await _seed_answer(clients, "only-young", body=b'{"y":2}', ttl_s=idempotency.IDEMPOTENCY_WINDOW_S)
    result = await idempotency.trim_archived_answers(pause_s=0)
    assert (result.stopped, result.complete, result.examined, result.resume_after) == ("young", True, 0, 0)


async def test_a_long_call_finished_after_a_lease_renewal_does_not_end_the_walk(clients, platform_on):
    """Its `created_at` is the last renewal, but it was claimed long ago (`expires_at` less the window)."""
    from datetime import datetime, timedelta, timezone

    finished = await _seed_answer(clients, "long-call", body=b'{"l":1}')   # claimed 23 hours ago
    await _link(finished, call_ref="call-long-call", archived=b'{"l":1}', age_s=30)
    behind = await _linked_answers(clients, "behind-long-call", 1)
    result = await idempotency.trim_archived_answers(pause_s=0, batch_size=1)
    assert (result.stopped, result.trimmed, result.resume_after) == ("end", 1, 0)
    async with session_maker() as db:
        assert (await db.get(IdempotentCall, behind[0])).response_body is None
        row = await db.get(IdempotentCall, finished)
        row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        await db.commit()
    assert (await idempotency.trim_archived_answers(pause_s=0)).resume_after == behind[0]


@pytest.mark.parametrize("dead", ["expired", "stale"])
async def test_a_dead_pending_claim_does_not_hold_the_trim_place(clients, platform_on, dead):
    from datetime import datetime, timedelta, timezone

    claim = await _seed_answer(clients, "dead-claim", status="pending",
                               ttl_s=-3600 if dead == "expired" else 3600)
    async with session_maker() as db:
        row = await db.get(IdempotentCall, claim)
        row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=idempotency.IDEMPOTENCY_STALE_PENDING_S + 60)   # no renewal since: its owner stopped
        await db.commit()
    behind = await _linked_answers(clients, "behind-dead-claim", 1)
    result = await idempotency.trim_archived_answers(pause_s=0, batch_size=1)
    assert (result.trimmed, result.resume_after) == (1, behind[0])
    async with session_maker() as db:
        assert (await db.get(IdempotentCall, claim)).status == "pending"   # passed over, not removed


async def _linked_answers(clients, prefix: str, count: int) -> list[int]:
    ids = []
    for n in range(count):
        body = b'{"%s":%d}' % (prefix.encode(), n)
        row_id = await _seed_answer(clients, f"{prefix}-{n}", body=body)
        await _link(row_id, call_ref=f"call-{prefix}-{n}", archived=body)
        ids.append(row_id)
    return ids


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _advance_on_trim_update(clock: _Clock, seconds: float):
    """Move `clock` on after every trim UPDATE: a page whose updates are slow."""
    def after(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE IDEMPOTENTCALL"):
            clock.now += seconds
    engine = session_maker.kw["bind"].sync_engine
    event.listen(engine, "after_cursor_execute", after)
    return lambda: event.remove(engine, "after_cursor_execute", after)


async def test_the_trim_stops_at_its_time_budget_between_pages_and_keeps_its_place(clients, platform_on):
    ids = await _linked_answers(clients, "budget", 3)
    clock = _Clock()
    stop = _advance_on_trim_update(clock, 6)
    try:
        result = await idempotency.trim_archived_answers(pause_s=0, batch_size=1, max_seconds=10, clock=clock)
    finally:
        stop()
    assert (result.stopped, result.complete, result.batches, result.trimmed) == ("budget", False, 2, 2)
    assert result.resume_after == ids[1]
    rest = await idempotency.trim_archived_answers(pause_s=0, after=result.resume_after)
    assert (rest.trimmed, rest.resume_after) == (1, ids[2])


async def test_the_time_budget_stops_inside_a_slow_page_and_resumes_at_the_first_row_left(clients, platform_on):
    ids = await _linked_answers(clients, "slow-page", 3)
    clock = _Clock()
    stop = _advance_on_trim_update(clock, 7)
    try:
        result = await idempotency.trim_archived_answers(pause_s=0, max_seconds=10, clock=clock)
    finally:
        stop()
    assert (result.stopped, result.trimmed, result.resume_after, result.batches) == ("budget", 2, ids[1], 1)
    async with session_maker() as db:
        assert (await db.get(IdempotentCall, ids[2])).response_body is not None   # committed up to the stop
    rest = await idempotency.trim_archived_answers(pause_s=0, after=result.resume_after)
    assert (rest.trimmed, rest.resume_after) == (1, ids[2])


async def test_a_renewed_pending_claim_neither_ends_the_trim_nor_is_passed_over(clients, platform_on):
    pending = await _seed_answer(clients, "renewed-claim", status="pending")   # its lease just renewed created_at
    old = await _linked_answers(clients, "after-claim", 1)
    for _ in range(2):
        result = await idempotency.trim_archived_answers(pause_s=0, batch_size=1)
        assert result.resume_after < pending
    async with session_maker() as db:
        assert (await db.get(IdempotentCall, old[0])).response_body is None
    assert result.trimmed == 0 and result.stopped == "end"   # the second pass had nothing left to drop


async def test_a_row_whose_archive_key_was_held_is_tried_again_from_the_saved_place(clients, platform_on, monkeypatch):
    from treg import archive

    ids = await _linked_answers(clients, "held-key", 2)
    real = archive.lock_keys_for_share

    async def first_key_held(session, key_hashes):
        return (await real(session, key_hashes)) - {"key-call-held-key-0"}
    monkeypatch.setattr(archive, "lock_keys_for_share", first_key_held)
    first = await idempotency.trim_archived_answers(pause_s=0)
    assert (first.trimmed, first.resume_after) == (1, 0)
    monkeypatch.setattr(archive, "lock_keys_for_share", real)
    again = await idempotency.trim_archived_answers(pause_s=0, after=first.resume_after)
    assert (again.trimmed, again.resume_after) == (1, ids[1])


async def test_each_committed_page_reports_its_place_before_a_later_page_fails(clients, platform_on):
    ids = await _linked_answers(clients, "progress", 2)
    opened = 0

    def failing_on_third_session():
        nonlocal opened
        opened += 1
        if opened == 3:  # the cutoff read, page one, then page two
            raise RuntimeError("page two is unreadable")
        return session_maker()
    saved: list[int] = []

    async def save(after: int) -> None:
        saved.append(after)
    with pytest.raises(RuntimeError):
        await idempotency.trim_archived_answers(pause_s=0, batch_size=1, on_progress=save,
                                                make_session=failing_on_third_session)
    assert saved == [ids[0]]
    async with session_maker() as db:
        assert (await db.get(IdempotentCall, ids[0])).response_body is None


async def test_a_cursor_past_the_table_starts_the_trim_from_the_front(clients, platform_on):
    row_id = await _seed_answer(clients, "restored", body=b'{"r":1}')
    await _link(row_id, call_ref="call-restored", archived=b'{"r":1}')
    result = await idempotency.trim_archived_answers(pause_s=0, after=row_id + 1000)
    assert (result.start_after, result.trimmed) == (0, 1)


async def _prune_command(monkeypatch, **args):
    from types import SimpleNamespace

    from cryptography.fernet import Fernet

    from treg.config import get_settings
    from treg.worker import _idempotency_prune

    # The real worker verifies production key configuration, including on Postgres CI.
    monkeypatch.setenv("TREG_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    try:
        return await _idempotency_prune(SimpleNamespace(
            **{"batch_size": 200, "max_batches": 10000, "pause_seconds": 0, "dry_run": False, **args}))
    finally:
        # Restore the environment first, then drop the cached settings (see the sweep test above).
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_the_prune_command_saves_the_trim_place_for_the_next_run(clients, platform_on, capsys, monkeypatch):
    import json

    from treg import ratestore
    from treg.worker import TRIM_CURSOR_KEY, TRIM_CURSOR_NS

    row_id = await _seed_answer(clients, "saved-place", body=b'{"s":1}')
    await _link(row_id, call_ref="call-saved-place", archived=b'{"s":1}')
    assert await _prune_command(monkeypatch) == 0
    first = json.loads(capsys.readouterr().out.splitlines()[-1])["trim"]
    assert (first["trimmed"], first["resume_after"]) == (1, row_id)
    async with session_maker() as db:
        assert await ratestore.kv_get(db, TRIM_CURSOR_NS, TRIM_CURSOR_KEY) == {"after": row_id}
    assert await _prune_command(monkeypatch) == 0
    second = json.loads(capsys.readouterr().out.splitlines()[-1])["trim"]
    assert (second["start_after"], second["examined"]) == (row_id, 0)


async def test_a_prune_run_that_finds_another_running_does_nothing(clients, capsys, monkeypatch):
    import json
    from contextlib import asynccontextmanager

    from treg.infra import db as infra_db

    @asynccontextmanager
    async def taken(key):
        yield False
    monkeypatch.setattr(infra_db, "only_one_run", taken)
    await _seed_answer(clients, "left-alone", ttl_s=-3600)
    assert await _prune_command(monkeypatch) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"stopped": "busy"}
    async with session_maker() as db:
        assert await db.scalar(select(func.count()).select_from(IdempotentCall)) == 1


async def test_a_dry_run_never_moves_the_saved_trim_place(clients, platform_on, capsys, monkeypatch):
    from treg import ratestore
    from treg.worker import TRIM_CURSOR_KEY, TRIM_CURSOR_NS

    await _linked_answers(clients, "dry-place", 2)
    assert await _prune_command(monkeypatch, dry_run=True) == 0
    async with session_maker() as db:
        assert await ratestore.kv_get(db, TRIM_CURSOR_NS, TRIM_CURSOR_KEY) is None


@pytest.mark.parametrize("dry_run", [False, True])
async def test_a_place_past_the_table_is_reset_even_when_the_pass_stops_at_once(
        clients, platform_on, capsys, monkeypatch, dry_run):
    from treg import ratestore
    from treg.worker import TRIM_CURSOR_KEY, TRIM_CURSOR_NS

    young = await _seed_answer(clients, "young-after-restore", ttl_s=idempotency.IDEMPOTENCY_WINDOW_S)
    async with session_maker() as db:
        await ratestore.kv_put(db, TRIM_CURSOR_NS, TRIM_CURSOR_KEY, {"after": young + 100}, ttl_s=3600)
        await db.commit()
    assert await _prune_command(monkeypatch, dry_run=dry_run) == 0
    async with session_maker() as db:
        saved = await ratestore.kv_get(db, TRIM_CURSOR_NS, TRIM_CURSOR_KEY)
    assert saved == ({"after": young + 100} if dry_run else {"after": 0})


@pytest.mark.parametrize("bad", [{"trim_after": -1}, {"trim_max_seconds": 0.0},
                                 {"trim_max_seconds": float("nan")}, {"trim_max_seconds": float("inf")}])
async def test_bad_trim_options_are_refused_before_the_prune_deletes_anything(clients, monkeypatch, bad):
    await _seed_answer(clients, "expired-but-kept", ttl_s=-3600)
    with pytest.raises(ValueError):
        await _prune_command(monkeypatch, **bad)
    async with session_maker() as db:
        assert await db.scalar(select(func.count()).select_from(IdempotentCall)) == 1


@pytest.mark.parametrize("argv", [["--trim-after", "-1"], ["--trim-max-seconds", "0"],
                                  ["--trim-max-seconds", "nan"], ["--trim-max-seconds", "-5"]])
def test_the_command_line_refuses_bad_trim_options(argv):
    from treg import worker

    with pytest.raises(SystemExit) as exit_:
        worker.main(["idempotency", "prune", *argv])
    assert exit_.value.code == 2


def _postgres_only():
    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("advisory locks and row locks need PostgreSQL")


async def test_the_run_lock_takes_no_slot_of_a_one_connection_pool(clients, monkeypatch):
    _postgres_only()
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from treg.infra import db as infra_db

    await _seed_answer(clients, "one-slot", ttl_s=-3600)
    one = create_async_engine(session_maker.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=2)
    try:
        one_slot = async_sessionmaker(one, expire_on_commit=False)
        monkeypatch.setattr(infra_db, "session_maker", one_slot)   # the API pool, configured to one slot
        async with infra_db.only_one_run(0x74657374) as alone:
            assert alone
            pruned = await idempotency.prune_expired_idempotency(pause_s=0, make_session=one_slot)
            trimmed = await idempotency.trim_archived_answers(pause_s=0, make_session=one_slot)
    finally:
        await one.dispose()
    assert (pruned.deleted, pruned.complete, trimmed.complete) == (1, True, True)


async def test_a_second_prune_run_on_postgres_answers_busy_and_deletes_nothing(clients, capsys, monkeypatch):
    _postgres_only()
    import json

    from sqlalchemy import text

    from treg.worker import IDEMPOTENCY_RUN_LOCK_KEY

    await _seed_answer(clients, "busy-on-pg", ttl_s=-3600)
    async with session_maker.kw["bind"].connect() as other:
        assert (await other.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                    {"k": IDEMPOTENCY_RUN_LOCK_KEY})).scalar_one()
        try:
            assert await _prune_command(monkeypatch) == 0
        finally:
            await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": IDEMPOTENCY_RUN_LOCK_KEY})
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"stopped": "busy"}
    async with session_maker() as db:
        assert await db.scalar(select(func.count()).select_from(IdempotentCall)) == 1


async def test_the_run_lock_outlives_an_idle_session_timeout(clients):
    """A database-wide `idle_session_timeout` must not end the lock session while the run goes on."""
    _postgres_only()
    import asyncio

    from sqlalchemy import text

    from treg.infra.db import only_one_run

    async with session_maker() as db:
        name = await db.scalar(text("SELECT current_database()"))
        await db.execute(text(f'ALTER DATABASE "{name}" SET idle_session_timeout = \'300ms\''))
        await db.commit()
    try:
        async with only_one_run(0x69646C65) as first:
            assert first
            await asyncio.sleep(0.8)
            async with only_one_run(0x69646C65) as second:
                assert not second
    finally:
        async with session_maker() as db:
            await db.execute(text(f'ALTER DATABASE "{name}" RESET idle_session_timeout'))
            await db.commit()


async def test_an_archive_key_locked_on_postgres_is_trimmed_by_the_next_pass(clients, platform_on):
    _postgres_only()
    from treg.models import ArchiveKey

    ids = await _linked_answers(clients, "pg-held", 1)
    async with session_maker() as holder:
        await holder.execute(select(ArchiveKey).where(ArchiveKey.key_hash == "key-call-pg-held-0")
                             .with_for_update())
        first = await idempotency.trim_archived_answers(pause_s=0)
        await holder.rollback()
    assert (first.trimmed, first.resume_after) == (0, 0)
    again = await idempotency.trim_archived_answers(pause_s=0, after=first.resume_after)
    assert (again.trimmed, again.resume_after) == (1, ids[0])
