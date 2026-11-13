"""Application-wide HTTP middleware owned by the composition boundary."""

from __future__ import annotations

import base64
import gzip
import zlib
from urllib.parse import urlsplit

import anyio
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import Headers, MutableHeaders

from . import client_notice
from .config import LEGACY_PUBLIC_HOSTS, get_settings
from .domain.identity import session as sess


# The pre-treg.to hostnames must keep answering the API forever — every installed CLI, skill.md
# and .mcp.json in the wild points here with a Bearer token, and most HTTP clients STRIP the
# Authorization header when a redirect crosses hosts (and some MCP clients follow no redirects at
# all). So only browser-facing marketing pages redirect to the canonical host; everything else —
# /call/, /mcp/, auth flows, webhooks, agent-fetched pages like /vendor-listing, install scripts
# fetched by `curl | sh` without -L — is served in place on both hosts.
_LEGACY_HOSTS = set(LEGACY_PUBLIC_HOSTS)
# Marketing pages — but only for ANONYMOUS visitors. A session cookie is host-scoped, so bouncing a
# signed-in browser to the canonical host silently logs it out mid-flow (the invite confirmation,
# for one, sets a legacy-host session and then lands on `/?invite_org=…`).
# robots.txt and sitemap.xml join them for a search-engine reason rather than a marketing one: the
# sitemap names canonical `public_url` URLs, and a sitemap whose own address is on a different host
# than the URLs inside it is cross-submission — a crawler is entitled to ignore the lot. Redirecting
# both means the legacy name resolves to one crawlable site, not a duplicate of it.
_REDIRECT_PATHS = {"/", "/login", "/terms", "/privacy", "/support", "/contact", "/help",
                   "/tutorial", "/robots.txt", "/sitemap.xml", "/catalog"}
# The auth ENTRY points redirect unconditionally, and that is a correctness fix, not a marketing
# one: each parks a host-scoped cookie and then continues on `public_url` — started on the legacy
# host, the continuation never sees the cookie. /auth/github + /auth/google set the CSRF state
# cookie the provider callback must find ("Bad state" otherwise); GET /oauth/authorize, signed out,
# parks the whole authorization request in `treg_oauth_return` and sends the browser through `/` to
# sign in. Exact paths only; the /callback routes (and POST /oauth/authorize, the consent approval)
# must keep serving in place — the middleware only touches GET/HEAD.
_REDIRECT_ALWAYS = {"/auth/github", "/auth/google", "/oauth/authorize"}


class _LegacyHostRedirectMiddleware:
    """Redirect marketing pages (301) and auth entries (302) from a legacy host to the canonical
    host. Auth entries get a temporary redirect: their URLs carry one-shot OAuth parameters, and a
    cached permanent answer is exactly the wrong thing to keep."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        host = request.headers.get("host", "").split(":")[0].rstrip(".").lower()
        if request.method in ("GET", "HEAD") and host in _LEGACY_HOSTS:
            path = request.url.path
            always = path in _REDIRECT_ALWAYS
            if always or (path in _REDIRECT_PATHS and sess.COOKIE not in request.cookies):
                canonical = get_settings().public_url.rstrip("/")
                # hostname equality, not substring: a self-hoster whose public_url IS a legacy host
                # must keep serving in place, but "not-treg.superdesign.dev" must not.
                if host != ((urlsplit(canonical).hostname or "").rstrip(".").lower()):
                    target = canonical + path
                    if request.url.query:
                        target += "?" + request.url.query
                    response = RedirectResponse(target, status_code=302 if always else 301)
                    return await response(scope, receive, send)
        return await self.app(scope, receive, send)


class _SecurityHeadersMiddleware:
    """The dashboard is an authenticated app; ship the baseline hardening headers it was missing —
    nosniff, clickjacking protection (X-Frame-Options), and a tight Referrer-Policy. `setdefault`
    so the /call proxy's own stricter CSP/nosniff isn't clobbered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message, headers=list(message.get("headers", [])))
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                # HSTS pins the browser to https so a spoofed X-Forwarded-Proto can't downgrade the
                # session cookie onto cleartext (browsers ignore it over http, so dev is unaffected).
                headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        return await self.app(scope, receive, send_with_security_headers)


class _ClientNoticeMiddleware:
    """Answers a CLI that sends `X-Treg-Client-Version` with `X-Treg-Client-Notice` when the
    operator's `cli_latest_version` or `cli_min_version` says it is behind (client_notice.py)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        settings = get_settings()
        if not (settings.cli_latest_version or settings.cli_min_version):
            return await self.app(scope, receive, send)
        version = next((v.decode("latin-1") for k, v in scope.get("headers", [])
                        if k == b"x-treg-client-version"), "")
        text = client_notice.notice(version, latest=settings.cli_latest_version,
                                    minimum=settings.cli_min_version)
        if not text:
            return await self.app(scope, receive, send)

        async def send_with_notice(message):
            if message["type"] == "http.response.start":
                message = dict(message, headers=list(message.get("headers", [])))
                MutableHeaders(scope=message)[client_notice.NOTICE_HEADER] = text
            await send(message)

        return await self.app(scope, receive, send_with_notice)


class _DevTitleMiddleware:
    """On a local dev server, every page's `<title>` starts with "[dev] ", so its browser tab is told
    apart from treg.to's at a glance. Registered only when `Settings.local_dev` holds; it buffers an
    HTML response to rewrite it, and leaves every other response alone."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start: dict | None = None
        body: list[bytes] = []

        async def send_marked(message):
            nonlocal start
            if message["type"] == "http.response.start":
                ctype = MutableHeaders(scope=message).get("content-type", "")
                if not ctype.startswith("text/html"):
                    return await send(message)
                start = message
                return None
            if start is None:
                return await send(message)
            body.append(message.get("body", b""))
            if message.get("more_body"):
                return None
            html = b"".join(body).replace(b"<title>", b"<title>[dev] ", 1)
            headers = MutableHeaders(scope=dict(start, headers=list(start.get("headers", []))))
            if "content-length" in headers:
                headers["content-length"] = str(len(html))
            await send(dict(start, headers=headers.raw))
            await send({"type": "http.response.body", "body": html})

        return await self.app(scope, receive, send_marked)


_GZIP_TYPES = ("text/", "application/json", "application/x-ndjson", "application/javascript",
               "application/xml", "image/svg+xml")
_GZIP_MIN_BYTES = 1024
_GZIP_LEVEL = 4
_GZIP_THREAD_BYTES = 64 * 1024
_GZIP_THREADS = anyio.CapacityLimiter(4)


def _accepts_gzip(scope) -> bool:
    for key, value in scope["headers"]:
        if key == b"accept-encoding":
            for item in value.decode("latin-1").lower().replace(" ", "").split(","):
                coding, _, q = item.partition(";q=")
                if coding == "gzip":
                    try:
                        return float(q or 1) > 0
                    except ValueError:
                        return False
    return False


def _gzippable(status: int, headers: Headers) -> bool:
    ctype = headers.get("content-type", "").lower()
    length = headers.get("content-length", "")
    return (status not in (204, 206, 304)
            and not (length.isdigit() and int(length) < _GZIP_MIN_BYTES)
            and "content-encoding" not in headers
            and "content-range" not in headers
            and "no-transform" not in headers.get("cache-control", "").lower()
            and not ctype.startswith("text/event-stream")
            and (ctype.startswith(_GZIP_TYPES) or "+json" in ctype or "+xml" in ctype))


def _deflate_now(encoder, body: bytes, more: bool) -> bytes:
    return encoder.compress(body) + encoder.flush(zlib.Z_SYNC_FLUSH if more else zlib.Z_FINISH)


async def _deflate(encoder, body: bytes, more: bool) -> bytes:
    if len(body) < _GZIP_THREAD_BYTES:
        return _deflate_now(encoder, body, more)
    return await anyio.to_thread.run_sync(_deflate_now, encoder, body, more, limiter=_GZIP_THREADS)


class _GzipResponseMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _accepts_gzip(scope):
            return await self.app(scope, receive, send)
        start: dict | None = None
        encoder = None

        async def send_gzipped(message):
            nonlocal start, encoder
            if message["type"] == "http.response.start":
                if _gzippable(message["status"], Headers(raw=message.get("headers", []))):
                    start = message
                    return None
                return await send(message)
            if start is None and encoder is None:
                return await send(message)
            body, more = message.get("body", b""), message.get("more_body", False)
            if encoder is None:
                begin, start = dict(start, headers=list(start.get("headers", []))), None
                if message["type"] != "http.response.body" or (not more and len(body) < _GZIP_MIN_BYTES):
                    await send(begin)
                    return await send(message)
                encoder = zlib.compressobj(_GZIP_LEVEL, zlib.DEFLATED, 31)
                data = await _deflate(encoder, body, more)
                headers = MutableHeaders(scope=begin)
                headers["content-encoding"] = "gzip"
                headers.add_vary_header("Accept-Encoding")
                if headers.get("etag", "").startswith('"'):
                    headers["etag"] = "W/" + headers["etag"]
                if more:
                    del headers["content-length"]
                else:
                    headers["content-length"] = str(len(data))
                await send(begin)
                return await send({"type": "http.response.body", "body": data, "more_body": more})
            data = await _deflate(encoder, body, more)
            return await send({"type": "http.response.body", "body": data, "more_body": more})

        return await self.app(scope, receive, send_gzipped)


_BODY_ENC_HEADER = b"x-treg-body-encoding"


def _decode_request_body(raw: bytes, enc: str) -> bytes:
    """Undo the transforms named in `enc` (left to right; `+`/`,`-separated). Supports `base64` and
    `gzip`, combinable (e.g. `base64+gzip` = base64-decode then gunzip). This lets a client smuggle a
    body whose plaintext (SQL, HTML) would otherwise trip an upstream WAF that inspects request bodies
    -- the edge sees only opaque base64, the server restores the real bytes before any route reads them."""
    out = raw
    for step in (s.strip().lower() for s in enc.replace(",", "+").split("+") if s.strip()):
        if step == "base64":
            out = base64.b64decode(out)
        elif step == "gzip":
            out = gzip.decompress(out)
        else:
            raise ValueError(f"unsupported body encoding: {step}")
    return out


class _BodyDecodeMiddleware:
    """Pure-ASGI: when a request carries `X-Treg-Body-Encoding`, decode the body before routing. The
    JSON endpoints (Pydantic re-reads the decoded body) and the /call proxy (which relays
    request.body() upstream) then both see the real bytes. No-op for requests without the header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        enc = next((v.decode("latin-1") for k, v in scope["headers"] if k == _BODY_ENC_HEADER), None)
        if enc is None:
            return await self.app(scope, receive, send)
        chunks: list[bytes] = []
        consumed_messages = []
        body_complete = False
        while True:
            msg = await receive()
            consumed_messages.append(msg)
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    body_complete = True
                    break
            else:
                break

        # A decoder needs the complete encoded payload. If the client disconnected mid-body, pass
        # the messages already observed through unchanged so downstream sees the real disconnect.
        if not body_complete:
            async def replay_incomplete():
                if consumed_messages:
                    return consumed_messages.pop(0)
                return await receive()

            return await self.app(scope, replay_incomplete, send)
        raw = b"".join(chunks)
        try:
            decoded = _decode_request_body(raw, enc)
        except Exception:  # noqa: BLE001 -- a malformed encoded body is a client error, not a 500
            return await JSONResponse({"detail": "invalid X-Treg-Body-Encoding body"}, status_code=400)(scope, receive, send)
        # Strip the marker, drop content-encoding, and fix content-length to the decoded size.
        headers = [(k, v) for k, v in scope["headers"]
                   if k not in (_BODY_ENC_HEADER, b"content-length", b"content-encoding")]
        headers.append((b"content-length", str(len(decoded)).encode("latin-1")))
        new_scope = dict(scope, headers=headers)
        delivered = False

        async def receive_decoded():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": decoded, "more_body": False}
            return await receive()

        return await self.app(new_scope, receive_decoded, send)


for _middleware in (
    _LegacyHostRedirectMiddleware,
    _SecurityHeadersMiddleware,
    _BodyDecodeMiddleware,
):
    _middleware.__module__ = "treg.api"
