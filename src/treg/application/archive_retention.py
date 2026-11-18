"""Delete archive questions no one asked for (docs/context/architecture/archive.md, "Row retention").

Run by `treg-worker archive retain`, never on a request. The unit is a whole question: a key, its
snapshots and every team's `ArchiveKeyOrg` mark on it go in one transaction, or nothing goes. A
snapshot is only ever referenced from inside its own key (`body_of` carriers and the decisive
`result_snapshot_id` are chosen by the recorder under the key's row lock), so deleting whole keys
never removes a snapshot something kept still points at. A key is a candidate only when, at
`cutoff` (now minus `archive_retention_days`), it is older than the cutoff, nobody asked for it or
paid for it since, it was not fetched since, its endpoint's policy is not `archive` (recorded or
current), it is not async terminal evidence (`treg://` request URL, read back by Activity), and no
unexpired idempotent retry row replays from it.

Rows before objects. A batch reads its candidates, then in a fresh, capped transaction locks the
keys (skipping any a recorder holds), repeats the candidate predicate, deletes the rows and, in
the same transaction, records in `archiveobjectpending` each object-stored body hash that lost its
last snapshot. Nothing in object storage is touched before that commit, so a batch that rolls back
deletes nothing. A later run deletes a pending object only once it was recorded more than
`archive_retention_object_grace_s` ago (longer than a recorder's check-that-the-object-exists to
commit window and its upload budget) and re-checks, right before each delete, that no snapshot
references the hash; a referenced hash leaves the table and its object stays. A failed delete
keeps its row for the next run; with no object store configured, the due rows wait.

The walk is a keyset over `archivekey.id` in fixed id ranges, so every statement reads a bounded
slice of the primary key however few rows in it qualify; `archiveretentioncursor` keeps the
position between runs, and a pass that reaches the end starts again from 0.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased

from .. import archive, archive_bodies
from ..config import get_settings
from ..domain.governance.teams import erase_archive_keys, erase_archive_marks
from ..infra.db import session_maker
from ..models import (
    ArchiveKey, ArchiveKeyOrg, ArchiveObjectPending, ArchiveRetentionCursor, ArchiveSnapshot,
    IdempotentCall,
)
from ..timeutil import utcnow_naive

_log = logging.getLogger("treg.archive.retention")

CURSOR_ID = "archivekey"
TERMINAL_URL_PREFIX = "treg://"
_STATEMENT_TIMEOUT_MS = 30_000
_LOCK_TIMEOUT_MS = 1_000
_MAX_CONSECUTIVE_FAILURES = 3
# asyncpg binds at most 32,767 values per statement, and `objects_only_under` binds every key id
# of a batch twice.
MAX_BATCH = 10_000
# One real run at a time (pg_try_advisory_lock); any constant no other advisory lock uses.
RUN_LOCK_KEY = 0x61726574
# Pending object deletes: rows read per page, and object-store calls in flight at once.
OBJECT_PAGE = 500
OBJECT_CONCURRENCY = 4
# Hashes per statement when recording pending objects.
_HASH_CHUNK = 5_000
# The grace period must outlast a recorder's whole store (`archive._STORE_TIMEOUT_S`): between
# confirming an object exists and committing its snapshot. Plus a margin.
MIN_OBJECT_GRACE_S = archive._STORE_TIMEOUT_S + 120
# A deleted object's row stays this long (or the grace period, if longer) as a tombstone: a
# recorder that confirmed the object just before the delete still sees the `deleting` mark.
TOMBSTONE_KEEP_S = 900


class ObjectStoreMissing(RuntimeError):
    """Pending objects are due for deletion but no object store is configured."""


class ObjectDeletesFailed(RuntimeError):
    """The pending-object step hit a database error; the run stops with `stopped: "error"`."""


@dataclass
class RetentionResult:
    enabled: bool
    dry_run: bool
    cutoff: str = ""
    start_after: int = 0
    next_after: int = 0
    upper_id: int = 0
    batches: int = 0
    keys: int = 0
    snapshots: int = 0
    marks: int = 0
    objects: int = 0          # dry: bodies only these keys hold; real: objects deleted this run
    objects_pending: int = 0  # hashes recorded for a later run to delete
    objects_kept: int = 0     # pending hashes referenced again, dropped from the table
    objects_failed: int = 0   # deletes that failed, moved to the back of the queue
    skipped_batches: int = 0
    pass_complete: bool = False
    stopped: str = ""  # pass_complete | time_budget | max_keys | failures | busy | disabled | error
    error: str | None = None


async def _bounded(db) -> None:
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
        await db.execute(text(f"SET LOCAL lock_timeout = {_LOCK_TIMEOUT_MS}"))


def archive_policy_endpoints() -> set[str]:
    """Endpoints the catalog judges `archive` today: their history is kept whatever a key last
    recorded."""
    from ..domain.catalog import store as catalog_store

    return {eid for eid, entry in catalog_store.load().by_id.items()
            if archive.policy(entry) == archive.CACHE_ARCHIVE}


def dormant(cutoff: datetime, now: datetime) -> tuple:
    """The candidate predicate on `ArchiveKey`, repeated by the delete under the row lock."""
    live_retry = exists().where(IdempotentCall.archive_key_hash == ArchiveKey.key_hash,
                                IdempotentCall.archive_content_hash.is_not(None),
                                IdempotentCall.expires_at > now)
    paid_since = exists().where(ArchiveKeyOrg.key_hash == ArchiveKey.key_hash,
                                ArchiveKeyOrg.last_call_at >= cutoff)
    return (ArchiveKey.policy != archive.CACHE_ARCHIVE,
            ArchiveKey.created_at < cutoff,
            ArchiveKey.fetched_at < cutoff,
            or_(ArchiveKey.last_requested_at.is_(None), ArchiveKey.last_requested_at < cutoff),
            or_(ArchiveKey.req_url.is_(None), ~ArchiveKey.req_url.startswith(TERMINAL_URL_PREFIX)),
            ~live_retry,
            ~paid_since)


async def candidates(db, *, after: int, upto: int, cutoff: datetime, now: datetime,
                     keep_endpoints: set[str]) -> list[tuple[int, str]]:
    """`(id, key_hash)` of the keys in the id range `(after, upto]` that may go, ascending."""
    rows = (await db.execute(
        select(ArchiveKey.id, ArchiveKey.key_hash, ArchiveKey.endpoint_id)
        .where(ArchiveKey.id > after, ArchiveKey.id <= upto, *dormant(cutoff, now))
        .order_by(ArchiveKey.id))).all()
    return [(int(i), str(h)) for i, h, endpoint_id in rows if endpoint_id not in keep_endpoints]


async def objects_only_under(db, key_ids: list[int]) -> set[str]:
    """Content hashes of object-stored bodies that only snapshots under `key_ids` point at."""
    if not key_ids:
        return set()
    other = aliased(ArchiveSnapshot)
    shared = exists().where(other.content_hash == ArchiveSnapshot.content_hash,
                            other.key_id.not_in(key_ids))
    rows = await db.execute(
        select(ArchiveSnapshot.content_hash).distinct()
        .where(ArchiveSnapshot.key_id.in_(key_ids),
               ArchiveSnapshot.body_storage.in_(("r2", "both")), ~shared))
    return set(rows.scalars().all())


async def _snapshot_count(db, key_ids: list[int]) -> int:
    return int((await db.execute(select(func.count()).select_from(ArchiveSnapshot)
                                 .where(ArchiveSnapshot.key_id.in_(key_ids)))).scalar_one())


async def _mark_count(db, key_hashes: list[str]) -> int:
    return int((await db.execute(select(func.count()).select_from(ArchiveKeyOrg)
                                 .where(ArchiveKeyOrg.key_hash.in_(key_hashes)))).scalar_one())


_OBJECT_STORED = ("r2", "both")


async def _record_unreferenced(db, hashes: set[str], now: datetime) -> int:
    """In the caller's transaction: record the hashes among `hashes` no snapshot references any
    more as pending object deletes (`recorded_at` now, also for one already pending) -> how many."""
    if not hashes:
        return 0
    ordered = sorted(hashes)
    still: set[str] = set()
    for start in range(0, len(ordered), _HASH_CHUNK):
        still |= set((await db.execute(
            select(ArchiveSnapshot.content_hash).distinct()
            .where(ArchiveSnapshot.content_hash.in_(ordered[start:start + _HASH_CHUNK]),
                   ArchiveSnapshot.body_storage.in_(_OBJECT_STORED)))).scalars().all())
    gone = [h for h in ordered if h not in still]
    if not gone:
        return 0
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    for start in range(0, len(gone), _HASH_CHUNK):
        statement = insert(ArchiveObjectPending).values(
            [{"content_hash": h, "recorded_at": now} for h in gone[start:start + _HASH_CHUNK]])
        # Recorded again: a new grace period. `deleting` stays as it was: once a delete was
        # attempted the object may be gone, and a recorder that sees the mark keeps its own copy.
        await db.execute(statement.on_conflict_do_update(
            index_elements=[ArchiveObjectPending.content_hash],
            set_={"recorded_at": statement.excluded.recorded_at, "deleted_at": None, "attempts": 0}))
    return len(gone)


async def _delete_one_object(result: RetentionResult, content_hash: str, *, cutoff: datetime,
                             store, session_factory) -> None:
    """Delete one due object, if no snapshot references it, in three short steps with no session
    open while object storage is called:

    1. mark the row `deleting` and commit. A recorder writing a snapshot of this hash reads the
       row `FOR SHARE` in its own transaction (`archive._store_locked`): one that already read it
       makes this wait for its commit; one that reads it after sees the mark and keeps a copy of
       the bytes in its row;
    2. re-check that no committed snapshot references the hash (one does: the row goes, the
       object stays);
    3. delete the object, then keep the row as a tombstone (`deleted_at`) for one more grace
       period, so a recorder that confirmed the object just before the delete still sees the mark.
    A failure moves the row to the back of the queue (`recorded_at` now, `attempts` + 1)."""
    try:
        async with session_factory() as db:
            await _bounded(db)
            marked = (await db.execute(
                update(ArchiveObjectPending)
                .where(ArchiveObjectPending.content_hash == content_hash,
                       ArchiveObjectPending.deleted_at.is_(None),
                       ArchiveObjectPending.recorded_at <= cutoff)
                .values(deleting=True)
                .returning(ArchiveObjectPending.content_hash))).first()
            await db.commit()
        if marked is None:
            return                       # re-recorded or taken meanwhile
        async with session_factory() as db:
            await _bounded(db)
            referenced = (await db.execute(select(exists().where(
                ArchiveSnapshot.content_hash == content_hash,
                ArchiveSnapshot.body_storage.in_(_OBJECT_STORED))))).scalar()
            if referenced:
                await db.execute(delete(ArchiveObjectPending)
                                 .where(ArchiveObjectPending.content_hash == content_hash))
                await db.commit()
                result.objects_kept += 1
                return
        await store.delete(content_hash)
        archive_bodies.forget(content_hash)
        async with session_factory() as db:
            await _bounded(db)
            await db.execute(update(ArchiveObjectPending)
                             .where(ArchiveObjectPending.content_hash == content_hash)
                             .values(deleted_at=utcnow_naive()))
            await db.commit()
        result.objects += 1
    except Exception as exc:  # noqa: BLE001 - one object failing must not stop the rest
        _log.warning("archive retention: object %s not deleted (%s)", content_hash[:12],
                     type(exc).__name__)
        result.objects_failed += 1
        try:
            async with session_factory() as db:
                await _bounded(db)
                await db.execute(update(ArchiveObjectPending)
                                 .where(ArchiveObjectPending.content_hash == content_hash,
                                        ArchiveObjectPending.deleted_at.is_(None))
                                 .values(recorded_at=utcnow_naive(),
                                         attempts=ArchiveObjectPending.attempts + 1))
                await db.commit()
        except DBAPIError as exc:   # the row is held elsewhere: it stays due, and the run goes on
            _log.warning("archive retention: pending object %s not moved back (%s)",
                         content_hash[:12], type(exc).__name__)


async def _gated(gate: asyncio.Semaphore, spent, work) -> None:
    async with gate:
        if spent():
            work.close()
            return
        await work


async def _delete_due_objects(result: RetentionResult, *, grace_s: float, session_factory,
                              spent, page: int = OBJECT_PAGE) -> None:
    """Delete the pending objects recorded more than `grace_s` ago, page after page until none is
    due or the time budget is spent, `OBJECT_CONCURRENCY` at a time; then forget the tombstones
    older than `TOMBSTONE_KEEP_S` (or the grace period, if longer)."""
    store = archive_bodies._store
    gate = asyncio.Semaphore(OBJECT_CONCURRENCY)
    # Fixed for the run: every row a page handles leaves the due set (deleted, tombstoned, or moved
    # past the cutoff by a failure or a new recording), so the pages end.
    cutoff = utcnow_naive() - timedelta(seconds=grace_s)
    # A row held elsewhere (its mark and its move-back both timed out) stays due: it is skipped
    # for the rest of the run, so it neither repeats nor hides the rows after it.
    attempted: set[str] = set()
    stuck: set[str] = set()
    while not spent():
        query = (select(ArchiveObjectPending.content_hash)
                 .where(ArchiveObjectPending.deleted_at.is_(None),
                        ArchiveObjectPending.recorded_at <= cutoff)
                 .order_by(ArchiveObjectPending.recorded_at).limit(page))
        if stuck:
            query = query.where(ArchiveObjectPending.content_hash.not_in(stuck))
        async with session_factory() as db:
            await _bounded(db)
            page_hashes = (await db.execute(query)).scalars().all()
        stuck.update(h for h in page_hashes if h in attempted)
        due = [h for h in page_hashes if h not in attempted]
        if not due:
            if page_hashes:
                continue
            break
        attempted.update(due)
        if store is None:
            raise ObjectStoreMissing("archive objects are due for deletion but no object store is configured")
        await asyncio.gather(*(
            _gated(gate, spent, _delete_one_object(result, h, cutoff=cutoff, store=store,
                                                   session_factory=session_factory))
            for h in due))
    async with session_factory() as db:
        await _bounded(db)
        await db.execute(delete(ArchiveObjectPending).where(
            ArchiveObjectPending.deleted_at
            < utcnow_naive() - timedelta(seconds=max(grace_s, TOMBSTONE_KEEP_S))))
        await db.commit()


async def _read_cursor(session_factory) -> int:
    async with session_factory() as db:
        row = await db.get(ArchiveRetentionCursor, CURSOR_ID)
        return int(row.after_id) if row is not None else 0


async def _save_cursor(db, after_id: int, *, pass_done: bool = False) -> None:
    row = await db.get(ArchiveRetentionCursor, CURSOR_ID)
    if row is None:
        row = ArchiveRetentionCursor(id=CURSOR_ID, after_id=0, passes=0)
    row.after_id = 0 if pass_done else after_id
    row.passes += 1 if pass_done else 0
    row.updated_at = utcnow_naive()
    db.add(row)


async def run(*, dry_run: bool = False, days: int | None = None, batch: int | None = None,
              pause_s: float | None = None, max_seconds: float | None = None,
              max_keys: int | None = None, after: int | None = None,
              object_grace_s: float | None = None,
              session_factory=session_maker, clock=time.monotonic) -> dict:
    """One bounded run -> the `RetentionResult` as a dict.

    `dry_run` reads the same batches and deletes nothing, setting or not; it starts at the saved
    cursor (or `after`) and never moves it. A real run does nothing unless
    `archive_retention_enabled`, and only one real run works at a time (a second one returns
    `stopped: "busy"`). Both stop at the end of the table, the time budget, or once `max_keys`
    keys were deleted (or, dry, counted)."""
    cfg = get_settings()
    days = cfg.archive_retention_days if days is None else days
    batch = cfg.archive_retention_batch if batch is None else batch
    pause_s = cfg.archive_retention_pause_s if pause_s is None else pause_s
    max_seconds = cfg.archive_retention_max_seconds if max_seconds is None else max_seconds
    max_keys = cfg.archive_retention_max_keys if max_keys is None else max_keys
    grace = cfg.archive_retention_object_grace_s if object_grace_s is None else object_grace_s
    if not dry_run and grace < MIN_OBJECT_GRACE_S:
        raise ValueError(f"the object grace period must be at least {MIN_OBJECT_GRACE_S} s, "
                         "longer than a recorder's store timeout")
    if (days < 1 or not 1 <= batch <= MAX_BATCH or not 0 <= pause_s <= 60 or max_seconds <= 0
            or max_keys < 1):
        raise ValueError(f"days >= 1, batch 1..{MAX_BATCH}, pause 0..60 s, a positive time budget "
                         "and max_keys >= 1")
    result = RetentionResult(enabled=cfg.archive_retention_enabled, dry_run=dry_run)
    if not dry_run and not cfg.archive_retention_enabled:
        result.stopped = "disabled"
        return asdict(result)
    options = dict(days=days, batch=batch, pause_s=pause_s, max_seconds=max_seconds,
                   max_keys=max_keys, after=after, object_grace_s=grace,
                   session_factory=session_factory, clock=clock)
    if dry_run:
        return await _run(result, **options)
    async with _only_run(session_factory) as alone:
        if not alone:
            result.stopped = "busy"
            return asdict(result)
        return await _run(result, **options)


@asynccontextmanager
async def _only_run(session_factory):
    """Hold a session-level advisory lock for the run on a connection of its own (idle between
    its two statements, so it pins no snapshot) -> whether this run got it. SQLite: always."""
    async with session_factory.kw["bind"].connect() as connection:
        if connection.dialect.name != "postgresql":
            yield True
            return
        got = bool((await connection.execute(text("SELECT pg_try_advisory_lock(:key)"),
                                             {"key": RUN_LOCK_KEY})).scalar_one())
        await connection.commit()
        try:
            yield got
        finally:
            if got:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"),
                                         {"key": RUN_LOCK_KEY})
                await connection.commit()


async def _run(result: RetentionResult, *, days: int, batch: int, pause_s: float,
               max_seconds: float, max_keys: int, after: int | None, object_grace_s: float,
               session_factory, clock) -> dict:
    dry_run = result.dry_run
    now = utcnow_naive()
    cutoff = now - timedelta(days=days)
    result.cutoff = cutoff.isoformat()
    keep_endpoints = archive_policy_endpoints()
    started = clock()
    cursor = await _read_cursor(session_factory) if after is None else after
    async with session_factory() as db:
        await _bounded(db)
        upper = int((await db.execute(select(func.max(ArchiveKey.id)))).scalar_one() or 0)
    result.start_after = result.next_after = cursor
    result.upper_id = upper
    failures = 0
    try:
        if not dry_run:
            try:
                await _delete_due_objects(result, grace_s=object_grace_s,
                                          session_factory=session_factory,
                                          spent=lambda: clock() - started >= max_seconds)
            except DBAPIError as exc:
                raise ObjectDeletesFailed(f"pending object deletes stopped ({type(exc).__name__})") from exc
        while True:
            if cursor >= upper:
                result.pass_complete, result.stopped = True, "pass_complete"
                break
            if clock() - started >= max_seconds:
                result.stopped = "time_budget"
                break
            if result.keys >= max_keys:
                result.stopped = "max_keys"
                break
            upto = min(cursor + batch, upper)
            try:
                await _batch(result, after=cursor, upto=upto, cutoff=cutoff, now=now,
                             keep_endpoints=keep_endpoints, session_factory=session_factory)
                failures = 0
            except DBAPIError as exc:
                failures += 1
                result.skipped_batches += 1
                _log.warning("archive retention: batch (%d, %d] skipped (%s)", cursor, upto,
                             type(exc).__name__)
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    result.stopped = "failures"
                    cursor = upto
                    break
            result.batches += 1
            cursor = upto
            result.next_after = cursor
            if pause_s:
                await asyncio.sleep(pause_s)
    except (ObjectStoreMissing, ObjectDeletesFailed) as exc:
        result.stopped, result.error = "error", str(exc)
    result.next_after = cursor
    if not dry_run:
        async with session_factory() as db:
            await _save_cursor(db, cursor, pass_done=result.pass_complete)
            await db.commit()
    _log.info("archive retention: %s", asdict(result))
    return asdict(result)


async def _batch(result: RetentionResult, *, after: int, upto: int, cutoff: datetime,
                 now: datetime, keep_endpoints: set[str], session_factory) -> None:
    async with session_factory() as db:
        await _bounded(db)
        found = await candidates(db, after=after, upto=upto, cutoff=cutoff, now=now,
                                 keep_endpoints=keep_endpoints)
        ids = [i for i, _ in found]
        if not found:
            return
        if result.dry_run:
            orphans = await objects_only_under(db, ids)
            snapshots = await _snapshot_count(db, ids)
            marks = await _mark_count(db, [h for _, h in found])
    if result.dry_run:
        result.keys += len(found)
        result.snapshots += snapshots
        result.marks += marks
        result.objects += len(orphans)
        return
    async with session_factory() as db:
        await _bounded(db)
        locked = (await db.execute(
            select(ArchiveKey.id, ArchiveKey.key_hash)
            .where(ArchiveKey.id.in_(ids), *dormant(cutoff, now))
            .order_by(ArchiveKey.id)
            .with_for_update(skip_locked=True, of=ArchiveKey))).all()
        key_ids, key_hashes = [int(i) for i, _ in locked], [str(h) for _, h in locked]
        keys = snapshots = marks = pending = 0
        if locked:
            snapshots = await _snapshot_count(db, key_ids)
            stored = set((await db.execute(
                select(ArchiveSnapshot.content_hash).distinct()
                .where(ArchiveSnapshot.key_id.in_(key_ids),
                       ArchiveSnapshot.body_storage.in_(_OBJECT_STORED)))).scalars().all())
            keys = await erase_archive_keys(db, key_ids)
            # Same transaction: a body hash goes on the pending list only if this commit lands.
            pending = await _record_unreferenced(db, stored, utcnow_naive())
        await _save_cursor(db, upto)
        # Last, just before the commit: a settle locks its team's org row and then upserts this
        # mark, so a mark lock held across the slow deletes above would queue that team's
        # payments behind this job. Marks a settle holds right now are skipped, not waited for.
        marks = await erase_archive_marks(db, key_hashes, unpaid_since=cutoff)
        await db.commit()
    result.objects_pending += pending
    result.keys += keys
    result.snapshots += snapshots
    result.marks += marks
