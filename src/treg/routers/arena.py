"""Standalone Arena page and authenticated application adapters."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..application import arena, arena_insights
from ..domain.arena import ArenaError
from ..domain.identity.access import Caller, require_member
from .auth import _client_ip
from .auth_helpers import _same_origin

router = APIRouter()
_WEB = Path(__file__).parent.parent / "web"


@router.get("/enrich-arena/people-search-bench", include_in_schema=False)
@router.get("/enrich-arena/leaderboard", include_in_schema=False)
@router.get("/enrich-arena", include_in_schema=False)
async def enrich_arena_page():
    return FileResponse(_WEB / "enrich-arena.html", headers={"Cache-Control": "no-cache"})


@router.get("/enrich-arena/{asset}", include_in_schema=False)
async def enrich_arena_asset(asset: str):
    if asset not in {"arena.css", "arena.js", "bench.js"}:
        raise HTTPException(404)
    return FileResponse(_WEB / "enrich-arena" / asset, headers={"Cache-Control": "no-cache"})


@router.get("/arena/tasks", include_in_schema=False)
async def arena_tasks():
    return arena.public_tasks()


@router.get("/arena/insights", include_in_schema=False)
async def arena_insights_data():
    return JSONResponse(await arena_insights.public_snapshot(), headers={"Cache-Control": "no-store"})


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str = Field(max_length=80)
    identity: dict[str, str] | None = Field(default=None, max_length=8)
    identities: list[dict[str, str]] | None = Field(default=None, min_length=1, max_length=50)
    auto_verify: bool = False
    mode: str = Field(default="compare", max_length=20)
    providers: list[str] | None = Field(default=None, max_length=20)
    max_cost_micro: int = Field(default=1_000_000, ge=0, le=10_000_000, strict=True)


class VoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=30)
    selected: list[str] = Field(default_factory=list, max_length=20)
    reasons: list[str] = Field(default_factory=list, max_length=7)
    comment: str = Field(default="", max_length=1000)


def _guard(request):
    if not _same_origin(request):
        raise HTTPException(403, "Cross-origin Arena request rejected.")


async def _answer(awaitable):
    try:
        return JSONResponse(await awaitable, headers={"Cache-Control": "private, no-store"})
    except ArenaError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except arena.CallFailure as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.post("/arena/plans", include_in_schema=False)
async def arena_plan(request: Request, body: PlanIn, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.quote(caller, **body.model_dump()))


@router.post("/arena/runs/{run_id}/start", include_in_schema=False)
async def arena_start(run_id: str, request: Request, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.start(caller, run_id, request.app.state.http, _client_ip(request)))


@router.get("/arena/runs", include_in_schema=False)
async def arena_history(caller: Caller = Depends(require_member),
                        before: str | None = Query(default=None, max_length=32),
                        limit: int = Query(default=30, ge=1, le=100)):
    return await _answer(arena.history(caller, before=before, limit=limit))


@router.get("/arena/runs/{run_id}", include_in_schema=False)
async def arena_run(run_id: str, caller: Caller = Depends(require_member)):
    return await _answer(arena.get_run(caller, run_id))


@router.post("/arena/runs/{run_id}/cancel", include_in_schema=False)
async def arena_cancel(run_id: str, request: Request, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.cancel(caller, run_id))


@router.post("/arena/runs/{run_id}/evaluations", include_in_schema=False)
async def arena_evaluate(run_id: str, request: Request, body: VoteIn, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.evaluate(caller, run_id, **body.model_dump()))


@router.post("/arena/runs/{run_id}/reveal", include_in_schema=False)
async def arena_reveal(run_id: str, request: Request, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.evaluate(caller, run_id, kind="skip", selected=[], reasons=[], comment=""))


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=40)
    comment: str = Field(default="", max_length=1000)


class ManualStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(max_length=80)


class RatingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=4)


@router.post("/arena/runs/{run_id}/attempts/{attempt_id}/rating", include_in_schema=False)
async def arena_rate(run_id: str, attempt_id: str, request: Request, body: RatingIn,
                     caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.rate_result(caller, run_id, attempt_id, **body.model_dump()))


@router.post("/arena/runs/{run_id}/attempts/{attempt_id}/report", include_in_schema=False)
async def arena_report(run_id: str, attempt_id: str, request: Request, body: ReportIn,
                       caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.report_result(caller, run_id, attempt_id, **body.model_dump()))


@router.post("/arena/runs/{run_id}/attempts/{attempt_id}/plan", include_in_schema=False)
async def arena_manual_plan(run_id: str, attempt_id: str, request: Request,
                            caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.manual_plan(caller, run_id, attempt_id))


@router.post("/arena/runs/{run_id}/attempts/{attempt_id}/start", include_in_schema=False)
async def arena_manual_start(run_id: str, attempt_id: str, request: Request, body: ManualStartIn,
                             caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.start_manual(caller, run_id, attempt_id, body.quote_id,
                                          request.app.state.http, _client_ip(request)))


@router.post("/arena/runs/{run_id}/attempts/{attempt_id}/verification/plan", include_in_schema=False)
async def arena_verification_plan(run_id: str, attempt_id: str, request: Request, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.verification_plan(caller, run_id, attempt_id))


@router.post("/arena/runs/{run_id}/attempts/{attempt_id}/verification/start", include_in_schema=False)
async def arena_verification_start(run_id: str, attempt_id: str, request: Request, body: ManualStartIn, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(arena.start_verification(caller, run_id, attempt_id, body.quote_id, request.app.state.http, _client_ip(request)))
