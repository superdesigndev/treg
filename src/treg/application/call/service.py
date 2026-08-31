"""The staged, framework-neutral proxied-call use case."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from ... import analytics, archive, audit, oauth
from ... import sandbox as demo_sandbox
from ...client_identity import _norm_client
from ...config import get_settings
from ...domain.catalog import store as catalog_store
from ...infra.db import session_maker
from ...models import Secret
from ...sandbox_identity import visitor_name
from ...domain.capacity import signatures as capacity_signatures
from ...domain.capacity.view import view as capacity_view
from ...infra.upstream.limiter import limiter as provider_limiter
from ...infra.upstream.relay import relay
from .authorize import authorize_call, enforce_public_demo_limit
from .evidence import (
    _ERROR_BODY_SLICE,
    _ERROR_CALLER_BODY_MAX,
    _ERROR_MASKING_FAILED,
    _ERROR_RESPONSE_MAX,
    _caller_request_snippet,
    _error_response_evidence,
    _redact_snippet,
    _safe_secret_renderings,
)
from .idempotency import IDEMPOTENCY_HEADER, _store_idempotent
from .intake import META_HEADER, _parse_call_meta, _tag_telemetry, prepare_call_intake
from .reserve import _enforce_tag_budgets, _platform_reserve
from .resolve import (
    MarketplaceCall,
    QueryValues,
    _billed_marketplace,
    _catalog_endpoint_for,
    _oauth_billed_provider,
    _resolve_call,
    resolve_call_target,
    resolve_marketplace_target,
)
from . import overflow as overflow_cycle
from . import route as routed
from .settle import (
    _buffer_response,
    _finish_cancelled_call as finish_cancelled_call,
    _note_capacity_signal,
    _peek_stream_head,
    _platform_settle,
    _record_first_call,
)
from .types import (
    AuthorizationFailed,
    CallContext,
    CallFailure,
    CallInput,
    FinalizationState,
    GatewayFailed,
    ResolutionFailed,
    UpstreamRequest,
    UpstreamResponse,
)


class _ApplicationRequest:
    def __init__(self, context: CallContext) -> None:
        self.context = context
        self.method = context.input.method
        self.raw_rest = context.input.raw_rest
        self.raw_query = context.input.raw_query
        self.headers = _HeaderView(context.input.raw_headers)
        self.query_params = QueryValues(context.input.query_items)
        self.body = context.input.body.read
        self.stream = context.input.body.stream
        self.caller = context.input.caller
        self.client_ip = context.input.client_ip
        self.has_body = _has_body(context.input.raw_headers)
        self.state = SimpleNamespace(
            idem_claim=context.idempotency,
            call_audited=context.audited,
            call_cost_micro=context.cost_micro,
        )
        self.db = session_maker()


def _served_response(served: dict, body: bytes) -> UpstreamResponse:
    """A stored answer in the relay's own clothes: an already-buffered UpstreamResponse carrying
    the cache headers. Content-length matches the bytes actually sent; nothing upstream to close."""
    async def _body():
        yield body

    async def _close() -> None:
        return None

    headers = [(b"content-length", str(len(body)).encode())]
    if served.get("media_type"):
        headers.append((b"content-type", served["media_type"].encode("latin-1", "replace")))
    headers += [
        (b"x-treg-cache", b"hit"),
        (b"x-treg-fetched-at", (served["fetched_at"].isoformat() + "Z").encode()),
        (b"x-treg-age", str(served["age_s"]).encode()),
    ]
    return UpstreamResponse(status=served["status_code"], raw_headers=tuple(headers),
                            body_stream=_body(), close=_close)


async def execute_call(context: CallContext, upstream_client: httpx.AsyncClient) -> UpstreamResponse:
    request = _ApplicationRequest(context)
    try:
        return await _execute_call(request, upstream_client)
    finally:
        context.idempotency = request.state.idem_claim
        context.audited = request.state.call_audited
        context.cost_micro = request.state.call_cost_micro
        await request.db.close()


def create_call_context(call_input: CallInput) -> CallContext:
    # The caller's tags, parsed ONCE and read by everything below — the budgets, the ledger, the
    # idempotency scope and the audit row. Before the idempotency block on purpose: a malformed bag
    # must not burn the caller's label on its way to a 422.
    meta = _parse_call_meta(
        _HeaderView(call_input.raw_headers).get(META_HEADER), call_input.caller)
    # ONE id for this call, minted before anything can spend: it becomes the ledger's call_id on a
    # metered call, lands on the audit row, and goes back as X-Treg-Call-Id — so a builder can join
    # our records to theirs on a single value.
    return CallContext(input=call_input, call_ref=uuid.uuid4().hex, meta=meta)


async def _finish_cancelled_call(
    request: _ApplicationRequest,
    mk: MarketplaceCall | None,
    call_ref: str,
    response: UpstreamResponse | None = None,
) -> None:
    claim, request.state.idem_claim = request.state.idem_claim, None
    if mk is not None and mk.metered:
        request.context.finalization = FinalizationState.FINALIZING
    await finish_cancelled_call(claim, mk, call_ref, response)


async def _await_before_reserve(awaitable, request: _ApplicationRequest, call_ref: str):
    try:
        return await awaitable
    except asyncio.CancelledError:
        await _finish_cancelled_call(request, None, call_ref)
        raise


def _client_name(request: _ApplicationRequest) -> str:
    return _norm_client(request.headers.get("X-Treg-Client", ""))


class _HeaderView:
    def __init__(self, raw: tuple[tuple[bytes, bytes], ...]) -> None:
        self.raw = raw

    def get(self, name: str, default=None):
        wanted = name.lower().encode("latin-1")
        for key, value in self.raw:
            if key.lower() == wanted:
                return value.decode("latin-1")
        return default


def _gateway_request_failure(exc: httpx.RequestError) -> GatewayFailed:
    kind = "read_timeout" if isinstance(exc, httpx.ReadTimeout) else "connect_failed"
    return GatewayFailed(
        kind, status_code=502,
        detail=f"upstream request failed: {str(exc) or type(exc).__name__}")


def _now_ms() -> int:
    """A monotonic millisecond stamp for measuring a call's duration."""
    return int(time.monotonic() * 1000)


def _has_body(raw_headers: tuple[tuple[bytes, bytes], ...]) -> bool:
    headers = httpx.Headers(raw_headers)
    content_length = headers.get("content-length")
    return bool(content_length is not None and content_length != "0") or (
        "chunked" in headers.get("transfer-encoding", "").lower())


SMOOTHING_RETRY_MAX_S = 5
"""The longest `retry-after` treg will honour with one re-send (plan §4.4, decided 2026-08-28)."""


def _idempotent_read(request: _ApplicationRequest) -> bool:
    """Only a body-less GET/HEAD is re-sent: the same bytes, provably, with nothing to replay."""
    return request.method.upper() in ("GET", "HEAD") and not request.has_body


def _burst_retry_after(provider: str, response: UpstreamResponse, body: bytes) -> float | None:
    """Seconds to wait before ONE re-send, or None when this 429 must be relayed as is: a quota
    429 (exhausted — marked after the settle), an unknown one, or a burst longer than the cap."""
    signal = capacity_signatures.classify(provider, 429, httpx.Headers(response.raw_headers), body[:4096])
    if signal is None or signal.kind != "burst" or signal.retry_after_s is None:
        return None
    if signal.retry_after_s > SMOOTHING_RETRY_MAX_S:
        return None
    return float(signal.retry_after_s)


def _hit_verdict(mk: MarketplaceCall, status: int, body: bytes) -> bool | None:
    """Found or not, read off a 2xx body by the endpoint's fixture-verified routing adapter; None
    when nothing can tell. The verdict is all that is kept — never the body."""
    if not 200 <= status < 300:
        return None
    adapter = catalog_store.load().adapters.get(mk.endpoint_id)
    if adapter is None or not adapter.verified:
        return None
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    try:
        return not adapter.is_miss(doc)
    except Exception:  # noqa: BLE001 — an undecidable predicate is a NULL, not a wrong verdict
        return None


def _refusal_kind(status_code: int) -> str | None:
    if status_code >= 500:
        return None
    return {401: "auth", 402: "balance", 403: "policy", 404: "resolution", 410: "retired",
            429: "cap"}.get(status_code, "request")


async def _empty_body():
    if False:
        yield b""


async def _one_chunk(body: bytes):
    yield body


async def _closed() -> None:
    return None


async def _drain(response: UpstreamResponse) -> bytes:
    """Read a small buffered response and re-arm it so it can still be returned."""
    chunks = []
    async for chunk in response.body_stream:
        chunks.append(chunk)
    body = b"".join(chunks)
    response.body_stream = _one_chunk(body)
    return body


def _bytes_response(
    content: bytes, status_code: int = 200, media_type: str = "",
    headers: dict[str, str] | None = None,
) -> UpstreamResponse:
    raw = [(b"content-length", str(len(content)).encode())]
    if media_type:
        raw.append((b"content-type", media_type.encode("latin-1")))
    raw.extend((name.lower().encode("latin-1"), value.encode("latin-1"))
               for name, value in (headers or {}).items())
    return UpstreamResponse(status_code, tuple(raw), _one_chunk(content), _closed)


def _json_response(value) -> UpstreamResponse:
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    return _bytes_response(body, media_type="application/json")


def _response_header(response: UpstreamResponse, name: str, default: str = "") -> str:
    wanted = name.encode("latin-1")
    for key, value in reversed(response.raw_headers):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return default


def _set_response_header(response: UpstreamResponse, name: str, value: str) -> None:
    wanted = name.lower().encode("latin-1")
    response.raw_headers = tuple(
        (key, old) for key, old in response.raw_headers if key.lower() != wanted
    ) + ((wanted, value.encode("latin-1")),)


async def _relay_live_demo(
    request: _ApplicationRequest, upstream_url: str, key: str, visitor: str,
    client: httpx.AsyncClient,
) -> UpstreamResponse:
    headers = {"Authorization": f"Bearer {key}"}
    content = None
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", "replace")
        pairs = [(k, v) for k, v in parse_qsl(body, keep_blank_values=True)
                 if k != "metadata[visitor]"]
        pairs.append(("metadata[visitor]", visitor))
        content = urlencode(pairs)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    response = await client.request(
        request.method, upstream_url, params=request.query_params.multi_items(),
        content=content, headers=headers)
    return _bytes_response(
        response.content, response.status_code,
        response.headers.get("content-type", "application/json"))


async def _execute_call(request: _ApplicationRequest, upstream_client: httpx.AsyncClient) -> UpstreamResponse:
    caller = request.caller
    rest = request.raw_rest
    meta = request.context.meta
    call_ref = request.context.call_ref
    db = request.db
    try:
        intake = await prepare_call_intake(
            meta=meta,
            idempotency_header=request.headers.get(IDEMPOTENCY_HEADER),
            method=request.method,
            rest=rest,
            raw_query=request.raw_query or "",
            read_body=request.body,
            caller=caller,
            enforce_tag_budgets=_enforce_tag_budgets,
        )
    except CallFailure:
        raise
    idem_key = intake.idempotency_key
    idem_fingerprint = intake.fingerprint
    if intake.replay is not None:
        replayed = intake.replay
        return _bytes_response(
            replayed.body,
            status_code=replayed.status_code,
            media_type=replayed.media_type,
            headers={"X-Treg-Idempotent-Replay": "true",
                     "X-Treg-Cost-Micro": str(replayed.charged_micro),
                     **({"X-Treg-Error": "1"} if replayed.status_code >= 400 else {}),
                     **({"X-Treg-Call-Id": replayed.call_ref} if replayed.call_ref else {})},
        )
    # Park it so a failure anywhere below can give the label back. Set AFTER the claim succeeds,
    # so losing the race above never releases the winner's row.
    request.state.idem_claim = intake.claim

    drop_params: set[str] = set()
    served_hit = False  # a cached hit — set where the archive answers instead of the vendor
    mk: MarketplaceCall | None = None
    own_tool_miss: dict | None = None
    ep: dict | None = None
    if request.context.input.catalog_only:
        # This reviewed surface accepts only a catalog id. A same-named team tool cannot shadow it.
        ep = _catalog_endpoint_for(rest)
        if ep is None:
            raise ResolutionFailed("target_not_found", status_code=404, detail=(
                f"unknown catalog endpoint {rest!r} — use catalog_search for a valid endpoint id"))
    else:
        try:
            target = await _await_before_reserve(
                resolve_call_target(rest, caller, _resolve_call), request, call_ref)
            request.context.target = target
            tool, upstream_url = target.tool, target.upstream
        except CallFailure as exc:
            # Not a tool → maybe a marketplace endpoint id (`treg call tikhub.tiktok.video.comments`).
            # Only the 404 falls through, so an org tool with the same name always wins.
            ep = _catalog_endpoint_for(rest) if exc.status_code == 404 else None
            if ep is None:
                raise
            if (isinstance(exc.detail, dict)
                    and str(exc.detail.get("hint", "")).startswith("your org has tool ")):
                own_tool_miss = exc.detail
    if ep is not None and ep.get("kind") == "routed":
        # A first-party routed endpoint (treg.<capability>): the router picks children and runs
        # each through THIS use case again (child contexts, own hold ids), then assembles one
        # answer. The parent owns the idempotency label and the X-Treg-* stamping below.
        try:
            body_bytes = await _await_before_reserve(request.body(), request, call_ref)
            response, charged = await routed.run_routed(
                request.context, ep, body_bytes, request.headers.get, upstream_client, execute_call,
                audit_client=_client_name(request))
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, None, call_ref)
            raise
        except CallFailure as exc:
            request.state.call_audited = True
            charged = (int(exc.detail.get("charged_micro") or 0)
                       if isinstance(exc.detail, dict) else 0)
            if exc.kind in ("route_caller_fault", "route_failed"):
                request.state.call_cost_micro = charged
            if idem_key and charged > 0 and exc.kind in ("route_caller_fault", "route_failed"):
                error_body = json.dumps(
                    {"detail": exc.detail}, ensure_ascii=False, allow_nan=False,
                    separators=(",", ":"),
                ).encode()
                try:
                    await _store_idempotent(
                        idem_key, caller, status_code=exc.status_code, body=error_body,
                        media_type="application/json", charged_micro=charged, metered=True,
                        call_ref=call_ref, terminal=True,
                    )
                except asyncio.CancelledError:
                    await _finish_cancelled_call(request, None, call_ref)
                    raise
                request.state.idem_claim = None
            audit.record_call(
                org_id=caller.org_id, user_email=caller.email, tool_name=ep["id"],
                method=request.method, path=rest, status_code=exc.status_code,
                client=_client_name(request), refused_by=_refusal_kind(exc.status_code),
                telemetry={"call_ref": call_ref, "endpoint_id": ep["id"], "provider": "treg",
                           "credential_tier": "routed", **_tag_telemetry(meta)})
            raise
        request.state.call_audited = True
        request.state.call_cost_micro = charged
        if idem_key:
            try:
                await _store_idempotent(idem_key, caller, status_code=response.status,
                                        body=await _drain(response), media_type="application/json",
                                        charged_micro=charged, metered=True, call_ref=call_ref)
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, None, call_ref)
                raise
            request.state.idem_claim = None
        _set_response_header(response, "X-Treg-Cost-Micro", str(charged))
        _set_response_header(response, "X-Treg-Call-Id", call_ref)
        return response
    if ep is not None:
        try:
            mk = await _await_before_reserve(resolve_marketplace_target(
                ep,
                method=request.method,
                query=request.query_params,
                has_body=request.has_body,
                read_body=request.body,
                caller=caller,
                resolve_call=_resolve_call,
            ), request, call_ref)
        except CallFailure as mkexc:
            # Catalog resolution is allowed to fall through from a named miss, but its own 404 must
            # not discard the useful fact discovered there: this org already has a nearby own tool.
            if mkexc.status_code == 404 and own_tool_miss is not None:
                mkexc.detail = {
                    "error": mkexc.detail,
                    "hint": own_tool_miss["hint"],
                    "did_you_mean": own_tool_miss["did_you_mean"],
                }
            # A malformed marketplace call (wrong method, missing param, no credential, 502) must
            # still leave a trace — it's exactly the row the caller will come asking about.
            request.state.call_audited = True
            audit.record_call(
                org_id=caller.org_id, user_email=caller.email, tool_name=ep["id"],
                method=request.method, path=rest, status_code=mkexc.status_code,
                client=_client_name(request),
                refused_by=("capacity" if mkexc.kind == "provider_capacity"
                            else _refusal_kind(mkexc.status_code)),
                telemetry={"call_ref": call_ref,
                           "endpoint_id": ep["id"], "provider": ep.get("provider"),
                           **_tag_telemetry(meta)})
            analytics.capture(caller.email, "tool_called",
                {"tool_name": ep["id"], "status_code": mkexc.status_code,
                 "client": _client_name(request), "method": request.method,
                 "own_tool": False, "provider": ep.get("provider"), "endpoint_id": ep["id"]},
                groups={"team": caller.org.slug})
            raise
        tool, upstream_url, drop_params = mk.tool, mk.upstream, mk.consumed
        request.context.marketplace = mk
    try:
        await _await_before_reserve(
            authorize_call(
                caller=caller,
                tool=tool,
                upstream_url=upstream_url,
                method=request.method,
                client_ip=request.client_ip,
            ),
            request,
            call_ref,
        )
    except CallFailure:
        raise

    # The caller's own request bytes, read ONCE when it is safe to buffer them, so a failure can be
    # explained later (see models.CallRecord.error_request). Metered JSON calls already require full
    # buffering. Otherwise only a declared body at or below 64 KiB is cached; large and chunked uploads
    # keep streaming and still retain their query-param half if they fail. Starlette's request cache
    # lets relay stream the same bytes after this read.
    # Named `caller_body`: `body` in this function is the buffered RESPONSE, and confusing the two
    # would file the provider's answer as the caller's request.
    caller_body = b""
    content_length = request.headers.get("content-length")
    small_declared_body = False
    if content_length is not None:
        try:
            small_declared_body = 0 <= int(content_length) <= _ERROR_CALLER_BODY_MAX
        except ValueError:
            small_declared_body = False
    if request.has_body and ((mk is not None and mk.metered) or small_declared_body):
        try:
            caller_body = await _await_before_reserve(request.body(), request, call_ref)
        except Exception:  # noqa: BLE001 — a caller that hung up must not become a 500 here
            caller_body = b""

    # Snapshot the audit identity NOW: a failed reserve rolls the session back, expiring the ORM
    # instances behind `caller` — reading them inside a later _audit would raise MissingGreenlet.
    audit_org_id, audit_email, audit_tool = caller.org_id, caller.email, tool.name
    audit_slug = caller.org.slug  # PostHog group key — must match the browser's posthog.group('team', slug)

    def _audit(status_code: int, *, observed_micro: int | None = None, charged_micro: int | None = None,
               duration_ms: int | None = None, response_bytes: int | None = None,
               refused_by: str | None = None, hit: bool | None = None,
               error_request: str | None = None, error_response: str | None = None) -> None:
        # Audit the attempt too — failures are results worth recording. A marketplace call additionally
        # carries its telemetry (which endpoint, which credential tier, what it cost): still
        # fire-and-forget, because the money itself already landed synchronously in the ledger.
        request.state.call_audited = True  # the refusal fallback in _mark_treg_own_errors stands down
        telemetry: dict = {"call_ref": call_ref}
        if meta.tags:
            # Own-tool calls carry tags too: a builder's usage report has to account for every call
            # their user made, not only the ones that spent treg's money.
            telemetry |= _tag_telemetry(meta)
        if mk is not None:
            telemetry |= {
                "endpoint_id": mk.endpoint_id, "provider": mk.provider, "credential_tier": mk.tier,
                # The archive answered instead of the vendor; money columns are identical to a
                # live call ON PURPOSE (the pricing of a hit is a deferred founder decision).
                **({"cached": True} if served_hit else {}),
                # An org credential riding treg's pay-per-use OAuth app: tier stays tool/credential
                # (the credential IS theirs), this says who the upstream billed.
                **({"oauth_billed": True} if mk.billed_oauth else {}),
                "cost_estimated_micro": mk.estimate_micro or None,  # informational on tiers 1/2
                "cost_observed_micro": observed_micro,
                "cost_charged_micro": charged_micro,
                "duration_ms": duration_ms, "response_bytes": response_bytes,
                "params_hash": mk.params_hash,
                # found / not found, when this endpoint's routing adapter could read the body
                **({"hit": hit} if hit is not None else {}),
            }
        # Sanctioned reversal of PR #139: failed own-key and own-tool calls now retain the same
        # redacted, admin-only, 14-day evidence as marketplace failures. Successes remain empty and
        # `/calls` still never exposes these columns.
        if error_request or error_response:
            telemetry |= {"error_request": error_request, "error_response": error_response}
        audit.record_call(
            org_id=audit_org_id, user_email=audit_email, tool_name=audit_tool,
            method=request.method, path=upstream_url, status_code=status_code,
            client=_client_name(request), refused_by=refused_by, telemetry=telemetry,
        )
        # Product analytics mirror of the row above. Deliberately excludes params, bodies, and the
        # full upstream URL (hostname only) — per-call detail beyond what a chart needs stays in the DB.
        props = {"tool_name": audit_tool, "status_code": status_code,
                 "client": _client_name(request), "method": request.method,
                 "own_tool": mk is None, "duration_ms": duration_ms}
        if mk is not None:
            props |= {"provider": mk.provider, "endpoint_id": mk.endpoint_id,
                      "tier": mk.tier, "metered": mk.metered, "cost_type": mk.cost_type,
                      "charged_micro": charged_micro, "observed_micro": observed_micro}
        else:
            props["provider"] = urlsplit(upstream_url).hostname or ""
        analytics.capture(audit_email, "tool_called", props, groups={"team": audit_slug})

    # Landing-page sandbox: never touch the network — EXCEPT the one live wire. A call to the
    # exact seeded stripe tool (fingerprint-matched; see sandbox.is_live_tool) relays to the real
    # Stripe test API with the env-held demo key. Any tampered/lookalike tool falls through to
    # synthesize below, so there is never a key to exfiltrate from a sandbox org.
    if demo_sandbox.is_sandbox(caller.org):
        live_key = get_settings().demo_stripe_key
        if live_key and demo_sandbox.is_live_tool(tool) and request.method in ("GET", "POST"):
            try:
                await _await_before_reserve(
                    enforce_public_demo_limit(request.client_ip), request, call_ref
                )  # one shared wire → meter by client IP
            except CallFailure:
                raise
            await _await_before_reserve(
                db.commit(), request, call_ref
            )  # end the DB phase before network I/O (see the same call before relay())
            try:
                response = await _await_before_reserve(
                    _relay_live_demo(
                        request, upstream_url, live_key,
                        visitor_name(caller.org.slug), upstream_client),
                    request, call_ref)
            except httpx.RequestError as exc:
                _audit(502)
                raise _gateway_request_failure(exc) from exc
            _audit(response.status)
            return response
        secrets = {}
        for sid in {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}:
            s = await _await_before_reserve(db.get(Secret, sid), request, call_ref)
            if s is not None and s.org_id == caller.org_id:
                secrets[sid] = s
        body = (await _await_before_reserve(request.body(), request, call_ref)).decode(
            "utf-8", "replace")
        result = demo_sandbox.synthesize(
            request.method, upstream_url, tool, secrets,
            query=request.query_params.multi_items(), body=body)
        _audit(200)
        return _json_response(result)

    # Load every secret the bindings need BEFORE the money gate (api does the DB work; proxy stays
    # I/O-free): whether this call is METERED can depend on the credential itself — a registry X
    # connect rides treg's pay-per-use app, so the org's "own" oauth secret is exactly what makes
    # the call billable. Nothing is reserved yet, so a load failure here leaves no hold behind.
    secrets: dict[int, Secret] = {}
    try:
        # A platform binding carries no secret_id — its value comes from settings at relay time.
        for sid in {b["secret_id"] for b in tool.bindings if b.get("secret_id") is not None}:
            secret = await _await_before_reserve(db.get(Secret, sid), request, call_ref)
            if secret is None or secret.org_id != caller.org_id:
                raise CallFailure(
                    "credential_missing", status_code=409, detail="a bound secret is missing")
            secrets[sid] = secret
    except CallFailure as exc:
        _audit(exc.status_code)  # record the failed attempt, same as a mid-relay refusal would
        raise
    billed_provider = _oauth_billed_provider(secrets)
    if billed_provider is not None:
        # The sandbox never reaches here (it returned above); the public demo could, and one shared
        # org must never be able to spend treg's upstream credits — refuse rather than relay free.
        if caller.org.public_demo:
            _audit(403)
            raise AuthorizationFailed("policy_denied", status_code=403, detail=(
                f"{billed_provider.display_name} calls are pay-per-use on treg's app and the "
                f"public demo can't spend — create your own team to use this"))
        mk = await _await_before_reserve(_billed_marketplace(
            mk, billed_provider, tool, upstream_url,
            method=request.method,
            query=request.query_params,
            has_body=request.has_body,
            read_body=request.body,
        ), request, call_ref)

    if mk is not None and mk.skip_direct:
        # The resolver knows treg's own account is out and an overflow route is on: no direct
        # attempt, no parent hold — straight to the child cycle (plan §4 ladder, tier 4b). The DB
        # phase ends here; the child places its own hold and the aggregator answers with none open.
        await db.commit()
        _audit(503, charged_micro=0, refused_by="capacity",
               error_response="treg: own account exhausted — served via overflow" )
        try:
            outcome = await overflow_cycle.maybe_overflow(
                mk=mk, caller=caller, meta=meta, call_ref=call_ref, status=402,
                headers=httpx.Headers(()), body=b"", method=request.method,
                query_items=request.query_params.multi_items(), caller_body=caller_body,
                client=upstream_client, audit_client=_client_name(request), force_trigger="exhausted")
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref)
            raise
        if outcome is None or not outcome.served or outcome.response is None:
            request.state.call_cost_micro = 0
            from .resolve import _provider_capacity_unavailable
            raise (outcome.failure if outcome is not None and outcome.failure is not None
                   else _provider_capacity_unavailable(
                       _catalog_endpoint_for(mk.endpoint_id) or {"id": mk.endpoint_id},
                       mk.provider, capacity_view.get(mk.provider)))
        response, body, charged = outcome.response, outcome.body, outcome.charged_micro
        if idem_key:
            try:
                await _store_idempotent(idem_key, caller, status_code=response.status, body=body,
                                        media_type=_response_header(response, "content-type"),
                                        charged_micro=charged, metered=True, call_ref=call_ref)
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
            request.state.idem_claim = None
        _set_response_header(response, "X-Treg-Cost-Micro", str(charged))
        _set_response_header(response, "X-Treg-Call-Id", call_ref)
        _set_response_header(response, "X-Treg-Served-Via", f"overflow:{outcome.aggregator}")
        return response

    # Metered — treg's own money is about to be spent (tier 4's platform key, or a registry OAuth
    # connect on a pay-per-use app), so take the money FIRST. Deliberately the last gate before the
    # network: everything above (ACL, deny rules, caps) can still refuse the call, and a refused
    # call must not leave a hold behind for the reaper to clean up.
    if mk is not None and mk.metered:
        # Rendered BEFORE the reserve while `tool` is live. The application closes its reservation
        # session before returning a 402, so refusal evidence cannot rely on a later ORM load. Doing
        # that once turned the refusal an agent is most likely to hit into a 500 with no balance or
        # top-up URL. Same reasoning as `block_id` in billing._credit, and pinned by a test.
        refusal_secrets = _safe_secret_renderings(tool, secrets)
        request.context.finalization = FinalizationState.PENDING
        try:
            # Secret reads above opened the dependency session. Release its pool slot before the
            # application opens the short transaction that owns the reservation.
            await db.commit()
            await _platform_reserve(mk, caller, meta=meta, call_ref=call_ref)
            request.context.finalization = FinalizationState.OPEN
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref)
            raise
        except CallFailure as exc:
            # A call refused for MONEY (402 empty balance / 429 daily cap) is the event the org will
            # ask about first — it must appear in the activity feed, charged 0.
            #
            # Keep the detail, because `cap` alone is not a diagnosis: every 429 maps to it, and that
            # covers a member call cap, a tag call or spend cap, the platform ceiling, a trial
            # allowance and a demo-IP limit. WHICH one is in `exc.detail` and was being discarded —
            # 878 refusals in a week that could not be told apart afterwards. This branch is inside
            # `mk.metered`, so it stays platform-only like every other capture site, and it runs
            # BEFORE relay, so no provider content can reach it.
            #
            # It is NOT free of caller data, though: a tag-cap detail carries the tag's `val` — an
            # end-customer id the builder supplied. That is the caller's own identifier, in the
            # caller's own row, and it is also the thing that makes the refusal diagnosable ("which
            # customer hit the cap"). It is strictly less than the request bodies this feature
            # already retains, and it is bounded by the same redaction and 14-day retention.
            _audit(exc.status_code, charged_micro=0,
                   refused_by="balance" if exc.status_code == 402 else "cap",
                   error_response=(
                       _ERROR_MASKING_FAILED if refusal_secrets is None else
                       _redact_snippet(f"treg: {exc.detail}", refusal_secrets,
                                       _ERROR_RESPONSE_MAX)))
            raise
    body = b""
    response: UpstreamResponse | None = None
    started = _now_ms()
    try:
        # treg keeps oauth tokens fresh: refresh in place if stale, before injecting. Inside the
        # try on purpose — a failed refresh after a reserve must release the hold (502 path below).
        for secret in secrets.values():
            try:
                await oauth.ensure_fresh(secret, db, upstream_client)
            except Exception as exc:  # noqa: BLE001 — surface a clear 502 instead of injecting a dead token
                raise GatewayFailed(
                    "refresh_failed", status_code=502,
                    detail=f"oauth refresh failed: {exc}") from exc
        request.context.credentials = secrets
        # END THE DB PHASE BEFORE NETWORK I/O. From here until the settle this request must hold NO
        # pooled connection. `ledger.reserve` already committed, but the org refresh after it, the
        # secret loads and a token refresh each auto-began a fresh transaction on this session, and
        # SQLAlchemy keeps that transaction's connection checked out until commit — i.e. for the whole
        # upstream round trip. `_platform_settle` then opens its OWN session for a second connection.
        # Two per in-flight call against a 15-slot pool (db.py) deadlocked at 15 concurrent calls: every
        # settle waited on a slot only another waiting call could free, until `pool_timeout` killed one
        # (a bare 500, or a settle that forfeited its charge) and the rest cascaded — every call in a
        # burst "took 30 s" (2026-08-24, reproduced from bootoshi's #9/#10). Nothing below reads `db`
        # (settle, first-call and the idempotent store all run on their own sessions), and the session
        # is `expire_on_commit=False`, so `tool`/`secrets`/`caller.org` stay usable without a reload.
        await db.commit()
        smoothed: list[str] = []
        try:
            upstream_request = UpstreamRequest(
                method=request.method,
                raw_headers=tuple(request.headers.raw),
                query_items=tuple(request.query_params.multi_items()),
                body_stream=request.stream,
                has_body=request.has_body,
            )
            platform_tier = mk is not None and mk.tier == "platform"
            if platform_tier:
                # Burst smoothing, half one (plan §4.4): many callers share treg's key, so a call that
                # would exceed the provider's published rate waits briefly (≤ 2 s, in-process, no DB —
                # the DB phase ended above) instead of being relayed into a 429 nobody can fix. Beyond
                # the cap it proceeds as before; nothing is ever refused here.
                rl = capacity_view.rate_limit(mk.provider)
                if rl is not None:
                    waited = await provider_limiter.acquire(mk.provider, *rl)
                    if waited:
                        smoothed.append(f"wait={waited}")
            # The archive's serve path (docs/context/architecture/archive.md): a fresh stored
            # answer replaces ONLY the network trip. Reserve already ran, settle/audit/cost header
            # run below unchanged — a cached hit is billed exactly like the live call it stands in
            # for, tagged `cached`; the founder's deferred pricing decision attaches to that tag.
            served = None
            if mk is not None and mk.metered and archive.serving():
                try:
                    served = await archive.lookup(
                        method=request.method, endpoint_id=mk.endpoint_id,
                        url=archive.key_url(upstream_url,
                                            list(request.query_params.multi_items()),
                                            drop_params or set()),
                        caller_body=caller_body, request_headers=request.headers)
                except Exception:  # noqa: BLE001 — lookup swallows internally; this catches even a
                    served = None  # fault in its own plumbing. Cache trouble must cost a vendor
                    #              call, never a 500.
            if served is not None:
                body = served["body"]
                served_hit = True
                response = _served_response(served, body)
            else:
                response = await relay(
                    upstream_request,
                    upstream_url, tool, secrets, upstream_client,
                    drop_params=drop_params or None,
                    force_identity=mk is not None and mk.metered,
                )
            if served is None and mk is not None and mk.metered:
                # Metered calls don't stream: settling needs the provider's own reported cost, which is
                # in the body (see _buffer_response). A failure while draining is still an upstream
                # failure, so it becomes a 502 and the hold goes back.
                response, body = await _buffer_response(response)
                if (platform_tier and response.status == 429 and _idempotent_read(request)
                        and (retry_s := _burst_retry_after(mk.provider, response, body)) is not None):
                    # Half two: ONE bounded wait on the provider's own `retry-after`, then the identical
                    # request again on the same hold — idempotent reads only, only when the provider
                    # said when, never on 401/402/5xx (the "no retries" rule stands for those).
                    await response.close()
                    await asyncio.sleep(retry_s)
                    response = await relay(
                        upstream_request, upstream_url, tool, secrets, upstream_client,
                        drop_params=drop_params or None, force_identity=True)
                    response, body = await _buffer_response(response)
                    smoothed.append("retry=1")
                # The archive's recorder (docs/context/architecture/archive.md): the body is already
                # in memory here for the settle, so observing it costs nothing on-request. Metered
                # 2xx only — gate 3 of eligibility is exactly 'this fact, at this line'. Off unless
                # TREG_ARCHIVE_MODE says otherwise; record() is fire-and-forget and never raises.
                if archive.recording() and 200 <= response.status < 300:
                    _ct = next((v.decode("latin-1") for k, v in response.raw_headers
                                if k.lower() == b"content-type"), "")
                    archive.record(
                        method=request.method, endpoint_id=mk.endpoint_id, provider=mk.provider,
                        url=archive.key_url(upstream_url,
                                            list(request.query_params.multi_items()),
                                            drop_params or set()),
                        caller_body=caller_body,
                        headers={k: request.headers.get(k, "") for k in ("accept", "accept-language")},
                        status_code=response.status, media_type=_ct, body=body)
            elif response.status >= 400:
                # Preserve streaming for own-key and own-tool calls while retaining only the small
                # diagnostic head. The replacement response replays every consumed byte verbatim.
                response, body = await _peek_stream_head(response, _ERROR_BODY_SLICE)
        except GatewayFailed:
            raise
        except httpx.RequestError as exc:  # upstream down/timeout is a gateway fault, not treg's 500
            raise _gateway_request_failure(exc) from exc
    except asyncio.CancelledError:
        await _finish_cancelled_call(request, mk, call_ref, response)
        raise
    except CallFailure as exc:
        # The provider never produced a billable answer (our own error, a failed injection, an
        # unreachable upstream) → return the hold in full, regardless of the endpoint's billing type.
        metered = mk is not None and mk.metered
        if metered:
            try:
                request.context.finalization = FinalizationState.FINALIZING
                await _platform_settle(
                    mk, None, reason=f"call_failed_{exc.status_code}",
                    finalized=lambda: setattr(
                        request.context, "finalization", FinalizationState.FINALIZED))
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
            # The shared exception handler builds the response and adds this zero-cost result.
            request.state.call_cost_micro = 0
        # No provider body exists on this branch. treg's own detail is the explanation instead, and
        # it is the one worth keeping: this branch carries refresh, timeout, injection and SSRF 502s.
        _renderings = _safe_secret_renderings(tool, secrets)
        _audit(exc.status_code, charged_micro=0 if metered else None,
               duration_ms=_now_ms() - started,
               error_request=(
                   _ERROR_MASKING_FAILED if _renderings is None else
                   _caller_request_snippet(
                       request.query_params.items,
                       request.headers.get("content-type", ""),
                       tool, caller_body, _renderings)),
               error_response=(
                   _ERROR_MASKING_FAILED if _renderings is None else
                   _redact_snippet(f"treg: {exc.detail}", _renderings, _ERROR_RESPONSE_MAX)))
        raise
    except Exception:  # noqa: BLE001 — an unexpected fault is still not the caller's bill
        # The reaper would eventually return this hold anyway; returning it now means a bug in the call
        # path can't make a funded org look broke for the next three minutes.
        if mk is not None and mk.metered:
            try:
                request.context.finalization = FinalizationState.FINALIZING
                await _platform_settle(
                    mk, None, reason="call_crashed",
                    finalized=lambda: setattr(
                        request.context, "finalization", FinalizationState.FINALIZED))
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
        raise
    duration_ms = _now_ms() - started
    # First successful call. The common case — an org that already has one — is an in-memory check
    # against `caller.org` (freshly loaded this request by require_member): zero DB cost on a path
    # that runs on every proxied call. Only an org's actual first call touches the database, and it
    # does so via _record_first_call's own session, never the request's `db` (which _platform_settle,
    # right below, is about to settle/release — see its docstring for why that session is off-limits).
    if 200 <= response.status < 400 and caller.org_id and caller.org.first_call_at is None:
        try:
            await _record_first_call(caller.org_id)
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref, response)
            raise
    if mk is not None and mk.metered:
        try:
            request.context.finalization = FinalizationState.FINALIZING
            charged, observed = await _platform_settle(
                mk, response.status, body, headers=httpx.Headers(response.raw_headers),
                # `provider_failed_`, not `call_failed_`: the latter is the branch above, where treg
                # never got an answer (timeout, SSRF refusal, a failed oauth refresh). Both release a
                # 502 the same way, so a shared prefix would make the two indistinguishable in the
                # journal once the 14-day error evidence expires — and they need different fixes.
                reason=(f"provider_failed_{response.status}" if response.status >= 500 else ""),
                finalized=lambda: setattr(
                    request.context, "finalization", FinalizationState.FINALIZED),
            )
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref, response)
            raise
        if response.status >= 400:
            # Did the provider just say OUR account is out? Mark it for the next caller (plan
            # §4.1). After the settle on purpose: the hold is closed, no connection is held.
            try:
                await _note_capacity_signal(mk, response.status, httpx.Headers(response.raw_headers), body)
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
        # A relayed non-2xx arrives HERE, as a Response — the vendor's own status is never raised
        # (see _refusal_kind). So this is where the provider's own explanation is captured, and the
        # only place it exists: nothing downstream keeps the body.
        err_request = err_response = None
        if response.status >= 400:
            _renderings = _safe_secret_renderings(tool, secrets)
            if _renderings is None:
                err_request = err_response = _ERROR_MASKING_FAILED
            else:
                err_request = _caller_request_snippet(
                    request.query_params.items,
                    request.headers.get("content-type", ""),
                    tool, caller_body, _renderings)
                err_response = _error_response_evidence(
                    response.raw_headers, body, _renderings)
        _audit(response.status, observed_micro=observed, charged_micro=charged,
               duration_ms=duration_ms, response_bytes=len(body), hit=_hit_verdict(mk, response.status, body),
               error_request=err_request, error_response=err_response)
        served_via = ""
        if response.status >= 400 and mk.tier == "platform":
            # Overflow (plan §4.3): the primary attempt is settled ($0) and audited above; a child
            # cycle may now serve the SAME endpoint through an aggregator. Off by default; shadow
            # mode returns the vendor's answer regardless.
            try:
                outcome = await overflow_cycle.maybe_overflow(
                    mk=mk, caller=caller, meta=meta, call_ref=call_ref, status=response.status,
                    headers=httpx.Headers(response.raw_headers), body=body, method=request.method,
                    query_items=request.query_params.multi_items(), caller_body=caller_body,
                    client=upstream_client, audit_client=_client_name(request))
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
            except CallFailure as exc:  # the child's own 402 (insufficient balance for the child hold)
                request.state.call_cost_micro = 0
                raise
            if outcome is not None and outcome.failure is not None:
                request.state.call_cost_micro = 0
                raise outcome.failure
            if outcome is not None and outcome.served and outcome.response is not None:
                await response.close()
                response, body, charged = outcome.response, outcome.body, outcome.charged_micro
                served_via = f"overflow:{outcome.aggregator}"
        if idem_key:
            # Here, and not earlier: this is the first point where BOTH the response and what it
            # actually cost are known, and a replay has to hand back the real charge rather than the
            # estimate that was reserved.
            try:
                await _store_idempotent(idem_key, caller, status_code=response.status, body=body,
                                        media_type=_response_header(response, "content-type"),
                                        charged_micro=charged, metered=True, call_ref=call_ref)
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
            request.state.idem_claim = None      # dealt with; nothing left to release
        # Tell the caller what the call actually cost. Both llms.txt and skill.md instruct an agent to
        # report the price it spent, and until now the only way to find out was to read the balance
        # before and after — which races with any other call and cannot attribute a figure to a
        # request. The header is set only on a METERED call: a team's own key is never charged, and a
        # `0` there would read as "free" rather than "not applicable".
        _set_response_header(response, "X-Treg-Cost-Micro", str(charged))
        _set_response_header(response, "X-Treg-Call-Id", call_ref)
        if served_via:
            _set_response_header(response, "X-Treg-Served-Via", served_via)
        if smoothed:
            _set_response_header(response, "X-Treg-Smoothed", " ".join(smoothed))
        return response
    # Fire-and-forget audit — does not block the streaming response (rule #2). A failed unmetered
    # call has already yielded just enough response bytes to retain redacted evidence; successes
    # still take the untouched streaming path.
    err_request = err_response = None
    if response.status >= 400:
        _renderings = _safe_secret_renderings(tool, secrets)
        if _renderings is None:
            err_request = err_response = _ERROR_MASKING_FAILED
        else:
            err_request = _caller_request_snippet(
                request.query_params.items,
                request.headers.get("content-type", ""),
                tool, caller_body, _renderings)
            err_response = _error_response_evidence(response.raw_headers, body, _renderings)
    _audit(response.status, duration_ms=duration_ms,
           error_request=err_request, error_response=err_response)
    if idem_key:
        # Unmetered: nothing was billed, so there is nothing to protect. Dropping the claim frees the
        # label at once instead of making the caller wait out the window to reuse it.
        try:
            await _store_idempotent(idem_key, caller, status_code=response.status, body=b"",
                                    media_type="", charged_micro=0, metered=False)
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref, response)
            raise
        request.state.idem_claim = None
    _set_response_header(response, "X-Treg-Call-Id", call_ref)
    return response
