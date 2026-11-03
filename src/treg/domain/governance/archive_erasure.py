"""What of the archive is a team's to have erased, and the queue of erasure requests.

docs/context/architecture/archive.md, "Erasure". The row rules here never commit; the
application (`application/archive_erasure.py`) owns the transactions, the object store and the
batch loop. This module is the only writer of `ArchiveErasureRequest`.

Every batch statement takes a bounded list (one batch of ids or hashes, never a team's whole
set) or an anti-join; selection is keyset-paginated so a stopped run resumes from its cursor.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from sqlalchemy import delete, exists, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from ...models import ArchiveEndpointStat, ArchiveErasureRequest, ArchiveKey, ArchiveKeyOrg, ArchiveSnapshot
from ...timeutil import utcnow_naive

ARCHIVE_PRIVATE_SCOPES = ("org", "conn")

PENDING, RUNNING, DONE, FAILED, CANCELLED = "pending", "running", "done", "failed", "cancelled"
OPEN_STATUSES = (PENDING, RUNNING)
PHASE_PRIVATE, PHASE_PUBLIC, PHASE_MARKS, PHASE_DONE = "private", "public", "marks", "done"

# A team at or under both limits is erased inline by its owner delete; a bigger one is queued.
INLINE_KEY_LIMIT = 25
INLINE_MARK_LIMIT = 500


# ---------------------------------------------------------------------------------------------
# Selection

async def private_archive_key_ids(db: AsyncSession, org_id: int, *, limit: int | None = None,
                                  after_id: int = 0) -> list[int]:
    """The archive keys that are the team's own: keys under its `org:`/`conn:` scopes. A private
    key holds only that team's snapshots, because only that team ever computes its hash
    ("Sharing"). Ascending by id after `after_id` (keyset), so a bounded pass is resumable."""
    owned = select(ArchiveSnapshot.key_id).where(ArchiveSnapshot.origin_org_id == org_id)
    rows = await db.execute(
        select(ArchiveKey.id)
        .where(ArchiveKey.scope.in_(ARCHIVE_PRIVATE_SCOPES), ArchiveKey.id > after_id,
               ArchiveKey.id.in_(owned))
        .order_by(ArchiveKey.id).limit(limit or sys.maxsize))
    return [int(r[0]) for r in rows]


@dataclass(frozen=True)
class MarkedKey:
    """One of the team's `ArchiveKeyOrg` marks and the key row it names, if any."""
    key_hash: str
    key_id: int | None
    erasable: bool          # a public key no other team has marked


async def marked_public_keys(db: AsyncSession, org_id: int, *, after_hash: str = "",
                             limit: int) -> list[MarkedKey]:
    """The team's next `limit` marks after `after_hash` (keyset on the `(org_id, key_hash)`
    unique index), each with its key. A key is erasable when it is public (`scope IS NULL`) and
    no other team has a mark on its hash. Public keys can be found only through the marks
    (platform-key snapshots carry no team), so the marks must outlive the keys they select."""
    other = aliased(ArchiveKeyOrg)
    shared = exists().where(other.key_hash == ArchiveKeyOrg.key_hash, other.org_id != org_id)
    rows = await db.execute(
        select(ArchiveKeyOrg.key_hash, ArchiveKey.id, ArchiveKey.scope, shared.label("shared"))
        .select_from(ArchiveKeyOrg)
        .outerjoin(ArchiveKey, ArchiveKey.key_hash == ArchiveKeyOrg.key_hash)
        .where(ArchiveKeyOrg.org_id == org_id, ArchiveKeyOrg.key_hash > after_hash)
        .order_by(ArchiveKeyOrg.key_hash).limit(limit))
    return [MarkedKey(key_hash, int(key_id) if key_id is not None else None,
                      key_id is not None and scope is None and not bool(is_shared))
            for key_hash, key_id, scope, is_shared in rows]


async def orphan_bodies_by_key(db: AsyncSession, key_ids: list[int]) -> dict[int, set[str]]:
    """Per key, the content hashes that only snapshots under `key_ids` point at: the bodies that
    become orphans once those keys go. Bodies are content-addressed and deduplicated across keys,
    so a hash some other key's snapshot shares is left out. One anti-join on
    `ix_archive_snapshot_content`; `key_ids` is one batch, so the hash list never travels back
    into a query as parameters."""
    if not key_ids:
        return {}
    other = aliased(ArchiveSnapshot)
    shared = exists().where(other.content_hash == ArchiveSnapshot.content_hash,
                            other.key_id.not_in(key_ids))
    rows = await db.execute(
        select(ArchiveSnapshot.key_id, ArchiveSnapshot.content_hash).distinct()
        .where(ArchiveSnapshot.key_id.in_(key_ids), ~shared))
    out: dict[int, set[str]] = {}
    for key_id, content_hash in rows:
        out.setdefault(int(key_id), set()).add(content_hash)
    return out


async def has_stored_objects(db: AsyncSession, key_ids: list[int]) -> bool:
    """True when any snapshot under `key_ids` keeps its bytes in the object store."""
    if not key_ids:
        return False
    return (await db.execute(select(ArchiveSnapshot.id).where(
        ArchiveSnapshot.key_id.in_(key_ids), ArchiveSnapshot.body_storage.in_(("both", "r2")))
        .limit(1))).first() is not None


async def archive_bodies_only_under(db: AsyncSession, key_ids: list[int]) -> set[str]:
    """The orphan hashes of `orphan_bodies_by_key`, as one set."""
    return set().union(*(await orphan_bodies_by_key(db, key_ids)).values())


async def has_private_keys(db: AsyncSession, org_id: int, *, more_than: int = 0) -> bool:
    """True when the team has more than `more_than` private keys. Bounded: reads at most
    `more_than + 1` ids."""
    return len(await private_archive_key_ids(db, org_id, limit=more_than + 1)) > more_than


async def has_marks(db: AsyncSession, org_id: int, *, more_than: int = 0) -> bool:
    rows = await db.execute(select(ArchiveKeyOrg.id).where(ArchiveKeyOrg.org_id == org_id)
                            .limit(more_than + 1))
    return len(rows.all()) > more_than


# ---------------------------------------------------------------------------------------------
# Deletes (no commit)

@dataclass
class Deleted:
    keys: list[int] = field(default_factory=list)
    snapshots: int = 0
    marks: int = 0


async def _delete_keys(db: AsyncSession, key_ids: list[int], keep_if) -> Deleted:
    """Delete the snapshots and keys among `key_ids` that pass `keep_if` (a WHERE clause on
    `ArchiveKey`, evaluated inside each DELETE), then move the running totals.

    The totals subtract what THESE statements deleted (`RETURNING`), never a count read
    beforehand: two erasers on the same rows would otherwise both subtract. The endpoint of each
    key is read first in the same transaction, so snapshots of a key that a racing statement
    kept are still counted. The totals UPDATEs run last, in endpoint order (the order
    `archive.flush_stats` uses), so this transaction holds a shared totals row only until commit."""
    out = Deleted()
    if not key_ids:
        return out
    endpoint_of = {int(k): e for k, e in (await db.execute(
        select(ArchiveKey.id, ArchiveKey.endpoint_id).where(ArchiveKey.id.in_(key_ids)))).all()}
    target = select(ArchiveKey.id).where(ArchiveKey.id.in_(key_ids), keep_if)
    gone = (await db.execute(
        delete(ArchiveSnapshot).where(ArchiveSnapshot.key_id.in_(target))
        .returning(ArchiveSnapshot.key_id, ArchiveSnapshot.body_storage,
                   ArchiveSnapshot.body.is_not(None), ArchiveSnapshot.size_bytes))).all()
    keys_gone = (await db.execute(
        delete(ArchiveKey).where(ArchiveKey.id.in_(key_ids), keep_if)
        .returning(ArchiveKey.id))).all()
    out.keys = sorted(int(r[0]) for r in keys_gone)
    out.snapshots = len(gone)
    per_endpoint: dict[str, list[int]] = {}
    for key_id in out.keys:
        endpoint_id = endpoint_of.get(key_id)
        if endpoint_id is not None:
            per_endpoint.setdefault(endpoint_id, [0, 0, 0, 0])[3] += 1
    for key_id, storage, has_body, size in gone:
        endpoint_id = endpoint_of.get(int(key_id))
        if endpoint_id is None:
            continue
        acc = per_endpoint.setdefault(endpoint_id, [0, 0, 0, 0])
        acc[0] += 1
        if storage is not None or bool(has_body):
            acc[1] += 1
            acc[2] += int(size or 0)
    for endpoint_id in sorted(per_endpoint):
        n_snaps, n_bodies, n_bytes, n_keys = per_endpoint[endpoint_id]
        await db.execute(
            update(ArchiveEndpointStat).where(ArchiveEndpointStat.endpoint_id == endpoint_id)
            .values(snapshots=ArchiveEndpointStat.snapshots - n_snaps,
                    bodies_kept=ArchiveEndpointStat.bodies_kept - n_bodies,
                    kept_bytes=ArchiveEndpointStat.kept_bytes - n_bytes,
                    keys=ArchiveEndpointStat.keys - n_keys))
    return out


async def delete_private_keys(db: AsyncSession, key_ids: list[int]) -> Deleted:
    """Delete these private keys with their snapshots. The scope is checked again in the DELETE."""
    return await _delete_keys(db, key_ids, ArchiveKey.scope.in_(ARCHIVE_PRIVATE_SCOPES))


async def delete_unshared_public_keys(db: AsyncSession, org_id: int, key_ids: list[int]) -> Deleted:
    """Delete these public keys with their snapshots, but only where no other team has marked
    the key. The check is inside each DELETE, not only at selection: a key another team marked
    after the batch was selected is kept."""
    other = aliased(ArchiveKeyOrg)
    unshared = ~exists().where(other.key_hash == ArchiveKey.key_hash, other.org_id != org_id)
    return await _delete_keys(db, key_ids, ArchiveKey.scope.is_(None) & unshared)


async def delete_marks(db: AsyncSession, org_id: int, key_hashes: list[str]) -> int:
    """Delete the team's marks on these hashes (one batch)."""
    if not key_hashes:
        return 0
    result = await db.execute(delete(ArchiveKeyOrg).where(
        ArchiveKeyOrg.org_id == org_id, ArchiveKeyOrg.key_hash.in_(key_hashes)))
    return int(result.rowcount or 0)


async def delete_next_marks(db: AsyncSession, org_id: int, *, limit: int) -> int:
    """Delete up to `limit` of the team's remaining marks: the final step, after no key is left."""
    batch = (select(ArchiveKeyOrg.id).where(ArchiveKeyOrg.org_id == org_id)
             .order_by(ArchiveKeyOrg.key_hash).limit(limit))
    result = await db.execute(delete(ArchiveKeyOrg).where(ArchiveKeyOrg.id.in_(batch)))
    return int(result.rowcount or 0)


async def erase_archive_rows(db: AsyncSession, org_id: int, key_ids: list[int] | None = None) -> int:
    """Delete a SMALL team's archive rows in `db` (no commit) -> keys deleted: the given private
    keys (default: all of them) and every mark. Unbounded by design, so only for a team that
    `has_private_keys`/`has_marks` showed is small; a big team goes through the queue. The bytes an
    object store holds are the application's to remove BEFORE this."""
    await db.execute(delete(ArchiveKeyOrg).where(ArchiveKeyOrg.org_id == org_id))
    if key_ids is None:
        key_ids = await private_archive_key_ids(db, org_id)
    return len((await delete_private_keys(db, key_ids)).keys)


# ---------------------------------------------------------------------------------------------
# The request queue

async def open_request(db: AsyncSession, org_id: int) -> ArchiveErasureRequest | None:
    return (await db.execute(select(ArchiveErasureRequest).where(
        ArchiveErasureRequest.org_id == org_id,
        ArchiveErasureRequest.status.in_(OPEN_STATUSES)))).scalars().first()


async def latest_request(db: AsyncSession, org_id: int) -> ArchiveErasureRequest | None:
    return (await db.execute(select(ArchiveErasureRequest).where(
        ArchiveErasureRequest.org_id == org_id).order_by(ArchiveErasureRequest.id.desc())
        .limit(1))).scalars().first()


async def add_request(db: AsyncSession, org_id: int, *, include_public: bool, reference: str,
                      requested_by: str, requested_by_user_id: int | None) -> ArchiveErasureRequest:
    """Queue a request (no commit). The caller checks `open_request` first; the partial unique
    index refuses a second open request for the team all the same."""
    now = utcnow_naive()
    row = ArchiveErasureRequest(
        org_id=org_id, include_public=include_public, reference=reference,
        requested_by=requested_by, requested_by_user_id=requested_by_user_id,
        requested_at=now, updated_at=now)
    db.add(row)
    await db.flush()
    return row


async def claim_next(db: AsyncSession) -> ArchiveErasureRequest | None:
    """A running request (resume) first, else the oldest pending one, which becomes running.
    The caller holds the erasure lock, so no other worker claims at the same time."""
    row = (await db.execute(select(ArchiveErasureRequest).where(
        ArchiveErasureRequest.status == RUNNING).order_by(ArchiveErasureRequest.id).limit(1))).scalars().first()
    if row is None:
        row = (await db.execute(select(ArchiveErasureRequest).where(
            ArchiveErasureRequest.status == PENDING).order_by(ArchiveErasureRequest.id).limit(1))).scalars().first()
        if row is None:
            return None
        now = utcnow_naive()
        row.status, row.updated_at = RUNNING, now
        row.started_at = row.started_at or now
    await db.flush()
    return row


async def status_of(db: AsyncSession, request_id: int) -> str | None:
    return (await db.execute(select(ArchiveErasureRequest.status).where(
        ArchiveErasureRequest.id == request_id))).scalar_one_or_none()


async def save_progress(db: AsyncSession, request_id: int, *, phase: str, cursor: str,
                        keys: int = 0, snapshots: int = 0, objects: int = 0, marks: int = 0,
                        kept_shared: int = 0) -> bool:
    """Add one batch's counts and move the cursor (no commit) -> False when the request is no
    longer running (cancelled meanwhile); the batch's deletes still commit, they are done."""
    t = ArchiveErasureRequest
    result = await db.execute(update(t).where(t.id == request_id, t.status == RUNNING).values(
        phase=phase, cursor=cursor, keys=t.keys + keys, snapshots=t.snapshots + snapshots,
        objects=t.objects + objects, marks=t.marks + marks, kept_shared=t.kept_shared + kept_shared,
        updated_at=utcnow_naive()))
    return bool(result.rowcount)


async def finish(db: AsyncSession, request_id: int, status: str, error: str | None = None) -> None:
    """End a running request as done or failed (no commit). A cancelled one stays cancelled."""
    t = ArchiveErasureRequest
    now = utcnow_naive()
    values = {"status": status, "finished_at": now, "updated_at": now}
    if status == DONE:
        values["phase"] = PHASE_DONE
    if error is not None:
        values["last_error"] = error[:2000]
    await db.execute(update(t).where(t.id == request_id, t.status == RUNNING).values(**values))


async def note_error(db: AsyncSession, request_id: int, error: str) -> None:
    """Record a retried error on a request that keeps running (no commit)."""
    t = ArchiveErasureRequest
    await db.execute(update(t).where(t.id == request_id).values(
        last_error=error[:2000], updated_at=utcnow_naive()))


async def cancel_open(db: AsyncSession, org_id: int) -> ArchiveErasureRequest | None:
    """Mark the team's open request cancelled (no commit) -> it, or None. A running worker sees
    the status between batches and stops after its current batch."""
    row = await open_request(db, org_id)
    if row is None:
        return None
    now = utcnow_naive()
    row.status, row.finished_at, row.updated_at = CANCELLED, now, now
    await db.flush()
    return row


async def count_open(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count()).select_from(ArchiveErasureRequest).where(
        ArchiveErasureRequest.status.in_(OPEN_STATUSES)))).scalar_one())
