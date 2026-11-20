"""The /showcase leaderboard API: the public board and votes, makers' applications, staff review.

Hosted only, like the page: the leaderboard lists apps on treg.to. Votes are browser actions on the
session cookie behind the same-origin guard. Makers act as themselves (session or identity token).
Staff routes live under /admin/ behind `require_superadmin`; the private admin calls them with the
env admin token and names its signed-in operator in `actor`, which only that token may do.
"""
import hashlib
import json
import re

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import images, showcase
from ..domain.identity.access import _user_from_session, require_identity, require_superadmin
from ..domain.images import KINDS, ImageRejected
from ..infra.db import get_admin_session, get_session
from ..infra.media_store import MediaStoreError
from ..models import Org, User
from .auth import _client_ip
from .auth_helpers import _same_origin
from .images import _body, _off, _rejected, may_upload
from .web import _hosted

router = APIRouter()

_STATUS = {"invalid": 422, "not_allowed": 403, "forbidden": 403, "not_found": 404,
           "rate_limited": 429, "limit": 409, "conflict": 409}
_DEFAULT_DETAIL = {"not_allowed": "verify your email to vote",
                   "not_found": "no such app", "rate_limited": "too many requests - try again later"}
_ACTOR = re.compile(r"[^@\s]{1,64}@[^@\s]{1,120}")


def _only_hosted() -> None:
    if not _hosted():
        raise HTTPException(404, "not found")


def _fail(exc: showcase.ShowcaseError) -> HTTPException:
    return HTTPException(_STATUS.get(exc.kind, 400), exc.detail or _DEFAULT_DETAIL.get(exc.kind, exc.kind))


def _private(body) -> JSONResponse:
    return JSONResponse(body, headers={"Cache-Control": "private, no-store"})


async def _run(awaitable):
    try:
        return _private(await awaitable)
    except showcase.ShowcaseError as exc:
        raise _fail(exc) from None
    except images.ImagesOff:
        raise _off() from None
    except MediaStoreError:
        raise HTTPException(502, "a picture could not be stored - try again") from None


class Revision(BaseModel):
    """The fields of an application. Checked in `domain.showcase.clean_revision`."""
    model_config = ConfigDict(extra="forbid")
    name: str = ""
    url: str = ""
    tagline: str = ""
    description: str = ""
    category: str = ""
    maker_name: str = ""
    x_handle: str = ""
    launch_post_url: str = ""
    icon_key: str | None = None
    shot_keys: list[str] = Field(default_factory=list)
    show_calls: bool = False
    show_streak: bool = False
    show_providers: bool = False
    show_categories: bool = False


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int
    fields: Revision


class UsageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    key_ids: list[int] = Field(default_factory=list)


class VoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    up: bool


@router.get("/showcase/leaderboard", include_in_schema=False)
async def showcase_leaderboard(request: Request, db: AsyncSession = Depends(get_session)):
    """Approved apps with their vote tallies, consented activity tiers and badges, and the boards.
    Never a team, an exact count or money (`domain.showcase.public_entry`)."""
    _only_hosted()
    # Checked on every view (an approval or a vote shows at once); an unchanged board answers 304.
    body = json.dumps(await showcase.leaderboard(db), separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    sent = {t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")}
    if etag in sent:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@router.get("/showcase/votes/me", include_in_schema=False)
async def showcase_my_votes(treg_session: str = Cookie(default=""), db: AsyncSession = Depends(get_session)):
    """The signed-in visitor's own votes, and whether they may vote and apply."""
    _only_hosted()
    user = await _user_from_session(treg_session, db) if treg_session else None
    if user is None:
        return _private({"signed_in": False, "can_vote": False, "upvoted": []})
    return _private({"signed_in": True, "can_vote": showcase.may_vote(user), "upvoted": await showcase.my_upvotes(db, user)})


@router.post("/showcase/vote", include_in_schema=False)
async def showcase_vote(body: VoteIn, request: Request, treg_session: str = Cookie(default=""),
                        db: AsyncSession = Depends(get_session)):
    """Upvote one app (`up: true`) or take the signed-in visitor's upvote back (`up: false`)."""
    _only_hosted()
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin vote rejected")
    user = await _user_from_session(treg_session, db) if treg_session else None
    if user is None:
        raise HTTPException(401, "sign in to vote")
    try:
        return _private(await showcase.vote(db, user, body.id, body.up, _client_ip(request)))
    except showcase.ShowcaseError as exc:
        raise _fail(exc) from None


async def _maker(user: User = Depends(require_identity), db: AsyncSession = Depends(get_session)) -> User:
    await db.commit()  # each use case opens its own sessions; hold no connection meanwhile
    _only_hosted()
    if not may_upload(user):
        raise HTTPException(403, "verify your email to list an app")
    return user


@router.get("/showcase/my-teams", include_in_schema=False)
async def showcase_my_teams(user: User = Depends(_maker)):
    return await _run(showcase.my_teams(user))


@router.get("/showcase/teams/{org_id}/apps", include_in_schema=False)
async def showcase_team_apps(org_id: int, user: User = Depends(_maker)):
    return await _run(showcase.team_apps(user, org_id))


@router.post("/showcase/teams/{org_id}/apps", include_in_schema=False)
async def showcase_apply(org_id: int, body: Revision, request: Request, user: User = Depends(_maker)):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    return await _run(showcase.apply(user, org_id, body.model_dump()))


@router.patch("/showcase/apps/{app_id}", include_in_schema=False)
async def showcase_edit(app_id: int, body: EditIn, request: Request, user: User = Depends(_maker)):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    return await _run(showcase.edit(user, app_id, body.fields.model_dump(), body.expected_version))


@router.delete("/showcase/apps/{app_id}", include_in_schema=False)
async def showcase_withdraw(app_id: int, request: Request, user: User = Depends(_maker)):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    try:
        await showcase.withdraw(user, app_id)
    except showcase.ShowcaseError as exc:
        raise _fail(exc) from None
    return _private({"withdrawn": app_id})


@router.get("/showcase/teams/{org_id}/keys", include_in_schema=False)
async def showcase_team_keys(org_id: int, user: User = Depends(_maker)):
    """The team's API keys an owner or admin may link to an app (names and prefixes only)."""
    return await _run(showcase.team_keys(user, org_id))


@router.put("/showcase/apps/{app_id}/usage", include_in_schema=False)
async def showcase_set_usage(app_id: int, body: UsageIn, request: Request, user: User = Depends(_maker)):
    """Where the app's activity comes from: the whole team, or some of its API keys. Not reviewed."""
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    return await _run(showcase.set_usage(user, app_id, body.source, body.key_ids))


@router.post("/showcase/apps/{app_id}/remove", include_in_schema=False)
async def showcase_remove(app_id: int, request: Request, user: User = Depends(_maker)):
    """The team takes its live app off the showcase."""
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin request rejected")
    return await _run(showcase.remove(user, app_id))


def _actor(principal: str, named: str | None) -> str:
    """Who acted: a superadmin's own address, or the operator the admin service names."""
    if principal == "env-admin":
        if named and _ACTOR.fullmatch(named):
            return f"admin:{named}"
        return "admin"
    return principal


async def _staff(principal: str = Depends(require_superadmin)) -> str:
    _only_hosted()
    return principal


class StaffAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int
    actor: str | None = None


class RejectIn(StaffAction):
    reason: str


class HideIn(StaffAction):
    hidden: bool


class StaffEditIn(StaffAction):
    fields: Revision


class StaffCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: Revision
    approve: bool = False
    actor: str | None = None


@router.get("/admin/showcase/apps", include_in_schema=False)
async def staff_queue(status: str | None = None, principal: str = Depends(_staff)):
    return await _run(showcase.queue(status))


@router.get("/admin/showcase/apps/{app_id}", include_in_schema=False)
async def staff_app(app_id: int, principal: str = Depends(_staff)):
    return await _run(showcase.staff_get(app_id))


@router.post("/admin/showcase/apps/{app_id}/approve", include_in_schema=False)
async def staff_approve(app_id: int, body: StaffAction, principal: str = Depends(_staff)):
    return await _run(showcase.approve(app_id, body.expected_version, actor=_actor(principal, body.actor)))


@router.post("/admin/showcase/apps/{app_id}/reject", include_in_schema=False)
async def staff_reject(app_id: int, body: RejectIn, principal: str = Depends(_staff)):
    return await _run(showcase.reject(app_id, body.expected_version, body.reason, actor=_actor(principal, body.actor)))


@router.post("/admin/showcase/apps/{app_id}/hide", include_in_schema=False)
async def staff_hide(app_id: int, body: HideIn, principal: str = Depends(_staff)):
    return await _run(showcase.set_hidden(app_id, body.expected_version, body.hidden, actor=_actor(principal, body.actor)))


@router.patch("/admin/showcase/apps/{app_id}", include_in_schema=False)
async def staff_edit(app_id: int, body: StaffEditIn, principal: str = Depends(_staff)):
    return await _run(showcase.staff_edit(app_id, body.expected_version, body.fields.model_dump(),
                                          actor=_actor(principal, body.actor)))


@router.post("/admin/showcase/teams/{org_id}/apps", include_in_schema=False)
async def staff_create(org_id: int, body: StaffCreateIn, principal: str = Depends(_staff)):
    return await _run(showcase.staff_create(org_id, body.fields.model_dump(), approve_now=body.approve,
                                            actor=_actor(principal, body.actor)))


@router.get("/admin/showcase/teams/{org_id}", include_in_schema=False)
async def staff_team(org_id: int, principal: str = Depends(_staff), db: AsyncSession = Depends(get_admin_session)):
    org = await db.get(Org, org_id)
    await db.commit()
    if org is None:
        raise HTTPException(404, "no such team")
    return _private({"id": org.id, "slug": org.slug, "name": org.name})


@router.post("/admin/images/{kind}", include_in_schema=False)
async def staff_upload(kind: str, request: Request, principal: str = Depends(_staff)):
    """Staff upload a picture for an entry: the same check and re-draw as everyone's."""
    if kind not in ("showcase_icon", "showcase_shot"):
        raise HTTPException(404, "unknown picture kind")
    data = await _body(request, KINDS[kind].max_bytes)
    try:
        return _private(await images.upload(kind, data, user_id=0))
    except images.ImagesOff:
        raise _off() from None
    except ImageRejected as exc:
        raise _rejected(exc) from None
    except MediaStoreError:
        raise HTTPException(502, "the picture could not be stored - try again") from None


@router.get("/admin/images/staging/{key:path}", include_in_schema=False)
async def staff_staged_image(key: str, principal: str = Depends(_staff)):
    from .images import _serve
    return await _serve("staging", key, "private, no-store")
