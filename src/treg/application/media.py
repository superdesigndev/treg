"""Host a reference file so a vendor can fetch it: the one use case behind `treg host`.

AIGC endpoints (reAPI, PiAPI, OpenRouter video) take reference images, voice clips and videos as
public URLs their fetcher downloads. An agent on a laptop has no host, and every paste host we tried
failed a vendor probe at least once (catbox unreachable from fal, tmpfiles answering HTML, uguu
timing out). This keeps the bytes in treg and serves them from an opaque token.

Deliberately not metered: hosting is a courtesy like polling, so money's five entries stay untouched.
Bounded instead: a size cap per file, a daily byte quota per org, a TTL, and a media-only type list.
"""
from datetime import timedelta
import secrets

from sqlalchemy import delete, func, select

from ..infra.db import session_maker
from ..models import Media, _now

MAX_BYTES = 30 * 1024 * 1024          # matches the vendors' per-reference limits
DAILY_ORG_BYTES = 300 * 1024 * 1024   # ten maximal files a day; a runaway agent stops there
TTL = timedelta(days=7)               # reAPI's own output URLs live about this long
ALLOWED_PREFIXES = ("image/", "audio/", "video/")
# SVG is an image type that carries script. Served inline on the treg.to origin it would run with a
# logged-in viewer's session, and no vendor takes an SVG reference anyway.
DENIED_TYPES = frozenset({"image/svg+xml", "image/svg"})


class MediaRejected(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


async def put(*, org_id: int, body: bytes, content_type: str) -> Media:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if not ctype.startswith(ALLOWED_PREFIXES) or ctype in DENIED_TYPES:
        raise MediaRejected(415, f"only image/*, audio/* and video/* (not SVG) can be hosted, not {ctype or 'an untyped body'!r}")
    if not body:
        raise MediaRejected(400, "empty body")
    if len(body) > MAX_BYTES:
        raise MediaRejected(413, f"file is {len(body)} bytes; the cap is {MAX_BYTES}")
    now = _now()
    async with session_maker() as db:
        # ponytail: expiry is swept inline on every upload rather than by a cron; a dedicated
        # sweep only matters once uploads are rare and the table is not.
        await db.execute(delete(Media).where(Media.expires_at < now))
        # ponytail: read-then-insert, so two concurrent uploads can overshoot the quota by one file;
        # a row lock per org is the upgrade if the courtesy ever gets abused.
        used = (await db.execute(select(func.coalesce(func.sum(Media.size), 0)).where(
            Media.org_id == org_id, Media.created_at >= now - timedelta(days=1)))).scalar_one()
        if used + len(body) > DAILY_ORG_BYTES:
            raise MediaRejected(429, f"this team has hosted {used} bytes in the last 24 h; the daily quota is {DAILY_ORG_BYTES}")
        row = Media(token=secrets.token_urlsafe(24), org_id=org_id, content_type=ctype,
                    size=len(body), body=body, created_at=now, expires_at=now + TTL)
        db.add(row)
        await db.commit()
        return row  # every field the router answers with was set above; no re-read of the body


async def get(token: str) -> Media | None:
    async with session_maker() as db:
        row = (await db.execute(select(Media).where(Media.token == token))).scalar_one_or_none()
        if row is None or row.expires_at < _now():
            return None
        return row
