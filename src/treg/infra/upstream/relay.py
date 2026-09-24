"""The relay — a faithful, smart proxy. The whole product in one function.

Faithfulness contract — it alters ONLY these, everything else is relayed verbatim
(method, path, ALL query params incl. duplicates, caller headers, caller cookies, body bytes streamed):
  1. transport/hop-by-hop headers — re-derived for the new hop (forwarding stale ones corrupts
     the stream); httpx sets them correctly upstream.
  2. treg's own control/infra headers + the edge's forwarding headers (`x-treg-*`,
     `ngrok-skip-browser-warning`, `x-forwarded-*`, `x-real-ip`, `forwarded`, `via`) — stripped so
     they never leak upstream. treg's own cookies (`treg_session`, `treg_oauth_state`) are scrubbed
     from the Cookie header too (the dashboard's `credentials:'include'` Try-it would otherwise leak
     our session token to the upstream); any other caller cookies are preserved.
  3. the credential(s) the tool's bindings inject — overwrite only their target header, query
     parameter or top-level JSON field. A JSON binding buffers, parses and reserializes the object;
     it is semantic JSON relay, not byte-faithful, and is forbidden for raw-body-signature APIs.
  4. on treg's SHARED key only (tier 4), the caller's `Idempotency-Key` is re-scoped per org by
     `scope_shared_idempotency_key` before the request is built. Every org shares one provider
     account there, so a provider that honors the header would hand org B the job org A created
     under the same label — and the ownership record would then make B its owner (reproduced live
     against LeadsForge, 2026-09-09). The caller loses nothing: treg's own idempotency table already
     replays their answer for the same label. A team's own key relays the header verbatim.

Except for that explicit JSON-binding contract, it never buffers the body (rule 5: stream, don't
duplicate) and uses the shared long-lived httpx client (rule 1: keepalive). Secrets are passed
already-loaded (api does the DB work).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable

import httpx

from ... import crypto
from ...application.call.types import GatewayFailed, UpstreamRequest, UpstreamResponse
from ...config import get_settings
from ...models import Secret, Tool
from . import injectors

# Connection-level headers that belong to a single hop and must NOT be forwarded as-is.
_HOP_BY_HOP = frozenset(
    {
        "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    }
)
# The edge's forwarding headers + third-party infra — never leak to the upstream. treg's OWN headers
# are handled by the `x-treg-` PREFIX rule below, not by name: this set used to enumerate them and the
# enumeration silently failed. `x-treg-client` was never listed, so every provider we relay to has been
# receiving the caller's runtime name; `x-treg-meta` would have leaked a builder's customer ids the same
# way. A prefix is the only form of this rule that stays correct when the next header is added.
_CONTROL = frozenset(
    {
        "ngrok-skip-browser-warning",
        "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port",
        "x-real-ip", "forwarded", "via",
    }
)
_TREG_PREFIX = "x-treg-"
_DROP_REQUEST = _HOP_BY_HOP | _CONTROL


def _is_dropped_request_header(name: str, extra: frozenset[str]) -> bool:
    """Whether a caller header is ours/hop-by-hop and must not reach the upstream."""
    return name in _DROP_REQUEST or name in extra or name.startswith(_TREG_PREFIX)


_IDEMPOTENCY_HEADER = b"idempotency-key"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object while refusing ambiguous duplicate keys before credential injection."""
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate JSON object key: {name}")
        result[name] = value
    return result


def scope_shared_idempotency_key(
    raw_headers: tuple[tuple[bytes, bytes], ...], org_id: int,
    *, pinned_tags: dict | None = None,
) -> tuple[tuple[bytes, bytes], ...]:
    """Rewrite 4 of the faithfulness contract: partition the caller's idempotency label by org and enforced pin.

    Only for calls on treg's shared provider key. The value is an opaque, fixed-length digest of
    (org, pin, label): distinct pins do not share a provider dedupe entry. With no pin, the
    existing (org, label) digest is preserved. The same scope retrying the
    same label still hits the provider's own dedupe should treg's replay window miss it.
    """
    scope = f"{org_id}\x1f".encode()
    if pinned_tags:
        scope += b"pins:" + json.dumps(pinned_tags, sort_keys=True, separators=(",", ":")).encode() + b"\x1f"
    return tuple(
        (k, hashlib.sha256(scope + v).hexdigest().encode())
        if k.lower() == _IDEMPOTENCY_HEADER else (k, v)
        for k, v in raw_headers
    )
_DROP_RESPONSE = _HOP_BY_HOP
_TREG_COOKIES = frozenset({"treg_session", "treg_oauth_state"})  # our cookies, scrubbed from Cookie


def _scrub_treg_cookies(headers: httpx.Headers) -> None:
    """Drop treg's own cookies from the forwarded Cookie header so a dashboard `credentials:'include'`
    call never leaks our session token upstream. Other caller cookies are kept; an emptied header is removed."""
    cookie = headers.get("cookie")
    if not cookie:
        return
    kept = [
        c.strip() for c in cookie.split(";")
        if c.strip() and c.split("=", 1)[0].strip().lower() not in _TREG_COOKIES
    ]
    if kept:
        headers["cookie"] = "; ".join(kept)
    else:
        del headers["cookie"]


async def relay(
    request: UpstreamRequest,
    upstream_url: str,
    tool: Tool,
    secrets: dict[int, Secret],
    client: httpx.AsyncClient,
    drop_params: set[str] | None = None,
    force_identity: bool = False,
) -> UpstreamResponse:
    # Headers: preserve everything (incl. duplicates / cookies) except hop-by-hop + our token.
    # `.raw` is the original (bytes, bytes) pairs; httpx.Headers is a multidict, so binding
    # injection (headers[name]=v) overwrites only its target and leaves the rest untouched.
    # RFC 7230 §6.1: also drop any header NAMED in the caller's own Connection header.
    req_drop = _connection_named(_header_value(request.raw_headers, "connection"))
    headers = httpx.Headers(
        [(k, v) for k, v in request.raw_headers
         if not _is_dropped_request_header(k.decode("latin-1").lower(), req_drop)]
    )
    _scrub_treg_cookies(headers)  # keep caller cookies, drop treg's own session cookie
    # Mirror the caller's compression choice. We relay the upstream body RAW (aiter_raw), so if the
    # upstream compresses, the caller receives compressed bytes. httpx supplies its own
    # `Accept-Encoding: gzip,…` whenever the request doesn't carry one — which would make us hand
    # gzip to a caller who never asked for it (binary garbage to any plain HTTP client or agent).
    # Asking for identity keeps what the caller gets matching what the caller requested.
    if "accept-encoding" not in headers:
        headers["accept-encoding"] = "identity"
    # Metered marketplace calls OVERRIDE the caller's choice: the settle path must json-parse the
    # buffered body for the provider's own reported charge, and a caller-requested gzip would hand
    # it ciphertext — the charge silently falls back to the estimate (real bug: dataforseo billed
    # $0.003 instead of its reported $0.00015 whenever the caller was httpx/a browser).
    if force_identity:
        headers["accept-encoding"] = "identity"
    # Query: a list of pairs preserves duplicate keys verbatim (?tag=a&tag=b). A marketplace
    # endpoint-id call may have CONSUMED some params into the path (`{siteUrl}` placeholders) —
    # those must not also reach the upstream as query noise.
    params: list[tuple[str, str]] = [
        (k, v) for k, v in request.query_items
        if not drop_params or k not in drop_params
    ]

    json_bindings = any(binding.get("location") == "json" for binding in tool.bindings)
    json_body: dict[str, object] | None = None
    if json_bindings:
        if not request.has_body or request.body_read is None:
            raise GatewayFailed(
                "injection_failed", status_code=502,
                detail="JSON credential injection requires a readable request body")
        try:
            parsed = json.loads(await request.body_read(), object_pairs_hook=_unique_json_object)
        except ValueError as exc:
            raise GatewayFailed(
                "injection_failed", status_code=502,
                detail="JSON credential injection requires valid JSON with unique object keys") from exc
        if not isinstance(parsed, dict):
            raise GatewayFailed(
                "injection_failed", status_code=502,
                detail="JSON credential injection requires a JSON object request body")
        json_body = parsed

    # Apply every binding (a request may need several credentials at once).
    for binding in tool.bindings:
        # A PLATFORM binding injects one of treg's own credentials (Google Ads' developer token),
        # read from settings rather than an org secret. Keeping it out of the org's secret store
        # matters: it's treg's credential, not the tenant's, so it must not be readable by them or
        # extractable through a local run.
        setting = binding.get("platform_setting")
        if setting:
            value = getattr(get_settings(), setting, "") or ""
            if not value:
                raise GatewayFailed(
                    "injection_failed", status_code=502,
                    detail=f"this server has no {setting} configured")
            try:
                injectors.inject(headers, params, binding, value, json_body=json_body)
            except ValueError as exc:
                raise GatewayFailed(
                    "injection_failed", status_code=502,
                    detail=f"credential injection failed: {exc}") from exc
            continue
        secret = secrets[binding["secret_id"]]
        try:
            injectors.inject(headers, params, binding, crypto.decrypt(secret.value),
                             json_body=json_body)
        except ValueError as exc:
            raise GatewayFailed(
                "injection_failed", status_code=502,
                detail=f"credential injection failed: {exc}") from exc

    # Only carry a body when the caller actually sent one — otherwise passing an (unsized) stream
    # makes httpx frame the request `Transfer-Encoding: chunked`, putting a bogus body-frame on a
    # GET/HEAD/OPTIONS (which strict upstreams reject).
    content = (json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode()
               if json_body is not None else request.body_stream() if request.has_body else None)
    # A streamed body with no length makes httpx frame it `Transfer-Encoding: chunked`. The bytes are
    # the caller's, unaltered, so the caller's own Content-Length is exact — carry it, and httpx
    # frames the upstream request with it instead. Meta's Graph API edge does not read a chunked
    # request body: every JSON/form/multipart POST arrived as a bodyless request, and an ad creative
    # sent that way failed "Ad incomplete" (live 2026-09-19). A caller who streamed chunked stays chunked.
    if json_body is not None:
        headers["content-length"] = str(len(content))
    elif content is not None and (cl := _header_value(request.raw_headers, "content-length")):
        headers["content-length"] = cl
    # Merge the query onto the URL rather than passing params=: httpx REPLACES a URL's existing
    # query whenever params is given (even an empty list), which silently stripped a catalog path's
    # own query — LinkedIn's `/rest/images?action=initializeUpload`, Facebook's `?is_hidden=true`.
    # Same trap, same fix as the health probe (health.py).
    url = httpx.URL(upstream_url)
    for k, v in params:
        url = url.copy_add_param(k, v)
    upstream_req = client.build_request(request.method, url, headers=headers, content=content)
    # Call-time SSRF guard: resolve the upstream host NOW and refuse an internal target — defeats DNS
    # rebinding (base_url was public at registration, its DNS now points at 169.254.169.254 / localhost).
    from . import health  # local: health imports proxy-adjacent modules, so keep the cycle lazy
    if get_settings().proxy_ssrf_check and not health.host_is_public(upstream_req.url.host):
        raise GatewayFailed(
            "ssrf_refused", status_code=502,
            detail="upstream host resolves to a non-public address")
    upstream_resp = await client.send(upstream_req, stream=True)

    # Response drop set: hop-by-hop + whatever the upstream marked hop-by-hop via its Connection
    # header. Keep upstream Content-Length on a bodyless reply (HEAD/204/304) — that's the whole
    # point of HEAD; for a normal GET we re-frame so it's dropped.
    drop_resp = _DROP_RESPONSE | _connection_named(upstream_resp.headers.get("connection"))
    if request.method == "HEAD" or upstream_resp.status_code in (204, 304):
        drop_resp = drop_resp - {"content-length"}
    # Relay every remaining upstream header verbatim (incl. multiple Set-Cookie), EXCEPT a
    # Set-Cookie for one of treg's own cookies — a registered upstream must not be able to
    # overwrite the operator's treg_session / treg_oauth_state under treg's origin (fixation).
    raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in upstream_resp.headers.multi_items()
        if k.lower() not in drop_resp and not _is_treg_setcookie(k, v)
    ]
    # The proxy is for API calls, but a browser could navigate to /call/… (authorized by the session
    # cookie) and render arbitrary upstream text/html AS AN ACTIVE DOCUMENT under treg's own origin —
    # reflected XSS with access to the operator's same-origin session. Neutralize it: nosniff + a
    # sandbox CSP (no script execution, no same-origin) on every relayed response. Agents ignore these.
    raw_headers.append((b"x-content-type-options", b"nosniff"))
    raw_headers.append((b"content-security-policy", b"sandbox"))
    return UpstreamResponse(
        status=upstream_resp.status_code,
        raw_headers=tuple(raw_headers),
        body_stream=upstream_resp.aiter_raw(),
        close=_close_once(upstream_resp.aclose),
    )


def _close_once(close: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
    """Join one close task even when its caller is cancelled or invokes cleanup repeatedly."""
    task: asyncio.Task[None] | None = None

    async def close_once() -> None:
        nonlocal task
        if task is None:
            task = asyncio.create_task(close())
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
        await task
        if interrupted:
            raise asyncio.CancelledError

    return close_once


def _connection_named(conn: str | None) -> frozenset[str]:
    """The header names a peer marked connection-scoped via its `Connection` header (RFC 7230)."""
    if not conn:
        return frozenset()
    return frozenset(t.strip().lower() for t in conn.split(",") if t.strip())


def _header_value(raw_headers: tuple[tuple[bytes, bytes], ...], name: str) -> str | None:
    wanted = name.encode("latin-1")
    for key, value in reversed(raw_headers):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def _is_treg_setcookie(name: str, value: str) -> bool:
    return name.lower() == "set-cookie" and value.split("=", 1)[0].strip().lower() in _TREG_COOKIES
