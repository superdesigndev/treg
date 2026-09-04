"""HTTP adapters for the proxied call surface."""

from __future__ import annotations

import json
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.background import BackgroundTask
from sqlalchemy.ext.asyncio import AsyncSession

from .. import analytics
from .. import audit
from .. import sandbox as demo_sandbox
from ..application.call.access import catalog_endpoint_access as get_catalog_endpoint_access
from ..application.call.idempotency import (
    _release_idempotent_claim as release_idempotent_claim,
)
from ..application.call.resolve import (
    QueryValues,
    _may_have_body as may_have_body,
)
from ..application.call.service import (
    _refusal_kind,
    _tool_called_props,
    create_call_context,
    execute_call,
)
from ..application.call.intake import (
    META_HEADER,
    CallMeta,
    _parse_call_meta as parse_call_meta,
)
from ..application.call.types import CallerSnapshot, CallFailure, CallInput, UpstreamResponse
from ..caller_metadata import _client_of
from ..call_surface import split_call_path
from ..config import get_settings
from ..domain.catalog import store as catalog_store
from ..domain.governance import access as access_policy
from ..domain.governance import publicdemo as publicdemo_policy
from ..domain.identity.access import Caller, require_member
from ..infra.db import get_session
from ..models import Tool
from .auth import _client_ip
from .orgs import count_today


# The app alias preserves the moved handlers' decorator text byte-for-byte.
app = APIRouter()
router = app


class _HttpRequestBody:
    def __init__(self, request: Request) -> None:
        self._request = request

    def stream(self):
        return self._request.stream()

    async def read(self) -> bytes:
        return await self._request.body()


def _http_upstream_response(upstream: UpstreamResponse) -> StreamingResponse:
    async def body_stream():
        completed = False
        try:
            async for chunk in upstream.body_stream:
                yield chunk
            completed = True
        finally:
            # A normal stream closes from Starlette's background task after the final ASGI body
            # frame. Error, disconnect, and cancellation never reach that frame, so close here.
            if not completed:
                await upstream.close()

    response = StreamingResponse(
        body_stream(),
        status_code=upstream.status,
        background=BackgroundTask(upstream.close),
    )
    response.raw_headers = list(upstream.raw_headers)
    return response


def _attach_async_descriptor(upstream: UpstreamResponse, context, rest: str = "") -> None:
    """Attach static catalog metadata before Starlette starts the upstream body stream.

    An idempotent replay returns before marketplace resolution, so the endpoint is taken from the
    path itself: a `--await` retry of a stored submission must still learn how to poll the task
    that is already running, or it would print the body and stop."""
    marketplace = context.marketplace
    endpoint_id = marketplace.endpoint_id if marketplace is not None else rest.split("?", 1)[0]
    endpoint = catalog_store.load().by_id.get(endpoint_id)
    descriptor = endpoint.get("async") if endpoint else None
    if not descriptor:
        return
    value = json.dumps(descriptor, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    upstream.raw_headers = tuple(
        (name, existing) for name, existing in upstream.raw_headers
        if name.lower() != b"x-treg-async"
    ) + ((b"x-treg-async", value),)


def _require_tool_use_http(caller: Caller, tool: Tool) -> None:
    try:
        access_policy._require_tool_use(caller, tool)
    except access_policy.AccessPolicyError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc


async def _enforce_public_demo_ip_cap(request: Request, db: AsyncSession) -> None:
    try:
        await publicdemo_policy.enforce_public_demo_ip_cap(_client_ip(request), db)
    except publicdemo_policy.PublicDemoLimitError as exc:
        await db.commit()
        raise HTTPException(status_code=429, detail=exc.detail) from exc
    await db.commit()


async def _enforce_daily_cap(caller: Caller, db: AsyncSession) -> None:
    """Refuse a call/run once the caller has used their per-user daily cap for this org. `-1` (the
    default) = unlimited, so unmetered members pay ZERO extra queries. The sandbox has its own limiter
    and is exempt. Soft by design: the count reads best-effort `CallRecord`s, so under heavy load it
    can lag slightly and fail OPEN (a few extra slip through) — never closed. See docs/USAGE-METERING-PLAN.md."""
    cap = caller.membership.daily_call_cap
    if cap < 0 or demo_sandbox.is_sandbox(caller.org):
        return
    used = await count_today(db, caller.org_id, caller.email)
    if used >= cap:
        raise HTTPException(status_code=429, detail=(
            f"daily usage limit reached ({used}/{cap}) — ask an admin to raise your cap"))


def _translate_call_failure(exc: CallFailure) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _parse_call_meta(request: Request, caller: Caller | None = None) -> CallMeta:
    try:
        return parse_call_meta(request.headers.get(META_HEADER), caller)
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc


async def _release_idempotent_claim(request: Request) -> None:
    claim = getattr(request.state, "idem_claim", None)
    request.state.idem_claim = None
    await release_idempotent_claim(claim)


def _query_values(request: Request) -> QueryValues:
    return QueryValues(tuple(request.query_params.multi_items()))


def _may_have_body(request: Request) -> bool:
    return may_have_body(tuple(request.headers.raw))


def _call_rest(request: Request, context: object | None) -> str:
    rest = getattr(getattr(context, "input", None), "raw_rest", None)
    if rest is not None:
        return rest
    call_path = split_call_path(request.url.path)
    return call_path[1] if call_path else request.url.path.lstrip("/")


def _capture_exceptional_call(
    request: Request, *, call_ref: str, status_code: int, failure_kind: str,
) -> None:
    """Mirror a call that never reached the normal audit funnel without leaking request data."""
    context = getattr(request.state, "call_context", None)
    marketplace = getattr(context, "marketplace", None)
    target = getattr(context, "target", None)
    rest = _call_rest(request, context)
    tool_name = (
        getattr(marketplace, "endpoint_id", None)
        or getattr(getattr(target, "tool", None), "name", None)
        or rest.split("/", 1)[0]
        or "-"
    )
    props = _tool_called_props(
        request,
        tool_name=tool_name,
        status_code=status_code,
        call_ref=call_ref,
        own_tool=(False if marketplace is not None else True if target is not None else None),
        refused_by=None,
        answered=False,
    ) | {"failure_kind": failure_kind, "provider": None, "endpoint_id": None}
    if marketplace is not None:
        props |= {
            "provider": marketplace.provider,
            "endpoint_id": marketplace.endpoint_id,
            "tier": marketplace.tier,
            "metered": marketplace.metered,
            "cost_type": marketplace.cost_type,
        }
    elif target is not None:
        props["provider"] = urlsplit(target.upstream).hostname or ""
    org_id, email = getattr(request.state, "call_identity", (None, ""))
    team_slug = getattr(request.state, "call_team_slug", "")
    analytics.capture(
        email or "anonymous",
        "tool_called",
        props,
        groups={"team": team_slug} if org_id is not None and team_slug else None,
    )


async def _stamp_call_exit(
    request: Request,
    resp: Response,
    status_code: int,
    *,
    failure_kind: str | None = None,
) -> None:
    """Give one call-surface exit the three things every other exit gets: the id that joins the response
    to the audit row, the row itself, and the release of any idempotency label the request took.

    Shared by the two handlers that answer a call without reaching `call_tool`'s own bookkeeping.
    Identity comes from `request.state` (stashed at handler entry); an exit that failed before the
    caller was resolved records an anonymous row, which is still the fact that someone knocked."""
    call_ref = getattr(request.state, "call_ref", "") or uuid.uuid4().hex
    request.state.call_ref = call_ref
    resp.headers["X-Treg-Call-Id"] = call_ref
    if (cost_micro := getattr(request.state, "call_cost_micro", None)) is not None:
        resp.headers["X-Treg-Cost-Micro"] = str(cost_micro)
    if not getattr(request.state, "call_audited", False):
        org_id, email = getattr(request.state, "call_identity", (None, ""))
        context = getattr(request.state, "call_context", None)
        rest = _call_rest(request, context)
        audit.record_call(
            org_id=org_id, user_email=email, tool_name=rest.split("/", 1)[0] or "—",
            method=request.method, path=request.url.path, status_code=status_code,
            client=_client_of(request), refused_by=_refusal_kind(status_code),
            telemetry={"call_ref": call_ref})
        if failure_kind:
            _capture_exceptional_call(
                request, call_ref=call_ref, status_code=status_code, failure_kind=failure_kind)
        request.state.call_audited = True
    # A failed call must not keep its idempotency label. The claim is taken before the upstream
    # call, and a request that dies anywhere after that — a bad parameter, a deny rule, an empty
    # balance, a saturated pool — would otherwise hold the label for the whole window and answer
    # every retry with 409. Worse than the problem this feature exists to solve, and found by the
    # test for it.
    await _release_idempotent_claim(request)


@app.get("/catalog/endpoints/{endpoint_id}/access", include_in_schema=False)
async def catalog_endpoint_access(
    endpoint_id: str, authorization_method: str = "",
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Translate the catalog access use case into HTTP."""
    try:
        return await get_catalog_endpoint_access(
            endpoint_id=endpoint_id,
            authorization_method=authorization_method,
            caller=caller,
            db=db,
        )
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc

@app.api_route(
    "/call/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def call_tool(
    rest: str,
    request: Request,
    caller: Caller = Depends(require_member),
):
    # Identity for the refusal fallback in `_mark_treg_own_errors`: a raise anywhere below (unknown
    # tool, deny rule, daily cap) leaves this handler without an audit row, and the exception handler
    # is the one place every such refusal passes through — but it has no Caller of its own.
    request.state.call_identity = (caller.org_id, caller.email)
    request.state.call_team_slug = caller.org.slug
    # Faithful-relay: use the RAW request path, not Starlette's decoded path param. Decoding is
    # lossy — an encoded slash (`%2f`) in `rest` would become a real `/` and change the upstream
    # route (npm's scoped publish `PUT /@scope%2fname` 404s as `/@scope/name`). httpx preserves
    # valid percent-escapes, so the original bytes travel through to the upstream one-to-one.
    raw_path = request.scope.get("raw_path")
    if raw_path:
        call_prefix = "/catalog/call/" if getattr(request.state, "catalog_only", False) else "/call/"
        _, sep, raw_rest = raw_path.decode("ascii", "replace").partition(call_prefix)
        if sep:
            rest = raw_rest
    call_input = CallInput(
        method=request.method,
        raw_rest=rest,
        raw_headers=tuple(request.headers.raw),
        query_items=tuple(request.query_params.multi_items()),
        raw_query=request.url.query or "",
        body=_HttpRequestBody(request),
        caller=CallerSnapshot.capture(caller),
        client_ip=_client_ip(request),
        catalog_only=bool(getattr(request.state, "catalog_only", False)),
    )
    try:
        context = create_call_context(call_input)
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc
    request.state.call_ref = context.call_ref
    request.state.call_context = context
    try:
        upstream = await execute_call(context, request.app.state.http)
        _attach_async_descriptor(upstream, context, rest)
        return _http_upstream_response(upstream)
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc
    except PoolTimeoutError:
        # The application-wide handler owns the typed 503 and its db_pool event.
        raise
    except Exception:
        # Starlette still owns the bare 500 response. Record the attempted call before re-raising so
        # this treg-owned failure is represented once in the same funnel as normal call outcomes.
        request.state.idem_claim = context.idempotency
        request.state.call_audited = context.audited
        request.state.call_cost_micro = context.cost_micro
        await _stamp_call_exit(
            request, Response(status_code=500), 500, failure_kind="unexpected_exception")
        context.idempotency = request.state.idem_claim
        context.audited = request.state.call_audited
        raise
    finally:
        request.state.idem_claim = context.idempotency
        request.state.call_audited = context.audited
        request.state.call_cost_micro = context.cost_micro


@app.api_route(
    "/catalog/call/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def call_catalog_endpoint(
    rest: str,
    request: Request,
    caller: Caller = Depends(require_member),
):
    """Call one catalog endpoint through the same policy, billing, audit, and relay path as /call."""
    if not get_settings().claude_connector_enabled:
        raise HTTPException(status_code=404, detail="Claude catalog connector is not enabled")
    request.state.catalog_only = True
    return await call_tool(rest, request, caller)
