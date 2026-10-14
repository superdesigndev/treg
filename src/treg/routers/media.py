"""`POST /media` hosts a reference file for a vendor to fetch; `GET /m/{token}` serves it."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import sandbox as demo_sandbox
from ..application import media as media_app
from ..config import get_settings
from ..domain.identity.access import Caller, require_member

app = APIRouter()


@app.post("/media", status_code=201)
async def host_media(request: Request, caller: Caller = Depends(require_member)) -> dict:
    """Raw body in, public URL out. The Content-Type header names the media type; there is no
    multipart wrapper because the CLI and an agent's HTTP client both send bytes more simply."""
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="hosting is disabled in the sandbox")
    # Refuse before buffering: no body-size middleware exists, so a declared or streamed body past
    # the cap must never reach worker RAM in full.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > media_app.MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"file is {declared} bytes; the cap is {media_app.MAX_BYTES}")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > media_app.MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"file exceeds the {media_app.MAX_BYTES}-byte cap")
        chunks.append(chunk)
    try:
        row = await media_app.put(org_id=caller.org_id, body=b"".join(chunks),
                                  content_type=request.headers.get("content-type", ""))
    except media_app.MediaRejected as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from None
    url = f"{get_settings().public_url.rstrip('/')}/m/{row.token}"
    return {"url": url, "token": row.token, "content_type": row.content_type, "size": row.size,
            "expires_at": row.expires_at.isoformat() + "Z"}


@app.get("/m/{token}", include_in_schema=False)
async def serve_media(token: str) -> Response:
    """Public by design: the vendor's fetcher holds no treg token. The token is 192 random bits
    and the row names nothing about the org, so the URL is the only capability."""
    row = await media_app.get(token)
    if row is None:
        raise HTTPException(status_code=404, detail="no such media, or it expired")
    # `sandbox` CSP + nosniff: user bytes served on the treg.to origin must be inert even if a
    # scriptable type ever slips past the allow-list. Vendors' fetchers ignore both headers.
    return Response(content=row.body, media_type=row.content_type, headers={
        "Content-Length": str(row.size), "Cache-Control": "public, max-age=3600",
        "X-Robots-Tag": "noindex", "Content-Disposition": "inline",
        "Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})
