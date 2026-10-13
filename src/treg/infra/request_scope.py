"""ASGI scope keys and credential extraction shared by the rate-limit middleware, auth and MCP.

Scope keys, not headers: a network client can send any header, but only code in this process
can put a key into the ASGI scope, so `INTERNAL` is a server-side credential rather than a claim.
"""

from __future__ import annotations

from collections.abc import Mapping

from .. import crypto

# A dict the rate-limit middleware puts in the scope. Auth reads `want_caller` and writes `caller`
# (with the hash of the credential it actually authenticated) and calls `check_org`.
RATE_LIMIT = "treg.rate_limit"
# Set by `internal_app` on requests treg makes to itself (MCP tools, hub checks).
INTERNAL = "treg.internal"


def treg_token(headers: Mapping) -> str:
    """The `X-Treg-Token` value, the first one, exactly as the API's auth dependencies read it."""
    return headers.get("x-treg-token") or ""


def bearer_token(headers: Mapping) -> str:
    """The `Authorization: Bearer` value, exactly as the MCP surfaces read it."""
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    return raw.removeprefix("Bearer ").removeprefix("bearer ").strip()


def is_mcp(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def credential(path: str, headers: Mapping) -> str:
    """The one credential the route's auth honors: the Bearer on the MCP surfaces, `X-Treg-Token`
    everywhere else. Nothing else a request carries identifies its caller."""
    return bearer_token(headers) if is_mcp(path) else treg_token(headers)


def key_hash(token: str) -> str:
    return crypto.hash_token(token)


def team_key(key: str, org_header: str) -> str:
    """The cache identity of an unpinned credential (an identity token whose team the request's
    `X-Treg-Org` chooses, which auth honors for it). Pinned credentials use the key hash alone."""
    return crypto.hash_token(f"{key}\n{org_header}")


def internal_app(app, *, outer_token: str = ""):
    """Wrap treg's own ASGI app for an in-process client. The outer request was already counted;
    `alias` lets the inner request's auth result also teach the key cache the outer credential."""
    marker = {"alias": key_hash(outer_token) if outer_token else ""}

    async def wrapped(scope, receive, send):
        if scope.get("type") == "http":
            scope = {**scope, INTERNAL: marker}
        await app(scope, receive, send)

    return wrapped
