"""Erase what a team stored in the archive (docs/context/architecture/archive.md, "Erasure").

A team's own-credential answers (every key under its `org:` or `conn:` scope) and the
`ArchiveKeyOrg` marks that tie the team to questions it paid for are the team's to have removed.
Platform-key (public) answers are public questions and stay by default; a request with
`include_public` also erases the public keys that only this team ever marked, for a customer who
asks for them in writing.

Every path runs the same batch engine (`_drive`):

- phases in order: private keys, then public keys (only with `include_public`), then the team's
  remaining marks. Public keys are found only through the marks, so a batch deletes only its own
  keys' marks and the rest go last: a run that stops anywhere resumes from `phase` + `cursor`.
- each batch: a short read transaction selects the keys and the bodies only they point at; the
  transaction ends; the objects are deleted (a missing object is already deleted); then one short
  write transaction deletes the rows, with the "no other team marked it" check inside the DELETE.
- every transaction sets `statement_timeout` and `lock_timeout`; a timeout halves the batch and
  retries; repeated timeouts at one key fail the request.

The queued path (`run_pending`, the `treg-worker admin erase-pending` cron) holds one database
connection and a Postgres advisory lock for the whole run, so only one erasure runs at a time.
The owner team delete erases a small team inline (`erase_small`, on short request-pool sessions
with no connection held across object I/O) and queues a bigger one.

Accepted race: a public key another team marks after its bodies were deleted keeps its rows, and
its snapshot reads back as "bytes not on file", which every reader treats as a miss. Each such
key is logged with the request id.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import archive_bodies
from ..domain.governance import archive_erasure as rows
from ..infra.db import session_maker, single_connection_engine
from ..models import ArchiveErasureRequest, Org, User

_log = logging.getLogger("treg.archive.erasure")

_LOCK = (0x65726173, 0x61726368)   # pg_try_advisory_lock(int, int) pair: "eras", "arch"
START_BATCH = 25                   # keys per batch at the start of every run
_MIN_BATCH = 1
_TIMEOUTS_AT_MIN = 3               # timeouts in a row at one key before the request fails
_MARKS_PER_KEY = 20                # the final marks step deletes batch * this per statement
_PAUSE_S = 0.5                     # between full batches, so the database gets room
BUDGET_S = 480.0                   # one cron run; the cron fires every 10 minutes
_STATEMENT_TIMEOUT = "30s"
_LOCK_TIMEOUT = "3s"
_TIMEOUT_SQLSTATES = {"57014", "55P03"}   # query_canceled (statement_timeout), lock_not_available

_local_lock = asyncio.Lock()       # SQLite stand-in for the advisory lock (tests, local dev)


class NotOptedOut(ValueError):
    """The team is still in the archive: opt it out first, so nothing is recorded meanwhile."""


class OrgNotFound(LookupError):
    pass


class AlreadyQueued(ValueError):
    """The team already has a pending or running erasure request."""


class Busy(RuntimeError):
    """Another erasure holds the lock."""


class ObjectsFailed(RuntimeError):
    """Some archive objects could not be deleted; the rows stay for a retry."""


class TooManyTimeouts(RuntimeError):
    pass


class NoObjectStore(ObjectsFailed):
    """Rows point at object-store bodies but this process has no store configured: deleting the
    rows would leave the bodies behind for good, so nothing is deleted."""


async def _require_store(db: AsyncSession, key_ids: list[int]) -> None:
    if archive_bodies._store is None and await rows.has_stored_objects(db, key_ids):
        raise NoObjectStore("archive rows point at object-store bodies, but no archive object "
                            "store is configured in this process (TREG_ARCHIVE_MODE, "
                            "TREG_ARCHIVE_BODY_* and TREG_ARCHIVE_OBJECT_STORE_*)")


@dataclass
class _Run:
    org_id: int
    include_public: bool
    phase: str = rows.PHASE_PRIVATE
    cursor: str = ""
    request_id: int | None = None
    keys: int = 0
    snapshots: int = 0
    objects: int = 0
    marks: int = 0
    kept_shared: int = 0

    def label(self) -> str:
        return f"request {self.request_id}" if self.request_id else f"org {self.org_id} (direct)"


# ---------------------------------------------------------------------------------------------
# Database plumbing

async def _bounded(db: AsyncSession) -> None:
    """Short limits for this transaction (Postgres only; SQLite has no such settings)."""
    if (await db.connection()).dialect.name == "postgresql":
        await db.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        await db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def _is_timeout(exc: BaseException) -> bool:
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if (getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)) in _TIMEOUT_SQLSTATES:
            return True
        stack.extend([getattr(e, "orig", None), e.__cause__, e.__context__])
    return False


@asynccontextmanager
async def _exclusive():
    """-> a session factory on ONE connection that holds the erasure lock; raises `Busy`.

    The connection holds the session-level advisory lock for the whole run. Between batches,
    and while objects are deleted, it is idle with no open transaction."""
    engine = single_connection_engine()
    if engine is None:
        if _local_lock.locked():
            raise Busy("another archive erasure is running")
        async with _local_lock:
            yield session_maker
        return
    try:
        async with engine.connect() as conn:
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"),
                                      {"a": _LOCK[0], "b": _LOCK[1]})).scalar()
            await conn.commit()
            if not got:
                raise Busy("another archive erasure is running")
            try:
                yield lambda: AsyncSession(bind=conn, expire_on_commit=False)
            finally:
                try:
                    await conn.rollback()
                    await conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"),
                                       {"a": _LOCK[0], "b": _LOCK[1]})
                    await conn.commit()
                except Exception as exc:  # noqa: BLE001 - closing the connection releases it too
                    _log.warning("archive erasure: unlock failed: %s", exc)
    finally:
        await engine.dispose()


async def _delete_objects(hashes: set[str]) -> int:
    """Remove the given bodies from the object store -> how many could not be removed. A missing
    object is already deleted (the store treats it so). Without a configured store there is
    nothing to do: bodies then live only in the rows. The in-process upload cache forgets the
    hash too, so an identical answer recorded later is uploaded again."""
    store = archive_bodies._store
    failed = 0
    if store is None:
        return 0
    for content_hash in sorted(hashes):
        try:
            await store.delete(content_hash)
            archive_bodies.forget(content_hash)
        except Exception as exc:  # noqa: BLE001 - one object failing must not stop the rest
            failed += 1
            _log.warning("archive erasure: object %s not deleted: %s", content_hash[:12], exc)
    return failed


async def _objects(hashes: set[str]) -> None:
    failed = await _delete_objects(hashes)
    if failed:
        raise ObjectsFailed(f"{failed} archive object(s) could not be deleted")


async def _commit_progress(db: AsyncSession, run: _Run, *, phase: str, cursor: str, **delta) -> bool:
    """Save the batch's counts with its deletes, commit, then count them in memory -> False when
    the request was cancelled meanwhile."""
    still = True
    if run.request_id is not None:
        still = await rows.save_progress(db, run.request_id, phase=phase, cursor=cursor, **delta)
    await db.commit()
    run.phase, run.cursor = phase, cursor
    for name, n in delta.items():
        setattr(run, name, getattr(run, name) + n)
    return still


# ---------------------------------------------------------------------------------------------
# One batch per phase -> (rows selected, still running)

async def _private_batch(factory, run: _Run, limit: int) -> tuple[int, bool]:
    async with factory() as db:
        await _bounded(db)
        key_ids = await rows.private_archive_key_ids(
            db, run.org_id, limit=limit, after_id=int(run.cursor or 0))
        orphans = await rows.archive_bodies_only_under(db, key_ids)
        await _require_store(db, key_ids)
        await db.commit()
    if not key_ids:
        return 0, True
    await _objects(orphans)
    async with factory() as db:
        await _bounded(db)
        gone = await rows.delete_private_keys(db, key_ids)
        still = await _commit_progress(db, run, phase=rows.PHASE_PRIVATE, cursor=str(max(key_ids)),
                                       keys=len(gone.keys), snapshots=gone.snapshots,
                                       objects=len(orphans))
    return len(key_ids), still


async def _public_batch(factory, run: _Run, limit: int) -> tuple[int, bool]:
    async with factory() as db:
        await _bounded(db)
        marked = await rows.marked_public_keys(db, run.org_id, after_hash=run.cursor, limit=limit)
        key_ids = [m.key_id for m in marked if m.erasable and m.key_id is not None]
        orphans_by_key = await rows.orphan_bodies_by_key(db, key_ids)
        await _require_store(db, key_ids)
        await db.commit()
    if not marked:
        return 0, True
    orphans = set().union(*orphans_by_key.values()) if orphans_by_key else set()
    await _objects(orphans)
    async with factory() as db:
        await _bounded(db)
        gone = await rows.delete_unshared_public_keys(db, run.org_id, key_ids)
        n_marks = await rows.delete_marks(db, run.org_id, [m.key_hash for m in marked])
        kept = sorted(set(key_ids) - set(gone.keys))
        still = await _commit_progress(db, run, phase=rows.PHASE_PUBLIC, cursor=marked[-1].key_hash,
                                       keys=len(gone.keys), snapshots=gone.snapshots,
                                       objects=len(orphans), marks=n_marks, kept_shared=len(kept))
    for key_id in kept:
        _log.warning("archive erasure %s: public key %s became shared after its bodies were "
                     "deleted; rows kept, %d body object(s) gone", run.label(), key_id,
                     len(orphans_by_key.get(key_id, ())))
    return len(marked), still


async def _marks_batch(factory, run: _Run, limit: int) -> tuple[int, bool]:
    async with factory() as db:
        await _bounded(db)
        n = await rows.delete_next_marks(db, run.org_id, limit=limit * _MARKS_PER_KEY)
        still = await _commit_progress(db, run, phase=rows.PHASE_MARKS, cursor="", marks=n)
    return (limit if n >= limit * _MARKS_PER_KEY else n), still


_BATCHES = {rows.PHASE_PRIVATE: _private_batch, rows.PHASE_PUBLIC: _public_batch,
            rows.PHASE_MARKS: _marks_batch}


def _next_phase(run: _Run) -> str:
    if run.phase == rows.PHASE_PRIVATE and run.include_public:
        return rows.PHASE_PUBLIC
    if run.phase in (rows.PHASE_PRIVATE, rows.PHASE_PUBLIC):
        return rows.PHASE_MARKS
    return rows.PHASE_DONE


async def _should_stop(factory, run: _Run) -> str | None:
    """Between batches: "cancelled" when the request is no longer running, "opted_in" when its
    team exists and is back in the archive. A team that is gone is fine: the request outlives it."""
    async with factory() as db:
        await _bounded(db)
        org = (await db.execute(select(Org.archive_opt_out_at).where(Org.id == run.org_id))).first()
        status = await rows.status_of(db, run.request_id) if run.request_id is not None else rows.RUNNING
        await db.commit()
    if status != rows.RUNNING:
        return "cancelled"
    if org is not None and org[0] is None:
        return "opted_in"
    return None


async def _drive(factory, run: _Run, *, deadline: float = math.inf) -> str:
    """Run batches until the erasure is done -> "done" | "paused" (out of time) | "cancelled" |
    "opted_in". Raises `ObjectsFailed`, `TooManyTimeouts` or any other database error."""
    batch, timeouts_at_min = START_BATCH, 0
    while run.phase != rows.PHASE_DONE:
        stop = await _should_stop(factory, run)
        if stop:
            return stop
        if time.monotonic() >= deadline:
            return "paused"
        try:
            selected, still = await _BATCHES[run.phase](factory, run, batch)
        except Exception as exc:
            if not _is_timeout(exc):
                raise
            if batch > _MIN_BATCH:
                batch = max(_MIN_BATCH, batch // 2)
                _log.warning("archive erasure %s: timeout in %s, batch now %d: %s",
                             run.label(), run.phase, batch, exc)
            else:
                timeouts_at_min += 1
                if timeouts_at_min >= _TIMEOUTS_AT_MIN:
                    raise TooManyTimeouts(f"{timeouts_at_min} timeouts at batch {batch}: {exc}") from exc
            if run.request_id is not None:
                async with factory() as db:
                    await rows.note_error(db, run.request_id, f"timeout (retried): {exc}")
                    await db.commit()
            await asyncio.sleep(_PAUSE_S)
            continue
        timeouts_at_min = 0
        if not still:
            return "cancelled"
        if selected == 0:
            phase = _next_phase(run)
            if run.request_id is not None and phase != rows.PHASE_DONE:
                async with factory() as db:
                    await _commit_progress(db, run, phase=phase, cursor="")
            run.phase, run.cursor = phase, ""
        elif selected >= batch:
            await asyncio.sleep(_PAUSE_S)
    return "done"


def _result(run: _Run) -> dict:
    return {"org_id": run.org_id, "keys": run.keys, "snapshots": run.snapshots,
            "objects": run.objects, "marks": run.marks, "kept_shared": run.kept_shared}


# ---------------------------------------------------------------------------------------------
# Entry points

async def _require_opted_out(org_id: int) -> None:
    async with session_maker() as db:
        row = (await db.execute(select(Org.archive_opt_out_at).where(Org.id == org_id))).first()
    if row is not None and row[0] is None:
        raise NotOptedOut(f"org {org_id} is still in the archive; opt it out first")


async def erase_org(org_id: int, *, include_public: bool = False) -> dict:
    """Erase one team now, on the safe path, under the erasure lock (`treg-worker admin
    erase-archive --org`). Refuses (`NotOptedOut`) a team still in the archive; raises `Busy`
    when another erasure runs, `ObjectsFailed` when objects could not be deleted (the rows stay
    for a retry)."""
    await _require_opted_out(org_id)
    run = _Run(org_id=org_id, include_public=include_public)
    async with _exclusive() as factory:
        outcome = await _drive(factory, run)
    if outcome == "opted_in":
        raise NotOptedOut(f"org {org_id} opted back in to the archive during the erasure")
    _log.info("archive erasure for org %s: %s", org_id, _result(run))
    return _result(run)


async def erase_small(org_id: int) -> bool:
    """For the owner team delete: erase a small team inline -> True, or False (nothing done)
    when the team is too big, so the cascade queues it. Short sessions on the request pool,
    none held across object I/O. Does not take the erasure lock: a queued run on the same rows
    is harmless, because every DELETE counts only what it deleted."""
    async with session_maker() as db:
        big = (await rows.has_private_keys(db, org_id, more_than=rows.INLINE_KEY_LIMIT)
               or await rows.has_marks(db, org_id, more_than=rows.INLINE_MARK_LIMIT))
    if big:
        return False
    await _drive(session_maker, _Run(org_id=org_id, include_public=False))
    return True


async def _run_request(factory, req: ArchiveErasureRequest, deadline: float) -> dict:
    run = _Run(org_id=req.org_id, include_public=req.include_public, phase=req.phase,
               cursor=req.cursor, request_id=req.id)
    error = None
    try:
        outcome = await _drive(factory, run, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the request
        outcome, error = "failed", f"{type(exc).__name__}: {exc}"
        _log.warning("archive erasure %s failed: %s", run.label(), error)
    if outcome in ("done", "failed", "opted_in"):
        status = rows.DONE if outcome == "done" else rows.FAILED
        if outcome == "opted_in":
            error = "the team is back in the archive; opt it out and queue a new request"
        async with factory() as db:
            await rows.finish(db, req.id, status, error)
            await db.commit()
    return {"request_id": req.id, "outcome": outcome, **_result(run)}


async def run_pending(*, budget_s: float = BUDGET_S) -> dict:
    """One cron run: take the lock (or exit), then claim and run requests until the queue is
    empty or the time budget is spent. Progress is saved after every batch."""
    deadline = time.monotonic() + budget_s
    done: list[dict] = []
    try:
        async with _exclusive() as factory:
            while time.monotonic() < deadline:
                async with factory() as db:
                    await _bounded(db)
                    req = await rows.claim_next(db)
                    await db.commit()
                if req is None:
                    break
                result = await _run_request(factory, req, deadline)
                done.append(result)
                if result["outcome"] == "paused":
                    break
    except Busy as exc:
        return {"skipped": str(exc)}
    return {"requests": done}


def describe(row: ArchiveErasureRequest) -> dict:
    iso = lambda t: t.isoformat() + "Z" if t is not None else None  # noqa: E731
    return {
        "request_id": row.id, "org_id": row.org_id, "include_public": row.include_public,
        "status": row.status, "reference": row.reference, "requested_by": row.requested_by,
        "requested_by_user_id": row.requested_by_user_id, "requested_at": iso(row.requested_at),
        "started_at": iso(row.started_at), "finished_at": iso(row.finished_at),
        "phase": row.phase, "counts": {"keys": row.keys, "snapshots": row.snapshots,
                                       "objects": row.objects, "marks": row.marks,
                                       "kept_shared": row.kept_shared},
        "last_error": row.last_error,
    }


async def enqueue(db: AsyncSession, org_id: int, *, include_public: bool, reference: str,
                  requested_by: str) -> dict:
    """Queue an erasure for an opted-out team and return at once. Never erases here."""
    org = await db.get(Org, org_id)
    if org is None:
        raise OrgNotFound(f"org {org_id} not found")
    if org.archive_opt_out_at is None:
        raise NotOptedOut(f"org {org_id} is still in the archive; opt it out first")
    if await rows.open_request(db, org_id) is not None:
        raise AlreadyQueued(f"org {org_id} already has a pending or running erasure")
    user_id = None
    if "@" in requested_by:
        user_id = (await db.execute(select(User.id).where(User.email == requested_by))).scalar_one_or_none()
    try:
        row = await rows.add_request(db, org_id, include_public=include_public, reference=reference,
                                     requested_by=requested_by, requested_by_user_id=user_id)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AlreadyQueued(f"org {org_id} already has a pending or running erasure") from exc
    return describe(row)


async def status(db: AsyncSession, org_id: int) -> dict | None:
    row = await rows.latest_request(db, org_id)
    await db.commit()
    return describe(row) if row is not None else None


async def cancel(db: AsyncSession, org_id: int) -> dict | None:
    row = await rows.cancel_open(db, org_id)
    if row is None:
        await db.rollback()
        return None
    await db.commit()
    return describe(row)
