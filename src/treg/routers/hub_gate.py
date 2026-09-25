"""Who is reading, for the hub's team limit (`TREG_HUB_TEAMS`).

The catalog routes, the share pages and the agent files are open: they need no sign-in. While the
hub is limited to some teams, they must still show it to those teams and hide it from everyone
else, so they ask here for the reader's team: the token or the dashboard session, when one is sent.
Nothing is refused here; an unknown reader is simply a reader with no team.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import hub as hub_app
from ..config import get_settings


async def reader_team(request: Request, db: AsyncSession) -> str | None:
    """The slug of the team the request acts for, or None. Resolved only while a team list is set:
    with no list the answer never changes what the reader sees."""
    s = get_settings()
    if not s.hub_enabled or not s.hub_team_set:
        return None
    token = request.headers.get("x-treg-token", "")
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    cookie = request.cookies.get("treg_session", "")
    if not token and not cookie:
        return None
    from ..domain.identity.access import require_member
    try:
        caller = await require_member(request, x_treg_token=token, x_treg_org=request.headers.get("x-treg-org", ""),
                                      treg_session=cookie, db=db)
    except HTTPException:
        return None
    return caller.org.slug if caller is not None and caller.org is not None else None


async def hub_visible(request: Request, db: AsyncSession) -> tuple[bool, str | None]:
    """(may this reader see the hub, the reader's team)."""
    slug = await reader_team(request, db)
    return hub_app.visible_to(slug), slug
