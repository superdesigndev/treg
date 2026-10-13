"""The outermost HTTP middleware: request admission before auth, the database or money.

HTTP translation only; every decision is `application.ratelimit.limiter`. The caller is the one
credential the route's auth honors (`request_scope.credential`). The client IP is the entry the
nearest proxy appended to X-Forwarded-For (`client_ip`); what the client wrote there is never used.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json

import anyio
from starlette.datastructures import Headers

from .application.ratelimit import inflight, metrics
from .application.ratelimit.limiter import RequestFacts, limiter
from .config import get_settings
from .infra import request_scope


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def client_ip(scope) -> str:
    """The entry `rate_limit_proxy_hops` from the right of X-Forwarded-For: the one the nearest
    proxy appended. Whatever the client wrote sits further left and is never used. With no proxy
    (0 hops), or no usable entry, the TCP peer."""
    client = scope.get("client")
    peer = (client[0] if client else "") or "unknown"
    hops = get_settings().rate_limit_proxy_hops
    if hops <= 0:
        return peer
    values = [v.decode("latin-1") for k, v in scope.get("headers") or () if k.lower() == b"x-forwarded-for"]
    entries = [e.strip() for e in ",".join(values).split(",") if e.strip()]
    if len(entries) < hops:
        return peer
    try:
        return str(ipaddress.ip_address(entries[-hops]))
    except ValueError:
        return peer


def _server_credential(token: str) -> bool:
    """Treg's own server-side tokens: the admin and blocklist tokens and the house-team tokens the
    deployment uses to call itself. Compared against settings, never trusted from a header."""
    s = get_settings()
    return any(secret and hmac.compare_digest(token.encode(), secret.encode())
               for secret in (s.admin_token, s.blocklist_admin_token, s.onboarding_treg_token,
                              s.jev_treg_token))


OVERLOAD_REASON = "overload_cap"
STREAM_OVERLOAD_REASON = "overload_cap_stream"


def _mcp_stream(scope) -> bool:
    """Only `GET /mcp...` is long-lived (the server-to-client stream). An MCP POST is one
    exchange even when it accepts `text/event-stream`, so it takes a main permit."""
    path = scope.get("path", "")
    return scope.get("method") == "GET" and (path == "/mcp" or path.startswith("/mcp/"))


def _overload_exempt(path: str) -> bool:
    return path in get_settings().rate_limit_exempt_path_set or path == "/admin" or path.startswith("/admin/")


class RateLimitMiddleware:
    def __init__(self, app):
        self.app = app
        self._overload: asyncio.Semaphore | None = None
        self._stream_cap: asyncio.Semaphore | None = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get(request_scope.INTERNAL):
            # treg calling itself (an MCP tool's /call/, a hub check): the outer request already
            # holds this process's permit and is already counted in flight.
            return await self._admit(scope, receive, send)
        inflight.enter()
        try:
            await self._capped(scope, receive, send)
        finally:
            inflight.leave()

    async def _capped(self, scope, receive, send):
        s = get_settings()
        if not s.overload_cap_enabled or _overload_exempt(scope.get("path", "")):
            return await self._admit(scope, receive, send)
        stream = _mcp_stream(scope)
        if stream:
            if self._stream_cap is None:
                self._stream_cap = asyncio.Semaphore(s.overload_cap_stream_size)
            sem, reason = self._stream_cap, STREAM_OVERLOAD_REASON
        else:
            if self._overload is None:
                self._overload = asyncio.Semaphore(s.overload_cap_size)
            sem, reason = self._overload, OVERLOAD_REASON
        if sem.locked():  # full: refuse now, never wait for a permit
            metrics.count("overload_stream_rejects" if stream else "overload_rejects")
            return await _reject(send, _Overloaded(s.overload_cap_retry_after_s, reason))
        await sem.acquire()  # cannot block: no await between locked() and here
        try:
            # A pure ASGI app returns only after the last body chunk (a stream included) is sent,
            # or the client went away, or it raised or was cancelled: the permit is held until then.
            await self._admit(scope, receive, send)
        finally:
            sem.release()

    async def _admit(self, scope, receive, send):
        slot: dict = {}
        scope[request_scope.RATE_LIMIT] = slot
        internal = scope.get(request_scope.INTERNAL)
        path = scope.get("path", "")
        token = request_scope.credential(path, Headers(scope=scope))  # what this route's auth honors
        trusted = bool(token and _server_credential(token))
        facts = RequestFacts(
            ip=client_ip(scope),
            path=path,
            key_hash=request_scope.key_hash(token) if token else None,
            org_header=_header(scope, b"x-treg-org"),
            alias=(internal or {}).get("alias") or None,
            exempt=bool(internal) or trusted or path in get_settings().rate_limit_exempt_path_set,
            trusted=trusted,
        )
        rl = limiter()
        status = 500
        admission = None

        async def tracked(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            admission = await rl.admit(facts, slot)
            if admission.rejection is not None:
                return await _reject(tracked, admission.rejection)
            await self.app(scope, receive, tracked)
        finally:
            if admission is not None:
                # Shielded: a cancelled request (anyio cancels every await in its scope, finally
                # blocks included) must still give back its leases.
                with anyio.CancelScope(shield=True):
                    await rl.finish(admission, slot, status=status)


class _Overloaded:
    status = 503

    def __init__(self, retry_after_s: int, reason: str = OVERLOAD_REASON):
        self.headers = ((b"retry-after", str(retry_after_s).encode()),
                        (b"x-treg-overload-reason", reason.encode()))
        self.detail = {"error": "overloaded", "reason": reason, "retry_after_s": retry_after_s}


async def _reject(send, rejection) -> None:
    body = json.dumps({"detail": rejection.detail}, separators=(",", ":")).encode()
    await send({"type": "http.response.start", "status": rejection.status, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *rejection.headers,
    ]})
    await send({"type": "http.response.body", "body": body})
