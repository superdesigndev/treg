"""Organization-scoped provider-resource views."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..application import provider_resources as provider_resource_app
from ..caller_metadata import _client_of
from ..domain.identity.access import Caller, require_member
from .auth import _client_ip


router = APIRouter()


@router.get("/orgs/{org_id}/provider-resources")
async def list_provider_resources(
    org_id: int,
    request: Request,
    response: Response,
    provider: str = Query(default=""),
    kind: str = Query(default=""),
    include_deleted: bool = Query(default=False),
    source: Literal["auto", "platform"] = Query(default="auto"),
    caller: Caller = Depends(require_member),
) -> list[dict]:
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="use this team's credential")
    try:
        result = await provider_resource_app.list_for_caller(
            caller,
            provider=provider,
            kind=kind,
            include_deleted=include_deleted,
            source=source,
            upstream_client=request.app.state.http,
            client_ip=_client_ip(request),
            client_name=_client_of(request),
        )
    except provider_resource_app.ResourceListFailed as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    response.headers["X-Treg-Resource-Source"] = result.source
    return result.resources
