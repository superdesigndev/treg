"""The tool hub's maker routes (docs/HUB-DECISIONS.md).

Behind TREG_HUB_ENABLED: with the flag off every route here answers 404, so the surface does not
exist until the whole hub lands. Phase 1: publish (validate + store) and read back. The check run,
versions and the run road arrive with the runner.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
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


@app.post("/hub/tools", status_code=201)
async def publish_hub_tool(
    body: PublishIn, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    _require_hub()
    _require_can_register(caller)
    try:
        published = await hub_app.publish(
            db, org=caller.org, maker_email=caller.email,
            manifest=body.manifest, script=body.script, check=body.check, readme=body.readme,
        )
    except ManifestError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "manifest_invalid", "field": exc.field, "rule": exc.rule,
        }) from None
    await db.commit()
    return {
        "tool_id": published.tool_id, "version": published.version, "status": published.status,
        "kind": published.kind,
        "note": "stored and validated; the check run that makes a version live arrives with the runner",
    }


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
