"""Host a reference file so a vendor can fetch it: the one use case behind `treg host`.

AIGC endpoints (reAPI, PiAPI, OpenRouter video) take reference images, voice clips and videos as
public URLs their fetcher downloads. An agent on a laptop has no host, and every paste host we tried
failed a vendor probe at least once (catbox unreachable from fal, tmpfiles answering HTML, uguu
timing out). This keeps the bytes in treg and serves them from an opaque token.

Deliberately not metered: hosting is a courtesy like polling, so money's five entries stay untouched.
Bounded instead: a size cap per file, a daily byte quota per org, a TTL, and a media-only type list.

Bytes go to the archive's bucket under a random `hosted-media/v1/<hex>` key when `media_body_write`
is "r2", else into the row. No database connection is held while the bucket is read or written
(AGENTS.md rule 3): each database step opens and closes its own session around the storage I/O.
"""
import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import timedelta
import logging
import secrets

from sqlalchemy import delete, func, select

from ..config import get_settings
from ..infra import object_store
from ..infra.db import session_maker
from ..models import Media, _now

log = logging.getLogger("treg.media")

MAX_BYTES = 30 * 1024 * 1024          # matches the vendors' per-reference limits
DAILY_ORG_BYTES = 300 * 1024 * 1024   # ten maximal files a day; a runaway agent stops there
TTL = timedelta(days=7)               # reAPI's own output URLs live about this long
ALLOWED_PREFIXES = ("image/", "audio/", "video/")
# SVG is an image type that carries script. Served inline on the treg.to origin it would run with a
# logged-in viewer's session, and no vendor takes an SVG reference anyway.
DENIED_TYPES = frozenset({"image/svg+xml", "image/svg"})


KEY_PREFIX = "hosted-media/v1/"
SWEEP_BATCH = 100        # expired rows removed per upload; each upload adds one
DELETE_QUEUE_MAX = 1000  # swept objects waiting for the background deleter
DELETE_RUN_S = 60.0      # one deleter run's budget
_QUOTA_LOCK = 0x6D656469  # pg_advisory_xact_lock namespace: "medi"

_delete_queue: deque[str] = deque()
_deleter: asyncio.Task | None = None

_store_override: object_store.StreamedObjectStore | None = None
_store_cache: tuple[tuple, object_store.StreamedObjectStore | None] | None = None


class MediaRejected(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class MediaUnavailable(Exception):
    """The object store could not serve a body that should be there."""


def use_store(store: object_store.StreamedObjectStore | None) -> None:
    """Tests and local tools: serve every call from `store` (None restores the configured one)."""
    global _store_override
    _store_override = store


def store() -> object_store.StreamedObjectStore | None:
    """The bucket bodies are read from and written to, or None while it is not configured. Opened
    whenever it is configured, whatever `media_body_write` says, so rows written before a switch
    back to "db" are still served."""
    global _store_cache
    if _store_override is not None:
        return _store_override
    s = get_settings()
    sig = (s.archive_object_store_endpoint, s.archive_object_store_bucket,
           s.archive_object_store_access_key_id, s.media_r2_timeout_s)
    if _store_cache is None or _store_cache[0] != sig:
        opened = (object_store.build_r2(s, max_body_bytes=MAX_BYTES, timeout_s=s.media_r2_timeout_s)
                  if object_store.configured(s) else None)
        _store_cache = (sig, opened)
    return _store_cache[1]


def validate_configuration() -> None:
    """Startup check: writing bodies to a bucket that is not configured fails loud, not per upload."""
    if get_settings().media_body_write == "r2" and store() is None:
        raise RuntimeError("media_body_write is r2 but the archive object store is not configured")


async def put(*, org_id: int, body: bytes, content_type: str) -> Media:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if not ctype.startswith(ALLOWED_PREFIXES) or ctype in DENIED_TYPES:
        raise MediaRejected(415, f"only image/*, audio/* and video/* (not SVG) can be hosted, not {ctype or 'an untyped body'!r}")
    if not body:
        raise MediaRejected(400, "empty body")
    if len(body) > MAX_BYTES:
        raise MediaRejected(413, f"file is {len(body)} bytes; the cap is {MAX_BYTES}")
    to_store = get_settings().media_body_write == "r2"
    st = store() if to_store else None
    if to_store and st is None:
        raise MediaRejected(503, "media storage is not configured")
    now = _now()
    swept = await _sweep(now)
    if to_store:
        _queue_deletes(swept)
    key = KEY_PREFIX + secrets.token_hex(32) if to_store else None
    row = Media(token=secrets.token_urlsafe(24), org_id=org_id, content_type=ctype, size=len(body),
                body=None if key else body, object_key=key, created_at=now, expires_at=now + TTL)
    async with session_maker() as db:
        if db.get_bind().dialect.name == "postgresql":
            # One upload per team at a time between the insert and the quota sum, so concurrent
            # uploads cannot all pass the check. SQLite serializes the insert's write lock instead.
            await db.execute(select(func.pg_advisory_xact_lock(_QUOTA_LOCK, org_id)))
        db.add(row)
        await db.flush()
        used = (await db.execute(select(func.coalesce(func.sum(Media.size), 0)).where(
            Media.org_id == org_id, Media.created_at >= now - timedelta(days=1)))).scalar_one()
        if used > DAILY_ORG_BYTES:
            await db.rollback()
            raise MediaRejected(429, f"this team has hosted {used - len(body)} bytes in the last 24 h; "
                                     f"the daily quota is {DAILY_ORG_BYTES}")
        await db.commit()
    if key is None:
        return row  # every field the router answers with was set above; no re-read of the body
    # The row exists before its object does, but its token is returned only after the PUT: no URL
    # can reach the row in between. A failed PUT removes the row and leaves any partial object to
    # the bucket's lifecycle rule.
    try:
        await st.put_named(key, body)
    except BaseException as exc:
        async with session_maker() as db:
            # By token too: SQLite may reuse the id if the team was deleted during the PUT.
            await db.execute(delete(Media).where(Media.id == row.id, Media.token == row.token))
            await db.commit()
        if isinstance(exc, object_store.ObjectStoreError):
            log.warning("media upload failed reason=%s", exc.reason)
            raise MediaRejected(503, "media storage is unavailable; try again") from None
        raise
    return row


async def _sweep(now) -> list[str]:
    """Delete one bounded batch of expired rows in its own transaction; returns their object keys."""
    async with session_maker() as db:
        expired = (select(Media.id).where(Media.expires_at < now).order_by(Media.expires_at)
                   .limit(SWEEP_BATCH).with_for_update(skip_locked=True).cte("expired_media")
                   .prefix_with("MATERIALIZED", dialect="postgresql"))
        swept = (await db.execute(delete(Media).where(Media.id.in_(select(expired.c.id)))
                                  .returning(Media.object_key)
                                  .execution_options(synchronize_session=False))).scalars().all()
        await db.commit()
    return [key for key in swept if key]


def _queue_deletes(keys: list[str]) -> None:
    """Hand swept objects to one background deleter, off the request path. Bounded by queue length
    and by time per run; what does not fit is left to the bucket's lifecycle rule on KEY_PREFIX."""
    global _deleter
    room = DELETE_QUEUE_MAX - len(_delete_queue)
    _delete_queue.extend(keys[:max(room, 0)])
    if not _delete_queue or (_deleter is not None and not _deleter.done()):
        return
    _deleter = asyncio.get_running_loop().create_task(_run_deletes())


async def _run_deletes() -> None:
    st = store()
    try:
        async with asyncio.timeout(DELETE_RUN_S):
            while _delete_queue and st is not None:
                key = _delete_queue.popleft()
                try:
                    await st.delete_named(key)
                except object_store.ObjectStoreError as exc:
                    log.warning("media object delete failed reason=%s", exc.reason)
    except TimeoutError:
        log.warning("media object deletes left to the lifecycle rule count=%d", len(_delete_queue))
    _delete_queue.clear()


async def drain() -> None:
    """Tests: wait for the background deleter."""
    if _deleter is not None:
        await asyncio.gather(_deleter, return_exceptions=True)


async def get(token: str) -> Media | None:
    async with session_maker() as db:
        row = (await db.execute(select(Media).where(Media.token == token))).scalar_one_or_none()
        if row is None or row.expires_at < _now():
            return None
        return row


async def open_body(row: Media) -> AsyncIterator[bytes] | None:
    """The stored bytes of a row whose body lives in the object store, as a stream, or None when
    the object is gone. Call with no session open."""
    st = store()
    if st is None:
        raise MediaUnavailable("media storage is not configured")
    try:
        opened = await st.open_named(row.object_key)
    except object_store.ObjectStoreError as exc:
        log.warning("media read failed reason=%s", exc.reason)
        raise MediaUnavailable(exc.reason) from None
    if opened is None:
        return None
    size, chunks = opened
    if size != row.size:
        await chunks.aclose()
        log.warning("media read failed reason=size_mismatch")
        raise MediaUnavailable("size_mismatch")
    return chunks
