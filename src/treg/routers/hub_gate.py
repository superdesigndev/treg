"""Who is reading, for the hub's limits (`TREG_HUB_TEAMS`, `TREG_HUB_USERS`).

The catalog routes, the share pages and the agent files are open: they need no sign-in. While the
hub is limited to some teams or people, they must still show it to those and hide it from everyone
else, so they ask here who reads: the token or the dashboard session, when one is sent.
Nothing is refused here; an unknown reader is simply a reader with no team.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import hub as hub_app
from ..config import get_settings


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
    from ..domain.identity.access import require_member
    try:
        caller = await require_member(request, x_treg_token=token, x_treg_org=request.headers.get("x-treg-org", ""),
                                      treg_session=cookie, db=db)
    except HTTPException:
        return None, None
    if caller is None:
        return None, None
    return (caller.org.slug if caller.org is not None else None), caller.email


async def hub_visible(request: Request, db: AsyncSession) -> tuple[bool, tuple[str | None, str | None]]:
    """(may this reader see the hub, (team slug, email))."""
    who = await reader(request, db)
    return hub_app.visible_to(*who), who
