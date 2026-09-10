"""The tool hub's maker routes (docs/HUB-DECISIONS.md).

Behind TREG_HUB_ENABLED: with the flag off every route here answers 404, so the surface does not
exist until the whole hub lands. Phase 1: publish (validate + store) and read back. The check run,
versions and the run road arrive with the runner.
"""

from __future__ import annotations

from typing import Any

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import case
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import hub as hub_app
from ..config import get_settings
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
        out["page"] = f"{get_settings().public_url.rstrip('/')}/hub/{published.tool_id}"
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
    """Every version of the team's tools, newest first, each with its derived health and the
    tool's last-30-day numbers (runs by others, earned) so the dashboard list needs one call."""
    _require_hub()
    from datetime import timedelta
    from sqlalchemy import func
    from ..application.hub import health as hub_health
    from ..models import HubRun
    from ..timeutil import utcnow_naive
    rows = (await db.execute(
        select(HubTool).where(HubTool.org_id == caller.org_id)
        .order_by(HubTool.tool_id, HubTool.version.desc()))).scalars().all()
    since = utcnow_naive() - timedelta(days=30)
    stats = {tid: {"runs_30d": int(n), "earned_30d_micro": int(e or 0)} for tid, n, e in (await db.execute(
        select(HubRun.tool_id, func.count(HubRun.id), func.coalesce(func.sum(HubRun.price_micro), 0))
        .where(HubRun.maker_org_id == caller.org_id, HubRun.caller_org_id != caller.org_id,
               HubRun.version > 0, HubRun.started_at >= since).group_by(HubRun.tool_id))).all()}
    out = []
    for r in rows:
        h = await hub_health.health_of(db, r.tool_id, r.version, r.check_result)
        out.append({**hub_app.view(r), "health": h.state, "fails_in_a_row": h.fails_in_a_row,
                    "last_run_at": h.last_run_at,
                    **stats.get(r.tool_id, {"runs_30d": 0, "earned_30d_micro": 0})})
    return out


@app.get("/hub/runs/{run_id}")
async def hub_run(
    run_id: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """One finished run. The CALLER's team sees what it paid, the inputs, the trace and the
    output. The MAKER's team sees the run of its tool: inputs (secret inputs masked), the trace,
    the script's log lines and the failure's error body, never the caller's identity or the
    output (docs/HUB-DECISIONS.md round 2 q7, round 5 q8, q10). Anyone else: 404."""
    _require_hub()
    from ..application.hub import health as hub_health
    from ..models import HubRun
    row = (await db.execute(select(HubRun).where(HubRun.run_id == run_id))).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="no such run")
    is_caller = row.caller_org_id == caller.org_id
    is_maker = row.maker_org_id == caller.org_id
    if not (is_caller or is_maker):
        raise HTTPException(status_code=404, detail="no such run")
    out = {"run_id": row.run_id, "tool_id": row.tool_id, "version": row.version, "status": row.status,
           "started_at": row.started_at.isoformat(), "finished_at": row.finished_at.isoformat() if row.finished_at else None,
           "duration_ms": row.duration_ms, "steps": row.steps, "inputs": row.inputs, "trace": row.trace,
           "usage": {"cost_micro": row.cost_micro + row.price_micro, "steps_micro": row.cost_micro,
                     "price_micro": row.price_micro},
           "you_are": "caller" if is_caller else "maker",
           "kind": ("scheduled check" if row.caller_email == hub_health.CHECK_EMAIL else
                    "maker's run" if row.caller_org_id == row.maker_org_id else "caller's run")}
    if is_caller:
        out["output"] = row.output
        out["caller_email"] = row.caller_email
        # the caller never reads an upstream error body: strip it from every trace entry
        out["trace"] = [{k: v for k, v in s.items() if k != "error"} for s in (row.trace or [])]
        if row.error:
            e = dict(row.error)
            # a caller sees the failing step's name, status and the tool's own name, never the
            # upstream error body (round 4 q10); the maker reads the full error in their log
            e.pop("log", None)
            for s in e.get("trace", []) or []:
                s.pop("error", None)
            out["error"] = e
    if is_maker:
        out["trace"] = row.trace
        out["log"] = row.log
        out["error"] = row.error
    return out


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


@app.get("/hub/tools/{tool_id}/earnings")
async def hub_tool_earnings(
    tool_id: str, days: int = 90, format: str = "json",
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> Response:
    """The seller's view (docs/HUB-DECISIONS.md round 3 q10, round 5 q9): per day, runs,
    successes, failures and what was earned, for one of the team's tools. Counts and amounts only;
    never who called. `format=csv` for a download."""
    _require_hub()
    from datetime import timedelta
    from sqlalchemy import func
    from ..models import HubRun
    from ..timeutil import utcnow_naive
    base, _ = hub_app.split_id(tool_id)
    owned = (await db.execute(select(HubTool.id).where(
        HubTool.tool_id == base, HubTool.org_id == caller.org_id).limit(1))).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status_code=404, detail=f"your team has no hub tool {base!r}")
    days = max(1, min(days, 365))
    since = utcnow_naive() - timedelta(days=days)
    day = func.date(HubRun.started_at)
    rows = (await db.execute(
        select(day.label("day"), func.count(HubRun.id),
               func.sum(case((HubRun.status == "ok", 1), else_=0)),
               func.sum(case((HubRun.status != "ok", 1), else_=0)),
               func.coalesce(func.sum(HubRun.price_micro), 0))
        # Sales only: the maker's own runs (the publish check, their own tests) carry no price and
        # would inflate the run count of a view that answers "what did this tool sell".
        .where(HubRun.tool_id == base, HubRun.maker_org_id == caller.org_id,
               HubRun.caller_org_id != caller.org_id,
               HubRun.started_at >= since, HubRun.version > 0)
        .group_by(day).order_by(day.desc()))).all()
    table = [{"day": str(d), "runs": int(n), "ok": int(ok or 0), "failed": int(bad or 0),
              "earned_micro": int(earned)} for d, n, ok, bad, earned in rows]
    total = sum(r["earned_micro"] for r in table)
    if format == "csv":
        lines = ["day,runs,ok,failed,earned_usd"] + [
            f"{r['day']},{r['runs']},{r['ok']},{r['failed']},{r['earned_micro'] / 1e6:.6f}" for r in table]
        return Response("\n".join(lines) + "\n", media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{base}-earnings.csv"'})
    return JSONResponse({"tool_id": base, "days": days, "earned_micro": total,
                         "runs": sum(r["runs"] for r in table), "by_day": table})


@app.delete("/hub/tools/{tool_id}")
async def retire_hub_tool(
    tool_id: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Retire every version of one of your team's tools: off the call road at once, the rows kept
    (earnings and run history stay readable)."""
    _require_hub()
    _require_can_register(caller)
    base, _ = hub_app.split_id(tool_id)
    n = await hub_app.retire(db, org_id=caller.org_id, tool_id=base)
    if n == 0:
        raise HTTPException(status_code=404, detail=f"your team has no live hub tool {base!r}")
    await db.commit()
    return {"tool_id": base, "status": "retired", "versions": n}


class PriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_usd: float


@app.patch("/hub/tools/{tool_id}")
async def set_hub_tool_price(
    tool_id: str, body: PriceIn, caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Change the seller's price of the newest live version; applies to later runs, no version bump."""
    _require_hub()
    _require_can_register(caller)
    base, _ = hub_app.split_id(tool_id)
    try:
        row = await hub_app.set_price(db, org_id=caller.org_id, tool_id=base, price_usd=body.price_usd)
    except ManifestError as exc:
        raise HTTPException(status_code=422, detail={"error": "manifest_invalid", "field": exc.field, "rule": exc.rule}) from None
    if row is None:
        raise HTTPException(status_code=404, detail=f"your team has no live hub tool {base!r}")
    await db.commit()
    return {"tool_id": base, "version": row.version, "price_usd": row.price_micro / 1_000_000}


@app.get("/hub/tools/{tool_id}/health")
async def hub_tool_health(
    tool_id: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The maker's health view: the derived state, the last check, and the last runs."""
    _require_hub()
    from ..application.hub import health as hub_health
    from ..models import HubRun
    base, pin = hub_app.split_id(tool_id)
    q = select(HubTool).where(HubTool.tool_id == base, HubTool.org_id == caller.org_id)
    if pin is not None:
        q = q.where(HubTool.version == pin)
    row = (await db.execute(q.order_by(HubTool.version.desc()).limit(1))).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"your team has no hub tool {base!r}")
    h = await hub_health.health_of(db, row.tool_id, row.version, row.check_result)
    runs = (await db.execute(select(HubRun).where(HubRun.tool_id == row.tool_id, HubRun.version == row.version)
                             .order_by(HubRun.started_at.desc()).limit(20))).scalars().all()
    return {"tool_id": row.tool_id, "version": row.version, "status": row.status,
            "health": h.state, "fails_in_a_row": h.fails_in_a_row, "last_run_at": h.last_run_at,
            "last_check": row.check_result,
            "runs": [{"run_id": r.run_id, "at": r.started_at.isoformat(), "status": r.status,
                      "kind": "scheduled check" if r.caller_email == hub_health.CHECK_EMAIL else
                              ("maker's run" if r.caller_org_id == r.maker_org_id else "caller's run"),
                      "steps": r.steps, "cost_micro": r.cost_micro, "price_micro": r.price_micro,
                      "ms": r.duration_ms, "error": (r.error or {}).get("error") if r.error else None}
                     for r in runs]}
