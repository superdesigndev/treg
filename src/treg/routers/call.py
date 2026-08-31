"""HTTP adapters for the proxied call surface."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, oauth_providers
from .. import sandbox as demo_sandbox
from ..application.call.idempotency import (
    _release_idempotent_claim as release_idempotent_claim,
)
from ..application.call.resolve import (
    QueryValues,
    _enforce_catalog_status,
    _marketplace_secret,
    _may_have_body as may_have_body,
    _platform_estimate_micro,
    _platform_offer,
    _resolve_call,
    resolve_call_target,
)
from ..application.call.service import create_call_context, execute_call, _refusal_kind
from ..application.call.intake import (
    META_HEADER,
    CallMeta,
    _parse_call_meta as parse_call_meta,
)
from ..application.call.types import CallerSnapshot, CallFailure, CallInput, UpstreamResponse
from ..caller_metadata import _client_of
from ..config import get_settings
from ..domain import money as ledger
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


async def _stamp_call_exit(request: Request, resp: Response, status_code: int) -> None:
    """Give one `/call/` exit the three things every other exit gets: the id that joins the response
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
        rest = request.url.path[len("/call/"):]
        audit.record_call(
            org_id=org_id, user_email=email, tool_name=rest.split("/", 1)[0] or "—",
            method=request.method, path=request.url.path, status_code=status_code,
            client=_client_of(request), refused_by=_refusal_kind(status_code),
            telemetry={"call_ref": call_ref})
    # A failed call must not keep its idempotency label. The claim is taken before the upstream
    # call, and a request that dies anywhere after that — a bad parameter, a deny rule, an empty
    # balance, a saturated pool — would otherwise hold the label for the whole window and answer
    # every retry with 409. Worse than the problem this feature exists to solve, and found by the
    # test for it.
    await _release_idempotent_claim(request)


@app.get("/catalog/endpoints/{endpoint_id}/access", include_in_schema=False)
async def catalog_endpoint_access(
    endpoint_id: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Authenticated dry-run of the marketplace credential ladder — which tier would serve YOU.
    Read by `treg catalog get` to print an honest access line under RUN IT (the open catalog
    endpoints stay unauthenticated; this one needs to know who is asking)."""
    ep = catalog_store.load().by_id.get(endpoint_id)
    if ep is None:
        raise HTTPException(status_code=404, detail=f"unknown endpoint {endpoint_id!r}")
    try:
        _enforce_catalog_status(ep)
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc
    service = ep["provider"]
    if ep.get("kind") == "routed":
        # The routed row's dry-run IS the plan for this org: which child would go first, and why.
        from ..application.call.route import RouteOptions, build_plan
        opts = RouteOptions.from_headers(lambda k: None)
        plan = await build_plan(ep, dict(ep.get("test_request", {}).get("body") or {}), caller, opts)
        if not plan.candidates:
            # The example body is one identity shape; a team may hold keys for providers that take
            # another. Try each variant of the contract before declaring the job unservable.
            contract = catalog_store.load().contracts.get(ep.get("capability") or "")
            for variant in (contract.identity if contract else []):
                trial = await build_plan(ep, {k: "example" for k in variant}, caller, opts)
                if trial.candidates:
                    plan = trial
                    break
        if not plan.candidates:
            return {"tier": "none", "detail": "no provider can serve any identity shape of this job for your team right now",
                    "dropped": plan.dropped}
        first = plan.candidates[0]
        how = ("your registered tool" if first.tier == "tool" else "your own credential" if first.tier == "credential"
               else f"treg's {first.endpoint['provider']} key, ~${(first.price_micro or 0) / 1e6:g}")
        # The plan above is for ONE identity shape — the endpoint's example body. Most drops are
        # "this adapter takes a different identity", not "your team cannot reach this provider";
        # labelling them "not available here" read as no-key and sent a reader hunting for a
        # missing credential (2026-08-29). Name the shape, and give each drop its reason.
        dropped_note = ""
        if plan.dropped:
            dropped_note = ("; for this {" + ", ".join(plan.variant) + "} example, not usable: "
                            + ", ".join(f"{d['endpoint_id']} ({d['why']})" for d in plan.dropped))
        return {"tier": "routed", "detail": f"routed — {len(plan.candidates)} providers callable now; first: "
                                            f"{first.endpoint['id']} on {how} (send {{{', '.join(first.variant)}}})"
                                            + dropped_note,
                "plan": [c.view() for c in plan.candidates], "dropped": plan.dropped}
    provider = oauth_providers.get(service)
    if provider is None or not provider.base_url:
        return {"tier": "none", "detail": f"{service} isn't proxy-callable yet"}
    # An oauth-billed provider is metered even on the org's own connection (the upstream bills
    # treg's app, not the account) — the dry-run must say so, or the price is a surprise.
    billed_note = ""
    if provider.platform_billed and service in get_settings().oauth_billed_set:
        cv = catalog_store.load().cost_view(ep.get("cost"), service) if ep.get("cost") else None
        est = _platform_estimate_micro(cv, {}) if cv and cv.get("usd") else 0
        billed_note = (f" — metered from the team balance (~${ledger.usd(est):g}/call: "
                       f"{service} bills treg's app per use)") if est else \
                      f" — metered from the team balance ({service} bills treg's app per use)"
    probe = provider.base_url.rstrip("/") + "/" + (ep["path"] or "/").lstrip("/")
    try:
        target = await resolve_call_target(probe, caller, _resolve_call)
        tool = target.tool
        return {"tier": "tool", "metered": bool(billed_note),
                "detail": f"will use this org's registered {tool.name!r} tool{billed_note}"}
    except CallFailure as exc:
        if exc.status_code == 403:
            return {"tier": "restricted", "detail": "a registered tool exists but your access is restricted — ask an admin"}
        if exc.status_code != 404:
            raise _translate_call_failure(exc) from exc
    if await _marketplace_secret(service, caller.org_id, db) is not None:
        return {"tier": "credential", "metered": bool(billed_note),
                "detail": f"will use this org's {service} credential (no tool needed){billed_note}"}
    cost = _platform_offer(ep, provider, caller.org)
    if cost is not None:
        # The number is the honest per-call price at the DEFAULT page size — a `per_result` endpoint
        # costs more or less depending on how many rows the caller asks for, so it is "~".
        est = _platform_estimate_micro(cost, {})
        return {
            "tier": "platform",
            "detail": (f"no key needed — uses treg's {service} key, ~${ledger.usd(est):g}/call "
                       f"from your team balance (treg balance)"),
            "estimated_cost_micro": est,
            "estimated_cost_usd": ledger.usd(est),
        }
    hint = (f"connect with: treg connections connect --provider {service}"
            if not provider.uses_pasted_secret else
            f"connect with: treg connections connect --provider {service}, or treg secret add {service} …")
    return {"tier": "none", "detail": f"no {service} credential in this org yet — {hint}"}

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
    try:
        return _http_upstream_response(await execute_call(context, request.app.state.http))
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc
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
