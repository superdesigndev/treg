"""Pictures: upload into staging, publish to the public bucket, retire, and set avatars.

The sequence for every kind (`domain.images.KINDS`):

1. upload  - a short rate-limit check in its own session, then the re-draw (in a worker thread),
             then the files go to STAGING. The caller gets the key and a preview URL.
2. publish - when the picture is set (an avatar) or approved (a /showcase file): read each file
             from staging and write it to PUBLIC. Idempotent.
3. retire  - after a row stops pointing at a key, delete it from PUBLIC unless another row still
             does. Staging is never cleaned here: the bucket's lifecycle rule expires it.
4. sweep   - `treg-worker media sweep`: delete every public picture no row names any more, such as
             those of a deleted team or person, through the same check.

No database connection is held while the buckets are read or written (AGENTS.md rule 3): each
database step opens and closes its own session around the storage I/O. This module is the only
writer of `user.avatar_key` and `org.avatar_key`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .. import ratestore
from ..config import get_settings
from ..domain.images import PREFIX, ImageRejected, is_stored_key, keys_for, kind_of, parse_key
from ..infra import media_store as stores
from ..infra.db import session_maker
from ..models import Org, ShowcaseApp, User

log = logging.getLogger("treg.images")

UPLOAD_NS = "image_upload"
UPLOAD_WINDOW_S = 3600
UPLOAD_MAX_PER_USER = 40

_store_override: stores.MediaStore | None = None
_store_cache: tuple[tuple, stores.MediaStore | None] | None = None


class ImagesOff(Exception):
    """Pictures are not configured on this registry."""


def use_store(store: stores.MediaStore | None) -> None:
    """Tests and local tools: serve every call from `store` (None restores the configured one)."""
    global _store_override
    _store_override = store


def store() -> stores.MediaStore | None:
    global _store_cache
    if _store_override is not None:
        return _store_override
    s = get_settings()
    sig = (s.media_store_endpoint, s.media_store_access_key_id, s.media_staging_bucket, s.media_public_bucket)
    if _store_cache is None or _store_cache[0] != sig:
        try:
            opened = stores.open_media_store(s)
        except ValueError as exc:   # an endpoint that is neither https nor localhost: pictures stay off
            log.error("picture storage is off: %s", exc)
            opened = None
        _store_cache = (sig, opened)
    return _store_cache[1]


def enabled() -> bool:
    return store() is not None


def _require_store() -> stores.MediaStore:
    st = store()
    if st is None:
        raise ImagesOff()
    return st


def public_url(key: str | None) -> str | None:
    if not key:
        return None
    base = get_settings().media_base_url.rstrip("/")
    return f"{base}/{key}" if base else f"/media-files/{key}"


def staging_url(key: str) -> str:
    return f"/images/staging/{key}"


async def upload(kind_name: str, data: bytes, *, user_id: int) -> dict:
    """Check, re-draw and stage one upload. Raises ImageRejected, ImagesOff or ratestore refusal."""
    kind = kind_of(kind_name)
    st = _require_store()
    async with session_maker() as db:
        await ratestore.sweep(db, UPLOAD_NS)
        allowed = await ratestore.rate_check(db, UPLOAD_NS, [(f"u:{user_id}", UPLOAD_MAX_PER_USER)], UPLOAD_WINDOW_S)
        await db.commit()
    if not allowed:
        raise ImageRejected("rate_limited", "too many uploads - try again later")
    from ..infra.imaging import render  # Pillow: server-only, imported on first upload
    rendered = await asyncio.to_thread(render, kind, data)
    for key, body, content_type in rendered.files:
        await st.put("staging", key, body, content_type)
    primary = rendered.files[0][0]
    log.info("image staged kind=%s user=%s", kind.name, user_id)
    return {"key": primary, "kind": kind.name, "preview_url": staging_url(primary)}


async def publish(key: str) -> None:
    """Copy one staged picture (all its sizes) to the public bucket. Idempotent: a file already
    public and no longer staged is fine; one in neither place is refused."""
    parsed = parse_key(key)
    if parsed is None:
        raise ImageRejected("bad_key", "unknown picture")
    kind, sha = parsed
    st = _require_store()
    for file_key in keys_for(kind, sha):
        body = await st.get("staging", file_key)
        if body is None:
            if await st.get("public", file_key) is not None:
                continue
            raise ImageRejected("missing", "the picture has expired - upload it again")
        content_type = "image/png" if file_key.endswith(".png") else "image/webp"
        await st.put("public", file_key, body, content_type)


async def _used_keys() -> set[str]:
    """Every picture a row names: avatars, and every app's icon and screenshots (live and proposed)."""
    async with session_maker() as db:
        used = set((await db.execute(select(User.avatar_key).where(User.avatar_key.is_not(None)))).scalars())
        used |= set((await db.execute(select(Org.avatar_key).where(Org.avatar_key.is_not(None)))).scalars())
        for icon, shots, pending in (await db.execute(
                select(ShowcaseApp.icon_key, ShowcaseApp.shot_keys, ShowcaseApp.pending))).all():
            used |= {icon, *(shots or [])}
            if pending:
                used |= {pending.get("icon_key"), *(pending.get("shot_keys") or [])}
    used.discard(None)
    return used


SWEEP_GRACE = timedelta(days=1)


async def sweep(*, now: datetime | None = None) -> dict:
    """Delete public pictures no row names any more: what a deleted team or person left behind, and
    any file a racing change missed. A file younger than SWEEP_GRACE is left alone, since an approval
    publishes its pictures before the row that names them is saved. Only `img/` is read: the public
    bucket is shared. Reads the rows, closes the session, then lists and deletes (rule 3)."""
    st = store()
    if st is None:
        return {"checked": 0, "deleted": 0}
    used = await _used_keys()
    keep = {k for key in used if (parsed := parse_key(key)) for k in keys_for(*parsed)}
    cutoff = (now or datetime.now(timezone.utc)) - SWEEP_GRACE
    listed = await st.listing("public", PREFIX)
    deleted = 0
    for key, modified in listed:
        if key in keep or not is_stored_key(key) or modified > cutoff:
            continue
        await st.delete("public", key)
        deleted += 1
    log.info("image sweep checked=%d deleted=%d", len(listed), deleted)
    return {"checked": len(listed), "deleted": deleted}


async def _still_used(key: str) -> bool:
    """Whether any row names `key`: the same rule as the sweep (`_used_keys`)."""
    async with session_maker() as db:
        for column in (User.avatar_key, Org.avatar_key, ShowcaseApp.icon_key):
            if (await db.execute(select(func.count()).where(column == key))).scalar_one():
                return True
        for shots, pending in (await db.execute(select(ShowcaseApp.shot_keys, ShowcaseApp.pending))).all():
            if key in (shots or []) or (pending and (key == pending.get("icon_key")
                                                     or key in (pending.get("shot_keys") or []))):
                return True
    return False


async def retire(keys: list[str | None]) -> None:
    """Delete pictures no row points at any more from the public bucket. Best effort: a failure
    leaves an orphan file, never a broken page."""
    st = store()
    if st is None:
        return
    for key in {k for k in keys if k}:
        parsed = parse_key(key)
        if parsed is None or await _still_used(key):
            continue
        for file_key in keys_for(*parsed):
            try:
                await st.delete("public", file_key)
            except stores.MediaStoreError:
                log.warning("image retire failed kind=%s", parsed[0].name)


def _expect_kind(key: str | None, kind_name: str) -> None:
    if key is None:
        return
    parsed = parse_key(key)
    if parsed is None or parsed[0].name != kind_name:
        raise ImageRejected("bad_key", "unknown picture")


async def set_user_avatar(user_id: int, key: str | None) -> str | None:
    """Set (or clear, with None) a person's own picture. Publishes first, then points the row."""
    _expect_kind(key, "user_avatar")
    if key is not None:
        await publish(key)
    async with session_maker() as db:
        user = await db.get(User, user_id)
        old, user.avatar_key = user.avatar_key, key
        await db.commit()
    await retire([old] if old != key else [])
    return public_url(key)


async def set_team_avatar(org_id: int, key: str | None) -> str | None:
    """Set (or clear) a team's picture. The caller has already checked owner/admin."""
    _expect_kind(key, "team_avatar")
    if key is not None:
        await publish(key)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        old, org.avatar_key = org.avatar_key, key
        await db.commit()
    await retire([old] if old != key else [])
    return public_url(key)


async def read(bucket: stores.Bucket, key: str) -> bytes | None:
    """The bytes of one stored file, for treg's own serving routes."""
    if not is_stored_key(key):
        return None
    return await _require_store().get(bucket, key)
