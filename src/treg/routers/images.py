"""Picture uploads and the files treg serves itself.

- POST /images/{kind}            raw image body -> staged picture (key + preview URL)
- GET  /images/staging/{key}     a staged picture, for the signed-in person previewing it
- GET  /media-files/{key}        the public bucket, while no media domain fronts it
- PUT  /me/avatar                set or clear your own picture
- PUT  /orgs/{org_id}/avatar     set or clear a team's picture (owner or admin)

Bytes served from treg's own origin are inert (`nosniff`, `sandbox`), like /m/ media.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import images
from ..domain.identity.access import require_identity
from ..domain.images import KINDS, ImageRejected
from ..infra.db import get_session
from ..infra.media_store import CACHE_FOREVER, MediaStoreError
from ..models import Membership, User
from .auth_helpers import _same_origin

router = APIRouter()

MAX_BODY = max(k.max_bytes for k in KINDS.values())
_REJECT_STATUS = {"too_large": 413, "rate_limited": 429, "unknown_kind": 404, "bad_key": 400,
                  "missing": 409}
_INERT = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}


def _rejected(exc: ImageRejected) -> HTTPException:
    return HTTPException(status_code=_REJECT_STATUS.get(exc.reason, 422), detail=exc.detail)


def _off() -> HTTPException:
    return HTTPException(status_code=503, detail="picture uploads are not enabled on this registry")


def may_upload(user: User) -> bool:
    return bool(user.email_verified_at) and not user.demo and not user.suspended


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="the file is too large")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="the file is too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/images/{kind}", include_in_schema=False)
async def upload_image(kind: str, request: Request, user: User = Depends(require_identity),
                       db: AsyncSession = Depends(get_session)):
    await db.commit()  # release the request's connection: no session is held during storage I/O
    if not _same_origin(request):   # a cross-site form could otherwise spend someone's upload budget
        raise HTTPException(status_code=403, detail="cross-origin request rejected")
    if not may_upload(user):
        raise HTTPException(status_code=403, detail="verify your email to upload pictures")
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="unknown picture kind")
    data = await _body(request, KINDS[kind].max_bytes)
    try:
        staged = await images.upload(kind, data, user_id=user.id)
    except images.ImagesOff:
        raise _off() from None
    except ImageRejected as exc:
        raise _rejected(exc) from None
    except MediaStoreError:
        raise HTTPException(status_code=502, detail="the picture could not be stored - try again") from None
    return JSONResponse(staged, headers={"Cache-Control": "no-store"})


async def _serve(bucket: str, key: str, cache_control: str) -> Response:
    try:
        body = await images.read(bucket, key)
    except images.ImagesOff:
        raise HTTPException(status_code=404, detail="not found") from None
    except MediaStoreError:
        raise HTTPException(status_code=502, detail="the picture could not be read") from None
    if body is None:
        raise HTTPException(status_code=404, detail="not found")
    media_type = "image/png" if key.endswith(".png") else "image/webp"
    return Response(body, media_type=media_type, headers={"Cache-Control": cache_control, **_INERT})


@router.get("/images/staging/{key:path}", include_in_schema=False)
async def staged_image(key: str, user: User = Depends(require_identity),
                       db: AsyncSession = Depends(get_session)):
    """A staged (unreviewed) picture, never cached by shared caches. Its name is the fingerprint of
    the re-drawn picture, so only someone who has the picture can name it."""
    await db.commit()
    return await _serve("staging", key, "private, no-store")


@router.get("/media-files/{key:path}", include_in_schema=False)
async def public_image(key: str):
    return await _serve("public", key, CACHE_FOREVER)


class AvatarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None


@router.put("/me/avatar", include_in_schema=False)
async def set_my_avatar(body: AvatarIn, request: Request, user: User = Depends(require_identity),
                        db: AsyncSession = Depends(get_session)):
    await db.commit()
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")
    if not may_upload(user):
        raise HTTPException(status_code=403, detail="verify your email to set a picture")
    try:
        url = await images.set_user_avatar(user.id, body.key)
    except images.ImagesOff:
        raise _off() from None
    except ImageRejected as exc:
        raise _rejected(exc) from None
    except MediaStoreError:
        raise HTTPException(status_code=502, detail="the picture could not be stored - try again") from None
    return {"avatar_url": url}


@router.put("/orgs/{org_id}/avatar", include_in_schema=False)
async def set_team_avatar(org_id: int, body: AvatarIn, request: Request, user: User = Depends(require_identity),
                          db: AsyncSession = Depends(get_session)):
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")
    role = (await db.execute(select(Membership.role).where(
        Membership.org_id == org_id, Membership.user_id == user.id))).scalars().first()
    await db.commit()  # release the dependency's connection before the storage I/O
    if role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="owner or admin role in this team is required")
    try:
        url = await images.set_team_avatar(org_id, body.key)
    except images.ImagesOff:
        raise _off() from None
    except ImageRejected as exc:
        raise _rejected(exc) from None
    except MediaStoreError:
        raise HTTPException(status_code=502, detail="the picture could not be stored - try again") from None
    return {"avatar_url": url}
