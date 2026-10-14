"""Erase what a team stored in the archive (docs/context/architecture/archive.md, "Opting out").

A team's own-credential answers (every key under its `org:` or `conn:` scope, with the request
shape those keys carry) and the `ArchiveKeyOrg` marks that tie the team to questions it paid for
are the team's to have removed. Platform-key answers are public questions by construction and
are not the team's data; nothing there names the team once its marks are gone.

Erasure is an explicit act, never a side effect of the opt-out switch: deleting the team runs it,
and `treg-worker admin erase-archive --org <id>` runs it on request. Both require the team to be
opted out of the archive first (or gone), which is what makes the job safe: with the gate shut
nothing of the team's is being recorded while its rows are removed, so there is no in-flight
writer to coordinate with and nothing to retry later.

Objects before rows, in separate sessions, because a request holds no database connection while
object I/O is in flight (AGENTS.md, non-negotiable 3) and because a row that outlives a failed
object delete is what makes a retry possible. The row rules live in `governance.teams`
(`private_archive_key_ids`, `archive_bodies_only_under`, `erase_archive_rows`); team deletion's
cascade uses the last of them, so a deleted team never leaves rows behind even when nobody ran the
object phase. Bodies are content-addressed and deduplicated across keys, so only a body no other
key's snapshot points at is deleted; the judgement and the delete are not atomic with a recording
that lands between them, and such a snapshot reads back as "bytes not on file", which every reader
already tolerates.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from .. import archive_bodies
from ..domain.governance.teams import archive_bodies_only_under, erase_archive_rows, private_archive_key_ids
from ..infra.db import session_maker
from ..models import Org

_log = logging.getLogger("treg.archive.erasure")

_KEY_BATCH = 200          # keys per pass: bounded lock footprint, resumable


class NotOptedOut(ValueError):
    """The team is still in the archive: opt it out first, so nothing is recorded meanwhile."""


async def _delete_objects(hashes: set[str]) -> int:
    """Remove the given bodies from the object store -> how many could not be removed. Without a
    configured store there is nothing to do: bodies then live only in the rows. The in-process
    upload cache forgets the hash too, so an identical answer recorded later is uploaded again
    instead of pointing at bytes that are gone."""
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


async def erase_org(org_id: int, *, session_factory=session_maker) -> dict:
    """Erase everything the team stored: objects first, then rows, in bounded passes.

    Each pass finds a batch of the team's keys and the bodies only they point at, closes the
    session, deletes those objects, then deletes the rows in a fresh transaction. A pass whose
    object deletes failed stops before the rows and raises: the rows are what a retry starts
    from. Refuses (`NotOptedOut`) for a team still in the archive. Short transactions on the
    request pool: a request-shaped job (the owner delete, the worker command), never a resident
    one."""
    async with session_factory() as db:
        row = (await db.execute(select(Org.archive_opt_out_at).where(Org.id == org_id))).first()
    if row is not None and row[0] is None:
        raise NotOptedOut(f"org {org_id} is still in the archive; opt it out first")
    keys = objects = 0
    while True:
        async with session_factory() as db:
            key_ids = await private_archive_key_ids(db, org_id, limit=_KEY_BATCH)
            orphans = await archive_bodies_only_under(db, key_ids) if key_ids else set()
        if not key_ids:
            break
        failed = await _delete_objects(orphans)
        if failed:
            raise RuntimeError(f"{failed} archive object(s) could not be deleted")
        objects += len(orphans)
        async with session_factory() as db:
            keys += await erase_archive_rows(db, org_id, key_ids)
            await db.commit()
    async with session_factory() as db:
        await erase_archive_rows(db, org_id, [])      # the marks alone, when no key was left
        await db.commit()
    _log.info("archive erasure for org %s: %d key(s), %d object(s)", org_id, keys, objects)
    return {"org_id": org_id, "keys": keys, "objects": objects}
