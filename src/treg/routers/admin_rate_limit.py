"""Admin control of request admission: the policy, the blocklist, and what each process serves.

Config writes need a super-admin. Blocklist writes also accept `blocklist_admin_token`, a credential
that opens the block, unblock, list and status endpoints and nothing else
(docs/context/architecture/rate-limit.md). The blocklist mode and the policy need a super-admin.
Every write route is hard-off (404, before auth) until its token is configured:
`blocklist_admin_token` for block and unblock, `admin_token` for the mode and the policy. The write API is expected to move to treg-internal.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..application.ratelimit import admin as rl_admin
from ..application.ratelimit import background, blocklist_admin, config_state
from ..config import get_settings
from ..domain.identity.access import require_blocklist_writer, require_superadmin
from ..infra import kv
from ..infra.db import get_admin_session

router = APIRouter()


def _blocklist_writes_on() -> None:
    """Hard-off without a configured `blocklist_admin_token`: the route answers 404 before auth."""
    if not get_settings().blocklist_admin_token:
        raise HTTPException(status_code=404, detail="Not Found")


def _config_writes_on() -> None:
    """Hard-off without a configured `admin_token`: the route answers 404 before auth."""
    if not get_settings().admin_token:
        raise HTTPException(status_code=404, detail="Not Found")


class ConfigWrite(BaseModel):
    config: dict
    note: str = Field(min_length=1, max_length=500)
    enforce_approval: str | None = Field(default=None, max_length=500)
    # The version this write was based on; a different current version is a 409, not a lost update.
    base_version: int | None = None


class RevertIn(BaseModel):
    note: str = Field(default="", max_length=500)


class BlockTarget(BaseModel):
    type: str
    value: str


class BlockIn(BaseModel):
    entries: list[BlockTarget]
    reason: str
    approved_by: str
    evidence: dict
    ttl_s: int | None = None


class ModeIn(BaseModel):
    mode: str
    approved_by: str
    reason: str


class UnblockIn(BaseModel):
    approved_by: str
    reason: str


async def _live() -> list[dict]:
    if not kv.configured():
        return [background.process_status()]
    try:
        rows = await kv.store().rl_live()
    except kv.Unavailable:
        return []
    out = []
    for raw in rows:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return sorted(out, key=lambda r: r.get("process_instance", ""))


@router.get("/admin/rate-limit/config", include_in_schema=False)
async def admin_rate_limit_config(
    _: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    rows = await rl_admin.history(db)
    return {"current": rows[0] if rows else None, "history": rows,
            "this_process": background.process_status()}


@router.put("/admin/rate-limit/config", include_in_schema=False, dependencies=[Depends(_config_writes_on)])
async def admin_rate_limit_config_write(
    body: ConfigWrite, principal: str = Depends(require_superadmin),
    db: AsyncSession = Depends(get_admin_session),
) -> dict:
    try:
        return await rl_admin.write(db, body.config, actor=principal, note=body.note,
                                    enforce_approval=body.enforce_approval,
                                    base_version=body.base_version)
    except config_state.InvalidConfig as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_config", "errors": exc.errors})
    except rl_admin.VersionConflict as exc:
        raise HTTPException(status_code=409, detail={"error": "version_conflict",
                                                     "current_version": exc.current})


@router.post("/admin/rate-limit/config/revert-to-shadow", include_in_schema=False, dependencies=[Depends(_config_writes_on)])
async def admin_rate_limit_revert_to_shadow(
    body: RevertIn | None = None, principal: str = Depends(require_superadmin),
    db: AsyncSession = Depends(get_admin_session),
) -> dict:
    return await rl_admin.revert_to_shadow(db, actor=principal, note=body.note if body else "")


@router.post("/admin/rate-limit/config/republish", include_in_schema=False, dependencies=[Depends(_config_writes_on)])
async def admin_rate_limit_republish(
    principal: str = Depends(require_superadmin), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    try:
        return await rl_admin.republish(db, actor=principal)
    except rl_admin.NothingToPublish:
        raise HTTPException(status_code=404, detail="no rate-limit config has been written")


@router.get("/admin/rate-limit/status", include_in_schema=False)
async def admin_rate_limit_status(_: str = Depends(require_blocklist_writer)) -> dict:
    """Every serving process's config and blocklist version and mode, from its heartbeat."""
    return {"this_process": background.process_status(), "processes": await _live()}


@router.get("/admin/blocklist", include_in_schema=False)
async def admin_blocklist(
    _: str = Depends(require_blocklist_writer), db: AsyncSession = Depends(get_admin_session),
) -> dict:
    return await blocklist_admin.listing(db)


@router.get("/admin/blocklist/status", include_in_schema=False)
async def admin_blocklist_status(
    version: int | None = None, _: str = Depends(require_blocklist_writer),
    db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """The readback an operator tool polls after a write: `live` is true once every process with a
    fresh heartbeat serves at least `version` (Postgres is the only copy; there is nothing between)."""
    pg_version = (await blocklist_admin.listing(db))["version"]
    processes = await _live()
    want = pg_version if version is None else version
    serving = [p.get("blocklist_version") or 0 for p in processes]
    return {
        "pg_version": pg_version,
        "processes": [{k: p.get(k) for k in ("process_instance", "blocklist_version", "blocklist_entries",
                                              "blocklist_loaded", "blocklist_mode")} for p in processes],
        "live": bool(serving) and min(serving) >= want,
    }


@router.post("/admin/blocklist", include_in_schema=False, dependencies=[Depends(_blocklist_writes_on)])
async def admin_blocklist_block(
    body: BlockIn, principal: str = Depends(require_blocklist_writer),
    db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """Block unpaid, new orgs or their keys, with a TTL. 400 names every guard a call failed."""
    # A fixed hourly window in the shared store; an unanswerable store refuses (fails closed).
    window = int(time.time() // 3600)
    if not await kv.store().take(f"rl:blocklist-writes:{window}", get_settings().blocklist_writes_per_hour, 3600):
        raise HTTPException(status_code=429, detail={"error": "blocklist_write_budget",
                                                     "message": "too many blocklist writes this hour"},
                            headers={"Retry-After": str(3600 - int(time.time()) % 3600)})
    try:
        return await blocklist_admin.block(
            db, actor=principal, entries=[t.model_dump() for t in body.entries], reason=body.reason,
            approved_by=body.approved_by, evidence=body.evidence, ttl_s=body.ttl_s)
    except blocklist_admin.Rejected as exc:
        raise HTTPException(status_code=400, detail={"error": "rejected", "errors": exc.errors})


@router.post("/admin/blocklist/{entry_id}/unblock", include_in_schema=False, dependencies=[Depends(_blocklist_writes_on)])
async def admin_blocklist_unblock(
    entry_id: int, body: UnblockIn, principal: str = Depends(require_blocklist_writer),
    db: AsyncSession = Depends(get_admin_session),
) -> dict:
    try:
        return await blocklist_admin.unblock(db, entry_id, actor=principal,
                                             approved_by=body.approved_by, reason=body.reason)
    except blocklist_admin.Rejected as exc:
        raise HTTPException(status_code=400, detail={"error": "rejected", "errors": exc.errors})
    except blocklist_admin.UnknownEntry:
        raise HTTPException(status_code=404, detail="no such blocklist entry")


@router.post("/admin/blocklist/mode", include_in_schema=False, dependencies=[Depends(_config_writes_on)])
async def admin_blocklist_mode(
    body: ModeIn, principal: str = Depends(require_superadmin),
    db: AsyncSession = Depends(get_admin_session),
) -> dict:
    """off | log_only | enforce_403, with version + 1 and an audit row in one transaction.
    Super-admin only, for every target mode: the blocklist token can add and remove entries but
    never decides whether they refuse requests. Hard-off, like the policy writes, without
    `admin_token`."""
    try:
        return await blocklist_admin.set_mode(db, body.mode, actor=principal,
                                              approved_by=body.approved_by, reason=body.reason)
    except blocklist_admin.Rejected as exc:
        raise HTTPException(status_code=400, detail={"error": "rejected", "errors": exc.errors})
