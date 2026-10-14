"""Web Arena pages and API, gated by the feature flag."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ..application import web_arena, web_arena_publications
from ..config import get_settings
from ..domain.web_arena import WebArenaError
from ..domain.identity.access import Caller, require_member
from .auth import _client_ip
from .auth_helpers import _same_origin

router = APIRouter()
_WEB = Path(__file__).parent.parent / "web"


def _enabled():
    if not web_arena.enabled():
        raise HTTPException(404)


def _guard(request):
    if not _same_origin(request):
        raise HTTPException(403, "Cross-origin Web Arena request rejected.")


async def _answer(awaitable):
    try:
        return JSONResponse(await awaitable, headers={"Cache-Control": "private, no-store"})
    except WebArenaError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except web_arena.CallFailure as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/web-arena", include_in_schema=False)
async def web_arena_page():
    _enabled()
    return FileResponse(_WEB / "web-arena.html", headers={"Cache-Control": "no-cache"})


@router.get("/web-arena/leaderboard", include_in_schema=False)
async def web_arena_old_leaderboard():
    _enabled()
    return RedirectResponse("/web-arena", status_code=308)


@router.get("/web-arena/{asset}", include_in_schema=False)
async def web_arena_asset(asset: str):
    _enabled()
    if asset not in {"arena.css", "arena.js"}:
        raise HTTPException(404)
    return FileResponse(_WEB / "web-arena" / asset, headers={"Cache-Control": "no-cache"})


@router.get("/web-arena/api/tasks", include_in_schema=False)
async def web_arena_tasks():
    _enabled()
    return web_arena.tasks()


@router.get("/web-arena/api/leaderboard", include_in_schema=False)
async def web_arena_leaderboard():
    _enabled()
    if get_settings().local_dev:
        return JSONResponse(await web_arena_publications.live_now(), headers={"Cache-Control": "no-store"})
    return JSONResponse(await web_arena_publications.published("live"), headers={"Cache-Control": "public, max-age=60"})


class QuoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(max_length=20)
    value: str = Field(max_length=500)
    query: str = Field(default="", max_length=500)
    mode: str = Field(default="battle", max_length=20)
    providers: list[str] | None = Field(default=None, max_length=30)
    jev: bool = True


@router.post("/web-arena/api/quotes", include_in_schema=False)
async def web_arena_quote(request: Request, body: QuoteIn, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(web_arena.quote(caller, **body.model_dump()))


@router.post("/web-arena/api/runs/{run_id}/start", include_in_schema=False)
async def web_arena_start(run_id: str, request: Request, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(web_arena.start(caller, run_id, request.app.state.http, _client_ip(request)))


@router.get("/web-arena/api/runs", include_in_schema=False)
async def web_arena_history(caller: Caller = Depends(require_member), limit: int = Query(default=30, ge=1, le=100)):
    return await _answer(web_arena.history(caller, limit))


@router.get("/web-arena/api/runs/{run_id}", include_in_schema=False)
async def web_arena_run(run_id: str, caller: Caller = Depends(require_member)):
    return await _answer(web_arena.get_run(caller, run_id))


@router.post("/web-arena/api/runs/{run_id}/cancel", include_in_schema=False)
async def web_arena_cancel(run_id: str, request: Request, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(web_arena.cancel(caller, run_id))


class RatingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=4)


@router.post("/web-arena/api/runs/{run_id}/attempts/{attempt_id}/rating", include_in_schema=False)
async def web_arena_rating(run_id: str, attempt_id: str, request: Request, body: RatingIn,
                           caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(web_arena.rate(caller, run_id, attempt_id, body.value))
