"""HTTP adapter for `/table/<tool id>`: the same call as `/call/<tool id>`, answered as rows and columns.

Only the ending differs from `/call/`: the call runs through `run_call_surface` (the same gates, key
injection, hold and settle, audit row and `Idempotency-Key`), then `application.table` reads the
answer and converts it. Behind `TREG_TABLE_ENABLED` (+ `TREG_TABLE_TEAMS` / `TREG_TABLE_USERS`).
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import table as table_app
from ..domain.identity.access import Caller, require_member
from ..infra.db import get_session
from ..infra import org_traffic
from .call import run_call_surface

app = APIRouter()
router = app


async def table_caller(
    request: Request,
    authorization: str = Header(default=""),
    x_treg_token: str = Header(default=""),
    x_treg_org: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> Caller:
    """The caller of a table route: a treg key or session as everywhere (`require_member`), or a
    "treg for Sheets" OAuth access token (`Authorization: Bearer`, audience `<base>/table`).

    The OAuth token is exchanged here, the way MCP exchanges its own (`mcp._internal_auth`): checked,
    then presented to `require_member` as a two-minute identity for the person it names, with the team
    from `X-Treg-Org` (else the token's default team). So membership, role, suspension and every team
    rule are the same checks a key gets. Only the table routes accept it: an MCP token has another
    audience and fails here; this token fails on MCP and on every other route."""
    bearer = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
    if bearer and not x_treg_token and not treg_session:
        from ..domain.identity import mcp_oauth, session
        from ..models import Org, User
        claims = mcp_oauth.read_access_token(bearer, expected_audience=mcp_oauth.sheets_resource_url())
        user = await db.get(User, claims["sub"]) if claims else None
        if claims is None or user is None or user.suspended or user.token_version != claims["tv"]:
            raise HTTPException(status_code=401, detail="invalid or expired access token",
                                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        team = x_treg_org
        if not team:
            org = await db.get(Org, claims["org"])
            team = org.slug if org is not None else ""
        # The token must never reach the provider: /table/ passes caller headers on, as /call/ does.
        request.state.table_oauth = True
        identity = session.make_identity(user.id, token_version=user.token_version, ttl=120)
        return await require_member(request, x_treg_token=identity, x_treg_org=team,
                                    treg_session="", db=db)
    return await require_member(request, x_treg_token=x_treg_token, x_treg_org=x_treg_org,
                                treg_session=treg_session, db=db)


async def _table_answer(request: Request, context, upstream, rest: str) -> Response:
    status, body, headers = await table_app.table_answer(context, upstream, rest)
    response = JSONResponse(body, status_code=status)
    for name, value in headers:
        response.headers[name.decode("latin-1")] = value.decode("latin-1")
    return response


@app.api_route(
    "/table/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def table_tool(
    rest: str,
    request: Request,
    caller: Caller = Depends(table_caller),
):
    """One call, answered as `{shape, columns, rows, ...}` (docs/context/architecture/table.md)."""
    org_traffic.identify(request.scope, caller.org_id)
    if not table_app.enabled_for(caller.org.slug, caller.email):
        # Answered here, not raised: a raised 404 on a call surface is stamped and audited as a
        # refusal, and with the flag off this route must look like it does not exist.
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await run_call_surface(rest, request, caller, prefix="/table/", finish=_table_answer,
                                  headers=table_app.plain_headers(
                                      tuple(request.headers.raw),
                                      drop_authorization=bool(getattr(request.state, "table_oauth", False))))


@app.get("/table-columns/{tool_id:path}", include_in_schema=False)
async def table_columns(tool_id: str, caller: Caller = Depends(table_caller)):
    """The columns `/table/<tool id>` will answer with, free: no provider call, no charge, no audit
    row. A separate path, not `/table/...?preview=1`: `/table/` passes every query parameter to the
    provider, as `/call/` does."""
    if not table_app.enabled_for(caller.org.slug, caller.email):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    view = await table_app.preview(tool_id, org_id=caller.org_id, org_slug=caller.org.slug, email=caller.email)
    if view is None:
        return JSONResponse({"error": "no_preview", "tool_id": tool_id,
                             "detail": "treg has no contract, manifest or saved example for this tool"},
                            status_code=404)
    return view


@app.get("/table-account", include_in_schema=False)
async def table_account(request: Request, caller: Caller = Depends(table_caller),
                        db: AsyncSession = Depends(get_session)):
    """Who is signed in and which teams can pay, with their balances: the add-on's panel header and
    team switch. With a "treg for Sheets" token: every team of the person. With a team key: that one
    team (a team key speaks for one team only)."""
    if not table_app.enabled_for(caller.org.slug, caller.email):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    every = bool(getattr(request.state, "table_oauth", False))
    return {"email": caller.email, "active_team": caller.org.slug,
            "teams": await table_app.account_teams(db, user_id=caller.user.id,
                                                   only_org_id=None if every else caller.org_id)}

