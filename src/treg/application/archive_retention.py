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

Objects before rows, as in erasure (application/archive_erasure.py): a batch reads its candidates
and the bodies only they point at, closes its session, deletes those objects, then deletes the rows
in a fresh, capped transaction that locks the keys (skipping any a recorder holds) and repeats the
candidate predicate, so a key asked for meanwhile stays. Such a key, or a batch whose row delete
fails, may have lost its objects; every reader treats missing bytes as a miss, and the next live
call records them again. A failed object delete keeps that batch's rows for the next pass, and a
batch that would delete objects with no object store configured stops the run, so no rows go while
their bytes stay behind.

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

from sqlalchemy import case, exists, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased

from .. import archive, archive_bodies
from ..config import get_settings
from ..domain.governance.teams import (
    apply_archive_stat_deltas, erase_archive_keys, erase_archive_marks,
)
from ..infra.db import session_maker
from ..models import (
    ArchiveKey, ArchiveKeyOrg, ArchiveRetentionCursor, ArchiveSnapshot,
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


class ObjectStoreMissing(RuntimeError):
    """Rows whose bytes live in object storage would go while the bytes stay: refuse."""


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
    objects: int = 0
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


async def _delete_objects(hashes: set[str]) -> int:
    """-> how many could not be deleted. The in-process upload cache forgets each one."""
    store = archive_bodies._store
    if hashes and store is None:
        raise ObjectStoreMissing("archive objects to delete but no object store is configured")
    failed = 0
    for content_hash in sorted(hashes):
        try:
            await store.delete(content_hash)
            archive_bodies.forget(content_hash)
        except Exception as exc:  # noqa: BLE001 - one object failing must not stop the rest
            failed += 1
            _log.warning("archive retention: object %s not deleted (%s)", content_hash[:12],
                         type(exc).__name__)
    return failed


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
    if (days < 1 or not 1 <= batch <= MAX_BATCH or not 0 <= pause_s <= 60 or max_seconds <= 0
            or max_keys < 1):
        raise ValueError(f"days >= 1, batch 1..{MAX_BATCH}, pause 0..60 s, a positive time budget "
                         "and max_keys >= 1")
    result = RetentionResult(enabled=cfg.archive_retention_enabled, dry_run=dry_run)
    if not dry_run and not cfg.archive_retention_enabled:
        result.stopped = "disabled"
        return asdict(result)
    options = dict(days=days, batch=batch, pause_s=pause_s, max_seconds=max_seconds,
                   max_keys=max_keys, after=after, session_factory=session_factory, clock=clock)
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
               max_seconds: float, max_keys: int, after: int | None, session_factory,
               clock) -> dict:
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
    except ObjectStoreMissing as exc:
        result.stopped, result.error = "error", str(exc)
    result.next_after = cursor
    if not dry_run:
        async with session_factory() as db:
            await _save_cursor(db, cursor, pass_done=result.pass_complete)
            await db.commit()
    _log.info("archive retention: %s", asdict(result))
    return asdict(result)


async def _hash_only_where_bytes_went(db, deleted: set[str], stats: dict[str, list[int]]) -> None:
    """Every remaining snapshot whose object this batch deleted, after the batch's keys and their
    snapshots are gone: a key the locked re-check kept, or another key that recorded the same body
    while the objects went (its upload saw the object and skipped). Mark them as no longer holding
    those bytes (`r2` -> hash-only, `both` -> `db`), so readers treat them as a miss or read the
    row's copy, and a retry row keeps its own copy instead of trimming it against bytes that are
    gone. The bodies left with no copy are added to `stats`."""
    if not deleted:
        return
    gone = (await db.execute(
        update(ArchiveSnapshot)
        .where(ArchiveSnapshot.content_hash.in_(deleted),
               ArchiveSnapshot.body_storage.in_(("r2", "both")))
        .values(body_storage=case((ArchiveSnapshot.body_storage == "both", "db"), else_=None))
        .returning(ArchiveSnapshot.key_id, ArchiveSnapshot.body_storage,
                   ArchiveSnapshot.size_bytes)
        .execution_options(synchronize_session=False))).all()
    freed = [(int(key_id), int(size or 0)) for key_id, storage, size in gone if storage is None]
    if not freed:
        return
    endpoint_of = dict((await db.execute(
        select(ArchiveKey.id, ArchiveKey.endpoint_id)
        .where(ArchiveKey.id.in_({k for k, _ in freed})))).all())
    for key_id, size in freed:
        acc = stats.setdefault(endpoint_of[key_id], [0, 0, 0, 0])
        acc[1] += 1
        acc[2] += size


async def _batch(result: RetentionResult, *, after: int, upto: int, cutoff: datetime,
                 now: datetime, keep_endpoints: set[str], session_factory) -> None:
    async with session_factory() as db:
        await _bounded(db)
        found = await candidates(db, after=after, upto=upto, cutoff=cutoff, now=now,
                                 keep_endpoints=keep_endpoints)
        ids = [i for i, _ in found]
        if not found:
            return
        orphans = await objects_only_under(db, ids)
        if result.dry_run:
            snapshots = await _snapshot_count(db, ids)
            marks = await _mark_count(db, [h for _, h in found])
    if result.dry_run:
        result.keys += len(found)
        result.snapshots += snapshots
        result.marks += marks
        result.objects += len(orphans)
        return
    if await _delete_objects(orphans):
        result.skipped_batches += 1      # rows stay, so the next pass retries these objects
        return
    result.objects += len(orphans)
    async with session_factory() as db:
        await _bounded(db)
        locked = (await db.execute(
            select(ArchiveKey.id, ArchiveKey.key_hash)
            .where(ArchiveKey.id.in_(ids), *dormant(cutoff, now))
            .order_by(ArchiveKey.id)
            .with_for_update(skip_locked=True, of=ArchiveKey))).all()
        key_ids, key_hashes = [int(i) for i, _ in locked], [str(h) for _, h in locked]
        keys = snapshots = marks = 0
        stats: dict[str, list[int]] = {}
        if locked:
            snapshots = await _snapshot_count(db, key_ids)
            keys = await erase_archive_keys(db, key_ids, stats=stats)
        await _hash_only_where_bytes_went(db, orphans, stats)
        await apply_archive_stat_deltas(db, stats)       # one pass, in endpoint order
        await _save_cursor(db, upto)
        # Last, just before the commit: a settle locks its team's org row and then upserts this
        # mark, so a mark lock held across the slow deletes above would queue that team's
        # payments behind this job. Marks a settle holds right now are skipped, not waited for.
        marks = await erase_archive_marks(db, key_hashes, unpaid_since=cutoff)
        await db.commit()
    result.keys += keys
    result.snapshots += snapshots
    result.marks += marks
