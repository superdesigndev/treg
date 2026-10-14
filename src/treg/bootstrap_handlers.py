"""Application-wide HTTP exception adapters owned by the composition boundary."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import analytics
from .call_surface import split_call_path
from .caller_metadata import _client_of
from .client_identity import _norm_client


# create_app supplies the call-specific compensation callback before registering these adapters.
_stamp_call_exit: Any = None


def _capture_call_intake_failure(request: Request, *, surface: str) -> None:
    """Report a call-shaped request that failed before treg could resolve its caller."""
    user_agent = request.headers.get("user-agent", "")
    analytics.capture("treg-server", "call_intake_failed", {
        "status_code": 503,
        "outcome": "gateway_failed",
        "failure_kind": "db_pool",
        "phase": "caller_identity",
        "surface": surface,
        "client": _client_of(request),
        "method": request.method,
        "user_agent": user_agent[:100],
        "ua_family": _norm_client(user_agent),
    })


async def _pool_saturated(request: Request, exc: PoolTimeoutError) -> JSONResponse:
    """The DB pool had no connection to give within `pool_timeout` (db.py). That is treg being
    saturated, not the caller's fault and not the provider's — so say so, typed, and fast. Before this
    handler the same condition escaped request handling and surfaced as a bare
    `500 Internal Server Error` after a 30 s wait, which an agent cannot tell from a provider bug.
    `treg_saturated` is the key a retrying client should branch on; `Retry-After` is how long to wait
    before doing so."""
    analytics.capture_fault(exc, component="db_pool")
    resp = JSONResponse(
        {"detail": "treg's database pool is saturated — retry in a moment", "treg_saturated": True},
        status_code=503, headers={"Retry-After": "2"})
    # A saturation 503 is answered HERE, not through `_mark_treg_own_errors`, so it needs its own
    # join key, row and label release. Without them the one failure mode a burst actually produces
    # (#181) is the one a caller cannot report and `/calls` cannot show. `X-Treg-Error` stays off:
    # the typed `treg_saturated` flag above is this exit's signal, and the header is documented as
    # the HTTPException handler's (interface/api.md).
    if call_path := split_call_path(request.url.path):
        if hasattr(request.state, "call_identity"):
            await _stamp_call_exit(request, resp, 503, failure_kind="db_pool")
        else:
            _capture_call_intake_failure(request, surface=call_path[0])
            await _stamp_call_exit(request, resp, 503)
    return resp

async def _mark_treg_own_errors(request: Request, exc: StarletteHTTPException):
    """Tag treg's own call-surface refusals with `X-Treg-Error`, then answer as before.

    A caller cannot otherwise tell a treg 404 ("no tool registered for that host") from the vendor's
    own 404 — both are a status code and some JSON. The local proxy needs that distinction to explain
    a failure without ever rewriting a real vendor response, and an agent reading a raw 403 needs to
    know whether to fix its request or ask an admin. The header is only ever ADDED; the status and the
    body are untouched, and a client that ignores it sees exactly what it saw before."""
    resp = await http_exception_handler(request, exc)
    if split_call_path(request.url.path):
        resp.headers["X-Treg-Error"] = "1"
        # Refusals that raised before the handler's own audit ran (bad token, unknown tool, ACL,
        # deny rule, daily cap) would otherwise leave NO row, and no id to report — the funnel's
        # early friction was invisible until this. Here because this is the ONE place every refusal
        # passes through; the handler has a dozen raise points and stamping at each would be a dozen
        # chances to miss one.
        await _stamp_call_exit(request, resp, exc.status_code)
    return resp


# Preserve the frozen composition snapshot until the Stage 4 close-out refreshes module paths.
_pool_saturated.__module__ = "treg.api"
_mark_treg_own_errors.__module__ = "treg.api"
