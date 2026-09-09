"""The tool hub's maker routes (docs/HUB-DECISIONS.md).

Behind TREG_HUB_ENABLED: with the flag off every route here answers 404, so the surface does not
exist until the whole hub lands. Phase 1: publish (validate + store) and read back. The check run,
versions and the run road arrive with the runner.
"""

from __future__ import annotations

from typing import Any

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import hub as hub_app
from ..domain.hub import ManifestError
from ..domain.identity.access import Caller, _require_can_register, require_member
from ..infra.db import get_session
from ..models import HubTool

app = APIRouter()


def _require_hub() -> None:
    if not hub_app.enabled():
        raise HTTPException(status_code=404, detail="Not Found")


class PublishIn(BaseModel):
    """The four files a maker ships, as fields. Same shape as the folder, one request."""

    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any]
    script: str | None = Field(default=None, max_length=200_000)
    check: dict[str, Any]
    readme: str = Field(min_length=1, max_length=4000)


async def _publish(body: PublishIn, request: Request, caller: Caller, db: AsyncSession,
                   *, must_exist: bool) -> dict:
    _require_hub()
    _require_can_register(caller)
    if must_exist:
        name = body.manifest.get("name") if isinstance(body.manifest, dict) else None
        exists = (await db.execute(select(HubTool.id).where(
            HubTool.org_id == caller.org_id, HubTool.name == name).limit(1))).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail=f"your team has no hub tool named {name!r}; POST /hub/tools creates one")
    try:
        published = await hub_app.publish(
            db, org=caller.org, maker_email=caller.email,
            manifest=body.manifest, script=body.script, check=body.check, readme=body.readme,
        )
    except ManifestError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "manifest_invalid", "field": exc.field, "rule": exc.rule,
        }) from None
    # The row must be visible to the check run's own request before it starts: commit first.
    await db.commit()
    row = (await db.execute(select(HubTool).where(
        HubTool.tool_id == published.tool_id, HubTool.version == published.version))).scalars().one()
    verdict = await hub_app.run_check(db, row, maker_headers=dict(request.headers), app=request.app)
    await db.commit()
    out = {"tool_id": published.tool_id, "version": published.version, "status": row.status,
           "kind": published.kind, "check": verdict}
    if row.status == "live":
        out["call"] = f"POST /call/{published.tool_id}"
    return out


@app.post("/hub/tools", status_code=201)
async def publish_hub_tool(
    body: PublishIn, request: Request, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Create (or add a version to) a hub tool: validate the four files, store the version, run
    check.json once for real on the maker's balance. Live on pass; `failed` with the reason on
    fail — the version is kept so the maker can read what happened."""
    return await _publish(body, request, caller, db, must_exist=False)


@app.put("/hub/tools/{tool_id}")
async def update_hub_tool(
    tool_id: str, body: PublishIn, request: Request, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """A new version of an existing tool of yours (the same body as POST). The newest live
    version serves by default; `<id>@N` pins an older one for 30 days after a newer one lands."""
    base, _ = hub_app.split_id(tool_id)
    if isinstance(body.manifest, dict) and body.manifest.get("name") != base.split(".", 1)[-1]:
        raise HTTPException(status_code=422, detail={
            "error": "manifest_invalid", "field": "name",
            "rule": f"must be {base.split('.', 1)[-1]!r} to update {base!r}"})
    return await _publish(body, request, caller, db, must_exist=True)


@app.get("/hub/tools/mine")
async def my_hub_tools(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> list[dict]:
    _require_hub()
    rows = (await db.execute(
        select(HubTool).where(HubTool.org_id == caller.org_id)
        .order_by(HubTool.tool_id, HubTool.version.desc()))).scalars().all()
    return [hub_app.view(r) for r in rows]


@app.get("/hub/tools/{tool_id}")
async def get_hub_tool(
    tool_id: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """One version, the newest unless the id carries `@N`. Any status: the maker sees their own
    unchecked and failed versions; other teams see only live ones (the public page is phase 7)."""
    _require_hub()
    base, pin = hub_app.split_id(tool_id)
    q = select(HubTool).where(HubTool.tool_id == base)
    if pin is not None:
        q = q.where(HubTool.version == pin)
    row = (await db.execute(q.order_by(HubTool.version.desc()).limit(1))).scalars().first()
    if row is None or (row.org_id != caller.org_id and row.status != "live"):
        raise HTTPException(status_code=404, detail=f"no hub tool {tool_id!r}")
    out = hub_app.view(row)
    if row.org_id == caller.org_id:
        out["script"] = row.script
        out["check"] = row.check
        out["readme"] = row.readme
    return out


class RunIn(PublishIn):
    """A dry run of a folder: the four files plus the inputs. Nothing is stored; every step is
    real and charged to the maker (HUB-DECISIONS round 4 q10 / round 5 q2: `treg hub run .`)."""

    inputs: dict[str, Any] = Field(default_factory=dict)


@app.post("/hub/run")
async def run_hub_folder(
    body: RunIn, request: Request, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> Response:
    _require_hub()
    _require_can_register(caller)
    from ..application.call.service import create_call_context, execute_call
    from ..application.call.types import CallFailure, CallInput, CallerSnapshot
    from ..application.hub import limits as hub_limits
    from ..application.hub import runner as hub_runner
    from .auth import _client_ip
    from .call import _http_upstream_response, _translate_call_failure

    try:
        row = await hub_app.transient(db, org=caller.org, maker_email=caller.email,
                                      manifest=body.manifest, script=body.script,
                                      check=body.check, readme=body.readme)
    except ManifestError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "manifest_invalid", "field": exc.field, "rule": exc.rule}) from None
    payload = json.dumps(body.inputs).encode()
    call_input = CallInput(
        method="POST", raw_rest=f"{row.tool_id}@0",
        raw_headers=tuple((k, v) for k, v in request.headers.raw if k.lower() not in (b"content-length", b"content-type")),
        query_items=(), raw_query="", body=_Bytes(payload),
        caller=CallerSnapshot.capture(caller), client_ip=_client_ip(request), catalog_only=False)
    context = create_call_context(call_input)
    await db.commit()
    try:
        with hub_limits.slot(caller.org_id):
            response, charged = await hub_runner.run_hub_tool(
                context, row, payload, request.headers.get, request.app.state.http, execute_call,
                audit_client="hub-run")
    except hub_limits.TeamBusy as busy:
        raise HTTPException(status_code=429, detail={
            "error": "hub_busy", "active": busy.active, "max": hub_limits.MAX_RUNS_PER_TEAM,
            "retry_after_s": hub_limits.RETRY_AFTER_S}) from None
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc
    out = _http_upstream_response(response)
    out.headers["X-Treg-Cost-Micro"] = str(charged)
    out.headers["X-Treg-Call-Id"] = context.call_ref
    return out


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def stream(self):
        yield self._data

    async def read(self) -> bytes:
        return self._data
