"""Native dashboard Lens endpoints; application code owns authorization and persistence."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..application import lens
from ..application.call.types import CallFailure
from ..client_ip import of as client_ip
from ..config import get_settings
from ..domain.identity.access import Caller, require_member
from ..domain.lens import LensError
from .auth_helpers import _same_origin


def _enabled():
    if not get_settings().lens_enabled:
        raise HTTPException(404, "Lens is not enabled.")


router = APIRouter(dependencies=[Depends(_enabled)])


class CompetitorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    domain: str | None = Field(default=None, max_length=253)


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brand: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)
    description: str = Field(default="", max_length=1200)
    competitors: list[CompetitorIn] = Field(default_factory=list, max_length=8)
    questions: list[str] = Field(default_factory=list, max_length=8)
    engines: list[str] = Field(default_factory=lambda: ["chatgpt", "perplexity", "gemini", "google"],
                               min_length=1, max_length=4)


class CreateProjectIn(ProfileIn):
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class UpdateProjectIn(ProfileIn):
    revision: int = Field(ge=1, strict=True)


class QuoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["onboarding", "audit"]
    url: str | None = Field(default=None, max_length=2048)
    project_id: str | None = Field(default=None, max_length=80)
    project_revision: int | None = Field(default=None, ge=1, strict=True)
    engines: list[str] | None = Field(default=None, min_length=1, max_length=4)


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(min_length=1, max_length=80)


def _guard(request):
    if not _same_origin(request):
        raise HTTPException(403, "Cross-origin Lens request rejected.")


async def _answer(awaitable):
    try:
        return JSONResponse(await awaitable, headers={"Cache-Control": "private, no-store"})
    except LensError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except CallFailure as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/lens/projects")
async def lens_projects(caller: Caller = Depends(require_member)):
    return await _answer(lens.list_projects(caller))


@router.post("/lens/projects")
async def lens_create_project(request: Request, body: CreateProjectIn, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(lens.create_project(caller, body.model_dump(exclude={"request_id"}), body.request_id))


@router.get("/lens/projects/{project_id}")
async def lens_project(project_id: str, caller: Caller = Depends(require_member)):
    return await _answer(lens.get_project(caller, project_id))


@router.put("/lens/projects/{project_id}")
async def lens_update_project(project_id: str, request: Request, body: UpdateProjectIn,
                              caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(lens.update_project(caller, project_id,
                                            body.model_dump(exclude={"revision"}), body.revision))


@router.post("/lens/quotes")
async def lens_quote(request: Request, body: QuoteIn, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(lens.quote(caller, **body.model_dump()))


@router.post("/lens/runs")
async def lens_start(request: Request, body: StartIn, caller: Caller = Depends(require_member)):
    _guard(request)
    return await _answer(lens.start(caller, body.quote_id, request.app.state.http, client_ip(request)))


@router.get("/lens/runs/{run_id}")
async def lens_run(run_id: str, caller: Caller = Depends(require_member)):
    return await _answer(lens.get_run(caller, run_id))
