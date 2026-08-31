"""Application-wide HTTP middleware owned by the composition boundary."""

from __future__ import annotations

import base64
import gzip
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import MutableHeaders

from .config import LEGACY_PUBLIC_HOSTS, PUBLIC_HOST_ALIASES, get_settings
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


# The qualified names are a frozen composition-snapshot and import compatibility surface. Retire
# when the Stage 4 close-out deliberately refreshes the composition snapshot to the truthful module paths.
for _middleware in (
    _LegacyHostRedirectMiddleware,
    _SecurityHeadersMiddleware,
    _BodyDecodeMiddleware,
):
    _middleware.__module__ = "treg.api"
