"""Who is reading, for the hub's limits (`TREG_HUB_TEAMS`, `TREG_HUB_USERS`).

The catalog routes, the share pages and the agent files are open: they need no sign-in. While the
hub is limited to some teams or people, they must still show it to those and hide it from everyone
else, so they ask here who reads: the token or the dashboard session, when one is sent.
Nothing is refused here; an unknown reader is simply a reader with no team.
"""
from __future__ import annotations

import hashlib
import time

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import hub as hub_app
from ..config import get_settings


# Who a credential is, remembered briefly: while a list limits the hub, every open request that
# carries a key asks, and each answer costs 3 to 5 database reads (review after the deploy,
# 2026-09-26: catalog search had cost none). Only visibility rides on it, never access: a revoked
# key still fails every route that checks it.
_READER_TTL_S = 60.0
_READER_MAX = 2048
_readers: dict[str, tuple[float, tuple[str | None, str | None]]] = {}


def _reader_key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def remembered(key: str) -> tuple[str | None, str | None] | None:
    hit = _readers.get(key)
    if hit is None or hit[0] < time.monotonic():
        return None
    return hit[1]


def remember(key: str, who: tuple[str | None, str | None]) -> None:
    if len(_readers) >= _READER_MAX:
        now = time.monotonic()
        for k in [k for k, (exp, _) in _readers.items() if exp < now] or list(_readers)[: _READER_MAX // 4]:
            _readers.pop(k, None)
    _readers[key] = (time.monotonic() + _READER_TTL_S, who)


async def reader(request: Request, db: AsyncSession) -> tuple[str | None, str | None]:
    """(team slug, sign-in email) of the request's reader, or (None, None). Resolved only while a
    list limits the hub: with no list the answer never changes what the reader sees."""
    s = get_settings()
    if not s.hub_enabled or not s.hub_limited:
        return None, None
    token = request.headers.get("x-treg-token", "")
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    cookie = request.cookies.get("treg_session", "")
    if not token and not cookie:
        return None, None
    org = request.headers.get("x-treg-org", "")
    key = _reader_key("http", token, cookie, org)
    if (hit := remembered(key)) is not None:
        return hit
    from ..domain.identity.access import require_member
    try:
        caller = await require_member(request, x_treg_token=token, x_treg_org=org, treg_session=cookie, db=db)
    except HTTPException:
        caller = None
    who = ((caller.org.slug if caller.org is not None else None), caller.email) if caller is not None else (None, None)
    remember(key, who)
    return who


async def hub_visible(request: Request, db: AsyncSession) -> tuple[bool, tuple[str | None, str | None]]:
    """(may this reader see the hub, (team slug, email))."""
    who = await reader(request, db)
    return hub_app.visible_to(*who), who
