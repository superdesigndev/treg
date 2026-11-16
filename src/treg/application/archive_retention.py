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
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased

from .. import archive, archive_bodies
from ..config import get_settings
from ..domain.governance.teams import erase_archive_keys, erase_archive_marks
from ..infra.db import session_maker
from ..models import ArchiveKey, ArchiveKeyOrg, ArchiveRetentionCursor, ArchiveSnapshot, IdempotentCall
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
    stopped: str = ""       # "pass_complete" | "time_budget" | "failures" | "disabled" | "error"
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
              after: int | None = None, session_factory=session_maker,
              clock=time.monotonic) -> dict:
    """One bounded run -> the `RetentionResult` as a dict.

    `dry_run` reads the same batches and deletes nothing, setting or not; it starts at the saved
    cursor (or `after`) and never moves it. A real run does nothing unless
    `archive_retention_enabled`. Both stop at the end of the table or the time budget."""
    cfg = get_settings()
    days = cfg.archive_retention_days if days is None else days
    batch = cfg.archive_retention_batch if batch is None else batch
    pause_s = cfg.archive_retention_pause_s if pause_s is None else pause_s
    max_seconds = cfg.archive_retention_max_seconds if max_seconds is None else max_seconds
    if days < 1 or not 1 <= batch <= MAX_BATCH or not 0 <= pause_s <= 60 or max_seconds <= 0:
        raise ValueError(f"days >= 1, batch 1..{MAX_BATCH}, pause 0..60 s and a positive time budget")
    result = RetentionResult(enabled=cfg.archive_retention_enabled, dry_run=dry_run)
    if not dry_run and not cfg.archive_retention_enabled:
        result.stopped = "disabled"
        return asdict(result)

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
        keys = snapshots = marks = 0
        if locked:
            key_ids, key_hashes = [int(i) for i, _ in locked], [str(h) for _, h in locked]
            snapshots = await _snapshot_count(db, key_ids)
            keys = await erase_archive_keys(db, key_ids)
            await _save_cursor(db, upto)
            # Last, just before the commit: a settle locks its team's org row and then upserts
            # this mark, so a mark lock held across the slow deletes above would queue that
            # team's payments behind this job.
            marks = await erase_archive_marks(db, key_hashes, unpaid_since=cutoff)
        else:
            await _save_cursor(db, upto)
        await db.commit()
    result.keys += keys
    result.snapshots += snapshots
    result.marks += marks
