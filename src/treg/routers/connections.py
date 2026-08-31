"""Credential connection HTTP routes and presentation translation."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import oauth_providers
from ..application import connect as connect_use_cases
from ..domain.identity.access import Caller, _require_can_register, require_member
from .auth import _auth_page
from .resources import _visible_secret_ids


class OAuthStartIn(BaseModel):
    """Two modes. BYO: supply client_id/client_secret/auth_uri/token_uri/scopes yourself.
    REGISTRY: supply `provider` (+ optional `capability`) and treg fills all of it from its own
    approved app — see oauth_providers.py."""

    name: str = ""  # the secret name to create on success; defaults to the provider service
    provider: str | None = None  # registry service id, e.g. "google-search-console"
    capability: str | None = None  # which scope set to request (default: read)
    # Reconnect/widen an EXISTING connection instead of adding another one. Omit to add.
    connection_id: int | None = None
    client_id: str = ""
    client_secret: str = ""
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    scopes: list[str] = []
    redirect_uri: str | None = None  # defaults to treg's public callback


class TokenConnectIn(BaseModel):
    provider: str
    token: str


class ResourceRefIn(BaseModel):
    resource_ref: str
    resource_name: str = ""  # the human label, so the UI never has to show "properties/384078430"


class ExtraCredentialIn(BaseModel):
    value: str


_CONNECT_HTTP_ERRORS = {
    "unknown_provider": 404,
    "unknown_connection": 404,
    "invalid_provider": 422,
    "invalid_token_provider": 422,
    "invalid_token": 422,
    "provider_unreachable": 502,
    "unknown_state": 404,
    "no_resource_discovery": 422,
    "resource_discovery_failed": 502,
    "no_extra_credential": 422,
    "extra_credential_required": 422,
    "all_orgs_forbidden": 403,
}


def _connect_http_error(exc: connect_use_cases.ConnectError) -> HTTPException:
    return HTTPException(status_code=_CONNECT_HTTP_ERRORS[exc.kind], detail=exc.detail)


oauth_router = APIRouter()


@oauth_router.get("/oauth/providers")
async def oauth_providers_list() -> list[dict]:
    """Providers treg holds its own approved app for. `configured` is false when this deployment
    hasn't set that provider's client credentials — listed, but its flow can't run here."""
    return oauth_providers.listing()


@oauth_router.post("/oauth/start")
async def oauth_start(
    body: OAuthStartIn, caller: Caller = Depends(require_member),
) -> dict:
    _require_can_register(caller)
    try:
        return await connect_use_cases.start_oauth_connection(
            name=body.name,
            provider_name=body.provider,
            capability=body.capability,
            connection_id=body.connection_id,
            client_id=body.client_id,
            client_secret=body.client_secret,
            auth_uri=body.auth_uri,
            token_uri=body.token_uri,
            scopes=list(body.scopes),
            redirect_uri=body.redirect_uri,
            org_id=caller.org_id,
            owner=caller.email,
        )
    except connect_use_cases.ConnectError as exc:
        raise _connect_http_error(exc) from exc


@oauth_router.get("/oauth/callback")
async def oauth_callback(
    request: Request, state: str = "", code: str = "", error: str = "",
):
    outcome = await connect_use_cases.complete_oauth_connection(
        state=state, code=code, error=error, client_factory=lambda: request.app.state.http,
    )
    if outcome.kind == "invalid":
        return _auth_page("Connect failed", "Invalid or expired authorization link.", ok=False, status=404)
    if outcome.kind == "done":
        return _auth_page("Connected", "You can close this tab.")
    if outcome.kind == "already_failed":
        return _auth_page("Connect failed", "This authorization already failed. Start the connect again.", ok=False, status=400)
    if outcome.kind == "expired":
        return _auth_page("Connect failed", "This authorization link expired. Start the connect again.", ok=False, status=400)
    if outcome.kind == "authorization_failed":
        return _auth_page("Connect failed", "Authorization failed. You can close this tab and try again.", ok=False, status=400)
    if outcome.kind == "exchange_failed":
        return _auth_page("Connect failed", "Token exchange failed. You can close this tab and try again.", ok=False, status=502)
    return _auth_page("Connected", "You can close this tab and return to the terminal.")


token_router = APIRouter()


@token_router.post("/connections/token")
async def connect_with_token(
    body: TokenConnectIn, request: Request,
    caller: Caller = Depends(require_member),
) -> dict:
    """Connect a provider the user brings a pasted credential for — a bot token (Slack) or an API
    key (Apollo, TikHub, Semrush, …).

    The credential is VERIFIED against the provider's probe before anything is stored. Saving an
    unverified credential just moves the failure to the first real call, by which point the user
    has left the setup screen and has no idea which of the steps they got wrong."""
    _require_can_register(caller)
    try:
        return await connect_use_cases.connect_with_pasted_secret(
            provider_name=body.provider,
            raw_token=body.token,
            org_id=caller.org_id,
            owner=caller.email,
            client_factory=lambda: request.app.state.http,
        )
    except connect_use_cases.ConnectError as exc:
        raise _connect_http_error(exc) from exc


status_router = APIRouter()


@status_router.get("/oauth/status/{state}")
async def oauth_status(
    state: str, caller: Caller = Depends(require_member),
) -> dict:
    try:
        return await connect_use_cases.get_oauth_status(state=state, org_id=caller.org_id)
    except connect_use_cases.ConnectError as exc:
        raise _connect_http_error(exc) from exc


resources_router = APIRouter()


@resources_router.get("/connections")
async def list_connections(
    caller: Caller = Depends(require_member),
) -> list[dict]:
    """Every OAuth credential in the org, with health AND expiry. Metadata only — no token material."""
    return await connect_use_cases.list_connections(org_id=caller.org_id)


@resources_router.get("/connections/{secret_id}/resources")
async def connection_resources(
    secret_id: int, request: Request,
    caller: Caller = Depends(require_member),
) -> dict:
    """What this connection can act on — GSC sites, and later GA properties / Ads accounts.

    Live-fetched rather than stored: the answer changes when the user gains or loses access
    upstream, and a stale picker is worse than no picker."""
    try:
        return await connect_use_cases.list_connection_resources(
            secret_id=secret_id,
            org_id=caller.org_id,
            client_factory=lambda: request.app.state.http,
        )
    except connect_use_cases.ConnectError as exc:
        raise _connect_http_error(exc) from exc


@resources_router.post("/connections/{secret_id}/resource")
async def set_connection_resource(
    secret_id: int, body: ResourceRefIn,
    caller: Caller = Depends(require_member),
) -> dict:
    try:
        return await connect_use_cases.select_connection_resource(
            secret_id=secret_id,
            resource_ref=body.resource_ref,
            resource_name=body.resource_name,
            org_id=caller.org_id,
        )
    except connect_use_cases.ConnectError as exc:
        raise _connect_http_error(exc) from exc


management_router = APIRouter()


@management_router.post("/connections/{secret_id}/extra-credential")
async def set_extra_credential(
    secret_id: int, body: ExtraCredentialIn,
    caller: Caller = Depends(require_member),
) -> dict:
    """Supply the second credential a provider needs, and finish building its tool.

    Google Ads takes the user's OAuth bearer AND a developer-token header from an approved manager
    account. treg can't invent the latter, so the connect deliberately stops short of a tool. This
    is the other half: store the extra credential and provision the tool with BOTH bindings, so the
    connection goes from "connected but uncallable" to actually usable."""
    _require_can_register(caller)
    try:
        return await connect_use_cases.supply_extra_credential(
            secret_id=secret_id,
            value=body.value,
            org_id=caller.org_id,
            owner=caller.email,
        )
    except connect_use_cases.ConnectError as exc:
        raise _connect_http_error(exc) from exc


@management_router.delete("/connections/{secret_id}")
async def revoke_connection(
    secret_id: int, caller: Caller = Depends(require_member),
) -> dict:
    """Disconnect, and take the provider's own tool with it.

    A tool left bound to a deleted credential isn't "still configured" — it's broken, and it says so
    only at call time with "a bound secret is missing". We remove the tool treg auto-provisioned for
    this provider (that's treg's creation, not the user's) and drop the dead binding from any tool
    the user built themselves, leaving their other bindings intact."""
    _require_can_register(caller)
    try:
        return await connect_use_cases.revoke_connection(
            secret_id=secret_id, org_id=caller.org_id,
        )
    except connect_use_cases.ConnectError as exc:
        raise _connect_http_error(exc) from exc


health_router = APIRouter()


@health_router.post("/health/run")
async def run_health(
    request: Request, all_orgs: bool = False,
    caller: Caller = Depends(require_member),
) -> dict:
    try:
        return await connect_use_cases.run_connection_health(
            all_orgs=all_orgs,
            is_superadmin=caller.user.is_superadmin,
            org_id=caller.org_id,
            client_factory=lambda: request.app.state.http,
        )
    except connect_use_cases.ConnectError as exc:
        raise _connect_http_error(exc) from exc


@health_router.get("/health")
async def get_health(
    caller: Caller = Depends(require_member),
) -> list[dict]:
    return await connect_use_cases.list_connection_health(
        org_id=caller.org_id,
        visible_ids_for=lambda db: _visible_secret_ids(caller, db),
    )
