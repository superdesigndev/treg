"""A hub tool's web page, the maker's routes (docs/context/architecture/hub-apps.md).

Behind TREG_HUB_APPS_ENABLED and the hub's own flag and lists: off, or off for this caller, every
route here answers 404, exactly as if it did not exist. The maker's routes come first, then the
visitor's (`/apps/<team>/<name>`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import hub as hub_app
from ..application.hub import apps as hub_apps
from ..domain.hub import ManifestError
from ..domain.identity.access import Caller, _require_can_register, require_member
from ..config import get_settings
from ..infra.db import get_session
from .auth import _client_ip
from .auth_helpers import _is_https, _same_origin
from .call import _relay_answer, run_call_surface
from .web import page_entry

app = APIRouter()


def _require_apps(caller: Caller) -> None:
    if not hub_apps.enabled_for(caller.org.slug, caller.email):
        raise HTTPException(status_code=404, detail="Not Found")


def _invalid(exc: ManifestError) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": "app_invalid", "field": exc.field, "rule": exc.rule})


class AppIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The last part of `/apps/<team>/<name>`. Omitted: the tool's own name on first use, then unchanged.
    name: str | None = Field(default=None, max_length=64)


class PasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str | None = Field(max_length=256)   # null clears it


@app.get("/hub/tools/{tool_id}/app")
async def get_hub_app(tool_id: str, caller: Caller = Depends(require_member),
                      db: AsyncSession = Depends(get_session)) -> dict:
    """The app of one of your team's tools: on or off, its URL, whether a password is set."""
    _require_apps(caller)
    base, _ = hub_app.split_id(tool_id)
    return {"tool_id": base, **hub_apps.view(await hub_apps.of_tool(db, org_id=caller.org_id, tool_id=base), caller.org.slug)}


@app.put("/hub/tools/{tool_id}/app")
async def enable_hub_app(tool_id: str, body: AppIn, caller: Caller = Depends(require_member),
                         db: AsyncSession = Depends(get_session)) -> dict:
    """Turn the app on (made on first use), or rename it. The tool needs a live version."""
    _require_apps(caller)
    _require_can_register(caller)
    base, _ = hub_app.split_id(tool_id)
    try:
        row = await hub_apps.enable(db, org=caller.org, tool_id=base, name=body.name, maker_email=caller.email)
    except ManifestError as exc:
        raise _invalid(exc) from None
    if row is None:
        raise HTTPException(status_code=404, detail=f"your team has no live hub tool {base!r}")
    await db.commit()
    return {"tool_id": base, **hub_apps.view(row, caller.org.slug)}


@app.delete("/hub/tools/{tool_id}/app")
async def disable_hub_app(tool_id: str, caller: Caller = Depends(require_member),
                          db: AsyncSession = Depends(get_session)) -> dict:
    """Take the page down. The name and the password are kept for when it comes back; the lock on
    the tool is lifted while it is off."""
    _require_apps(caller)
    _require_can_register(caller)
    base, _ = hub_app.split_id(tool_id)
    row = await hub_apps.disable(db, org_id=caller.org_id, tool_id=base)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{base!r} has no app")
    await db.commit()
    return {"tool_id": base, **hub_apps.view(row, caller.org.slug)}


@app.put("/hub/tools/{tool_id}/app/password")
async def set_hub_app_password(tool_id: str, body: PasswordIn, caller: Caller = Depends(require_member),
                               db: AsyncSession = Depends(get_session)) -> dict:
    """Set the password (stored only as a hash) or clear it with null. While the app is on, it
    guards the page and the tool for every other team. Any change signs everyone out of the app."""
    _require_apps(caller)
    _require_can_register(caller)
    base, _ = hub_app.split_id(tool_id)
    try:
        row = await hub_apps.set_password(db, org_id=caller.org_id, tool_id=base, password=body.password)
    except ManifestError as exc:
        raise _invalid(exc) from None
    if row is None:
        raise HTTPException(status_code=404, detail=f"{base!r} has no app; turn it on first")
    await db.commit()
    return {"tool_id": base, **hub_apps.view(row, caller.org.slug)}


# ---------------------------------------------------------------------------------------------
# The visitor's side: `/apps/<team>/<name>`. The page is open; reading the contract of a locked
# app needs the unlock; running and reading runs need a signed-in member of some team, and the run
# is paid by that team through the one call road.

class UnlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=256)


async def _gate(request: Request, db: AsyncSession, caller: Caller | None = None) -> tuple:
    """(team slug, email) of the reader, or 404 when apps are off or the hub is off for them."""
    from .hub_gate import reader
    who = (caller.org.slug, caller.email) if caller is not None else await reader(request, db)
    if not get_settings().hub_apps_enabled or not hub_app.visible_to(*who):
        raise HTTPException(status_code=404, detail="Not Found")
    return who


async def _found(request: Request, team: str, name: str, db: AsyncSession, caller: Caller | None = None):
    who = await _gate(request, db, caller)
    found = await hub_apps.by_path(db, team, name, reader_slug=who[0], reader_email=who[1])
    if found is None:
        raise HTTPException(status_code=404, detail="no such app")
    return found


def _unlocked(request: Request, found, caller: Caller | None = None) -> bool:
    """Not locked, the maker's own team, or this browser typed the current password."""
    if not hub_apps.locked(found.app):
        return True
    if caller is not None and caller.org_id == found.app.org_id:
        return True
    return hub_apps.read_unlock(found.app, request.cookies.get(hub_apps.cookie_name(found.app)))


def _require_unlocked(request: Request, found, caller: Caller | None = None) -> None:
    if not _unlocked(request, found, caller):
        raise HTTPException(status_code=401, detail={"error": "app_locked", "message": "this app needs its password"})


def _require_same_origin(request: Request) -> None:
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


@app.get("/apps/{team}/{name}", include_in_schema=False)
async def app_page(team: str, name: str, request: Request, db: AsyncSession = Depends(get_session)):
    """The app page itself, `noindex`. A missing or turned-off app is a 404 here, before any script.
    A link from before a rename (the team's slug or the app's name) redirects to where it is now."""
    await _gate(request, db)
    target = await hub_apps.moved(db, team, name)
    if target:
        q = request.url.query
        return RedirectResponse(target + (f"?{q}" if q else ""), status_code=308)
    await _found(request, team, name, db)
    return page_entry("apps")


@app.get("/apps/{team}/{name}/contract")
async def app_contract(team: str, name: str, request: Request, db: AsyncSession = Depends(get_session)) -> dict:
    """What the page renders. Locked and not unlocked: only the name, the maker and `locked`."""
    found = await _found(request, team, name, db)
    caller = await _optional_member(request, db)
    if not _unlocked(request, found, caller):
        return {"locked": True, "name": found.tool.name, "maker": found.team_slug, "app": found.app.name}
    return {"locked": False, **await hub_apps.contract(db, found)}


@app.post("/apps/{team}/{name}/unlock")
async def app_unlock(team: str, name: str, body: UnlockIn, request: Request,
                     db: AsyncSession = Depends(get_session)) -> Response:
    """Check the password; on success a cookie scoped to this app's path remembers it for a few hours."""
    _require_same_origin(request)
    found = await _found(request, team, name, db)
    if not hub_apps.locked(found.app):
        return JSONResponse({"unlocked": True})
    await db.commit()   # no connection held while the hash runs
    verdict = await hub_apps.try_password(found.app, body.password, _client_ip(request))
    if verdict == "busy":
        raise HTTPException(status_code=429, detail={"error": "app_password_busy",
                                                     "message": "too many tries; wait a few minutes"})
    if verdict != "ok":
        raise HTTPException(status_code=401, detail={"error": "app_password_wrong", "message": "wrong password"})
    resp = JSONResponse({"unlocked": True})
    resp.set_cookie(hub_apps.cookie_name(found.app), hub_apps.make_unlock(found.app), max_age=hub_apps.UNLOCK_TTL_S,
                    path=f"/apps/{team}/{name}", httponly=True, samesite="lax", secure=_is_https(request))
    return resp


@app.post("/apps/{team}/{name}/run")
async def app_run(team: str, name: str, request: Request, caller: Caller = Depends(require_member),
                  db: AsyncSession = Depends(get_session)) -> Response:
    """Run the tool as the signed-in visitor's team: the same `/call/<tool_id>` road, the same gates,
    holds, ceiling and price, the same reply. No cookie travels further than this route."""
    _require_same_origin(request)
    found = await _found(request, team, name, db, caller)
    _require_unlocked(request, found, caller)
    tool_id = found.tool.tool_id
    await db.commit()
    headers = [(k, v) for k, v in request.headers.raw if k.lower() not in (b"cookie", b"x-treg-tool-password")]
    return await run_call_surface(tool_id, request, caller, prefix="/call/", finish=_relay_answer,
                                  headers=headers, hub_unlocked=True)


@app.get("/apps/{team}/{name}/runs")
async def app_runs(team: str, name: str, request: Request, caller: Caller = Depends(require_member),
                   db: AsyncSession = Depends(get_session)) -> dict:
    """Your own runs of this app's tool (your team and your sign-in), newest first, 30 days."""
    found = await _found(request, team, name, db, caller)
    _require_unlocked(request, found, caller)
    return {"runs": await hub_apps.runs_of(db, tool_id=found.tool.tool_id, org_id=caller.org_id, email=caller.email)}


@app.get("/apps/{team}/{name}/runs/{run_id}")
async def app_run_one(team: str, name: str, run_id: str, request: Request, caller: Caller = Depends(require_member),
                      db: AsyncSession = Depends(get_session)) -> dict:
    found = await _found(request, team, name, db, caller)
    _require_unlocked(request, found, caller)
    run = await hub_apps.run_of(db, tool_id=found.tool.tool_id, run_id=run_id, org_id=caller.org_id, email=caller.email)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    return run


async def _optional_member(request: Request, db: AsyncSession) -> Caller | None:
    """The signed-in member behind this request, or None: the contract is readable signed out."""
    from ..domain.identity.access import require_member as _member
    cookie = request.cookies.get("treg_session", "")
    token = request.headers.get("x-treg-token", "")
    if not cookie and not token:
        return None
    try:
        return await _member(request, x_treg_token=token, x_treg_org=request.headers.get("x-treg-org", ""),
                             treg_session=cookie, db=db)
    except HTTPException:
        return None
