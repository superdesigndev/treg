"""Team-owned Lens profiles and single-consumption paid quotes.

The database owns the claim. In-process tasks survive browser disconnects; an owner lost to a
process shutdown is interrupted, never put back into a queue or submitted again.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update
from sqlmodel import select

from .. import crypto
from ..config import get_settings
from ..domain import lens as rules
from ..infra.db import session_maker
from ..models import LensProject, LensRun, Org, ROLE_RANK
from ..timeutil import utcnow_naive as now
from . import lens_execution
from .call.types import CallerSnapshot

log = logging.getLogger(__name__)
RUN_SECONDS = 900
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
_owners: dict[str, asyncio.Task] = {}


def _release_owner(run_id, task):
    if _owners.get(run_id) is task:
        _owners.pop(run_id, None)
    if not task.cancelled() and task.exception() is not None:
        # A storage outage may also prevent recording interruption. The durable deadline remains
        # the recovery boundary; consume the exception without logging SQL/provider arguments.
        log.error("event=lens_owner_lost run_id=%s", run_id)


def _pack(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if len(raw.encode()) > MAX_PAYLOAD_BYTES:
        raise rules.LensError("The Lens result exceeded the saved-result limit.", 413)
    return crypto.encrypt(raw)


def _unpack(value: str) -> dict:
    return json.loads(crypto.decrypt(value))


def _iso(value):
    return value.isoformat() + "Z" if value is not None else None


def _date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def require_access(caller, *, write=False):
    if not get_settings().lens_enabled:
        raise rules.LensError("Lens is not enabled.", 404)
    member = caller.membership
    if (member.pinned_tags or member.project_access is not None or member.tool_access is not None
            or caller.org.public_demo):
        raise rules.LensError("Lens requires an unrestricted team membership.", 403)
    if write and ROLE_RANK.get(caller.role, -1) < ROLE_RANK["member"]:
        raise rules.LensError("A team member must approve changes and paid Lens runs.", 403)


def _can_edit(caller, row):
    return ROLE_RANK.get(caller.role, -1) >= ROLE_RANK["admin"] or (
        caller.role == "member" and row.user_id == caller.user.id)


async def _lock_team(db, org_id):
    result = await db.execute(update(Org).where(Org.id == org_id).values(id=Org.id))
    if not result.rowcount:
        raise rules.LensError("This team is no longer available.", 404)


async def _project(db, caller, project_id):
    row = (await db.execute(select(LensProject).where(
        LensProject.id == project_id, LensProject.org_id == caller.org_id))).scalar_one_or_none()
    if row is None:
        raise rules.LensError("Brand project not found.", 404)
    return row


async def _run_row(db, caller, run_id):
    row = (await db.execute(select(LensRun).where(
        LensRun.id == run_id, LensRun.org_id == caller.org_id))).scalar_one_or_none()
    if row is None:
        raise rules.LensError("Lens run not found.", 404)
    return row


def _project_view(row, caller):
    return {**_unpack(row.payload), "id": row.id, "revision": row.revision,
            "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
            "can_edit": _can_edit(caller, row)}


def _quote_view(row, payload):
    plan = payload["plan"]
    return {"id": row.id, "run_id": row.id, "kind": row.kind, "input": plan["input"],
            "total_cost_micro": plan["estimate_micro"], "expires_at": _iso(row.expires_at),
            "note": plan.get("note", ""), "currency": "USD",
            "project_id": row.project_id, "project_revision": row.project_revision}


def _run_view(row):
    payload = _unpack(row.payload)
    return {"id": row.id, "kind": row.kind, "project_id": row.project_id,
            "state": row.state, "created_at": _iso(row.created_at),
            "started_at": _iso(row.started_at), "completed_at": _iso(row.completed_at),
            "result": payload.get("result"), "error": payload.get("error"),
            "quote": _quote_view(row, payload)}


async def list_projects(caller):
    require_access(caller)
    async with session_maker() as db:
        rows = (await db.execute(select(LensProject).where(
            LensProject.org_id == caller.org_id).order_by(
            LensProject.updated_at.desc(), LensProject.id).limit(50))).scalars().all()
        return {"projects": [_project_view(row, caller) for row in rows]}


async def create_project(caller, data, request_id):
    require_access(caller, write=True)
    profile = rules.normalize_project(data)
    fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    async with session_maker() as db:
        await _lock_team(db, caller.org_id)
        prior = (await db.execute(select(LensProject).where(
            LensProject.org_id == caller.org_id, LensProject.request_id == request_id))).scalar_one_or_none()
        if prior:
            if prior.fingerprint != fingerprint or prior.user_id != caller.user.id:
                raise rules.LensError("This save identifier was already used for another profile.", 409)
            return _project_view(prior, caller)
        count = await db.scalar(select(func.count()).select_from(LensProject).where(
            LensProject.org_id == caller.org_id))
        if count >= 50:
            raise rules.LensError("This team has reached its 50 brand-project limit.", 409)
        row = LensProject(id=uuid.uuid4().hex, org_id=caller.org_id, user_id=caller.user.id,
                          request_id=request_id, fingerprint=fingerprint, payload=_pack(profile))
        db.add(row)
        await db.commit()
        return _project_view(row, caller)


async def update_project(caller, project_id, data, revision):
    require_access(caller, write=True)
    profile = rules.normalize_project(data)
    async with session_maker() as db:
        await _lock_team(db, caller.org_id)
        row = await _project(db, caller, project_id)
        if not _can_edit(caller, row):
            raise rules.LensError("Only the project creator or a team admin can edit this profile.", 403)
        changed = await db.execute(update(LensProject).where(
            LensProject.id == project_id, LensProject.org_id == caller.org_id,
            LensProject.revision == revision).values(
                payload=_pack(profile), revision=revision + 1, updated_at=now()))
        if not changed.rowcount:
            raise rules.LensError("This brand profile changed. Reload before saving your edits.", 409)
        await db.commit()
        await db.refresh(row)
        return _project_view(row, caller)


def _interrupted(payload):
    result = payload.get("result")
    if result is not None:
        result["cost_known"] = False
        result["total_cost_micro"] = None
        for attempt in result.get("attempts", []):
            if attempt.get("state") == "running":
                attempt.update(state="unknown", charged_micro=None,
                               detail="Execution was interrupted; this call will not be repeated.")
    payload["error"] = "This run was interrupted. Review its saved results and billing before starting a new run."
    return payload


async def _recover(db, row):
    # A GET may have loaded a snapshot while an owner was persisting its next receipt. Lock and
    # refresh before changing terminal state so recovery cannot overwrite newer paid evidence.
    if not ((row.state == "quoted" and row.expires_at <= now()) or
            (row.state == "running" and row.deadline_at is not None and row.deadline_at <= now())):
        return
    await db.execute(update(LensRun).where(LensRun.id == row.id).values(state=LensRun.state))
    await db.refresh(row)
    state = None
    if row.state == "quoted" and row.expires_at <= now():
        state = "expired"
    elif row.state == "running" and row.deadline_at is not None and row.deadline_at <= now():
        state = "interrupted"
    if state is None:
        return
    payload = _unpack(row.payload)
    if state == "interrupted":
        payload = _interrupted(payload)
    changed = await db.execute(update(LensRun).where(
        LensRun.id == row.id, LensRun.state == row.state).values(
            state=state, payload=_pack(payload), completed_at=now()))
    if changed.rowcount:
        await db.commit()
        await db.refresh(row)


async def get_project(caller, project_id):
    require_access(caller)
    async with session_maker() as db:
        project = await _project(db, caller, project_id)
        rows = (await db.execute(select(LensRun).where(
            LensRun.project_id == project_id, LensRun.org_id == caller.org_id,
            LensRun.state != "quoted").order_by(
            LensRun.created_at.desc(), LensRun.id).limit(50))).scalars().all()
        for row in rows:
            await _recover(db, row)
        return {"project": _project_view(project, caller), "runs": [_run_view(row) for row in rows]}


async def quote(caller, *, kind, url=None, project_id=None, project_revision=None, engines=None):
    require_access(caller, write=True)
    if kind == "onboarding":
        if project_id is not None or project_revision is not None or engines is not None or not url:
            raise rules.LensError("Brand research requires only a website URL.", 422)
        inputs = {"url": url}
    elif kind == "audit":
        if url is not None or not project_id or project_revision is None:
            raise rules.LensError("An audit requires a saved brand project and its revision.", 422)
        async with session_maker() as db:
            project = await _project(db, caller, project_id)
            if project.revision != project_revision:
                raise rules.LensError("This brand profile changed. Reload before getting a quote.", 409)
            inputs = _unpack(project.payload)
        if engines is not None:
            inputs["engines"] = engines
    else:
        raise rules.LensError("Choose brand research or an audit.", 422)
    # Planning may inspect provider credentials/prices, with its own short transactions.
    # The persistence transaction begins only after that work has finished.
    plan = await lens_execution.quote(caller, kind, inputs)
    async with session_maker() as db:
        await _lock_team(db, caller.org_id)
        if project_id:
            project = await _project(db, caller, project_id)
            if project.revision != project_revision:
                raise rules.LensError("This brand profile changed. Reload before getting a quote.", 409)
        recent = await db.scalar(select(func.count()).select_from(LensRun).where(
            LensRun.org_id == caller.org_id, LensRun.created_at > now() - timedelta(hours=1)))
        if recent >= 100:
            raise rules.LensError("This team has created 100 Lens quotes this hour. Try again later.", 429)
        row = LensRun(id=uuid.uuid4().hex, org_id=caller.org_id, user_id=caller.user.id,
                      project_id=project_id, project_revision=project_revision, kind=kind,
                      expires_at=_date(plan["expires_at"]), payload=_pack({"plan": plan}))
        db.add(row)
        await db.commit()
        return _quote_view(row, {"plan": plan})


async def get_run(caller, run_id):
    require_access(caller)
    async with session_maker() as db:
        row = await _run_row(db, caller, run_id)
        await _recover(db, row)
        return _run_view(row)


async def start(caller, quote_id, http, client_ip):
    require_access(caller, write=True)
    async with session_maker() as db:
        await _lock_team(db, caller.org_id)
        row = await _run_row(db, caller, quote_id)
        await _recover(db, row)
        if row.state != "quoted":
            return _run_view(row)
        if row.user_id != caller.user.id:
            raise rules.LensError("Only the person who requested this quote can approve it.", 403)
        if row.project_id:
            project = await _project(db, caller, row.project_id)
            if project.revision != row.project_revision:
                raise rules.LensError("This brand profile changed. Get a fresh audit quote.", 409)
        active = await db.scalar(select(func.count()).select_from(LensRun).where(
            LensRun.org_id == caller.org_id, LensRun.state == "running", LensRun.deadline_at > now()))
        if active >= 3:
            raise rules.LensError("Three Lens runs are already active for this team.", 429)
        claim = await db.execute(update(LensRun).where(
            LensRun.id == quote_id, LensRun.org_id == caller.org_id, LensRun.state == "quoted").values(
                state="running", started_at=now(), deadline_at=now() + timedelta(seconds=RUN_SECONDS)))
        plan = _unpack(row.payload)["plan"]
        await db.commit()
        await db.refresh(row)
        response = _run_view(row)
    if claim.rowcount:
        task = asyncio.create_task(_execute(quote_id, caller.org_id, CallerSnapshot.capture(caller),
                                            plan, http, client_ip))
        _owners[quote_id] = task
        task.add_done_callback(lambda finished: _release_owner(quote_id, finished))
    return response


async def _persist(run_id, org_id, result=None, *, state=None, error=None):
    async with session_maker() as db:
        await db.execute(update(LensRun).where(
            LensRun.id == run_id, LensRun.org_id == org_id).values(state=LensRun.state))
        row = (await db.execute(select(LensRun).where(
            LensRun.id == run_id, LensRun.org_id == org_id))).scalar_one_or_none()
        if row is None or row.state not in {"running", "interrupted"}:
            return False
        payload = _unpack(row.payload)
        if result is not None:
            payload["result"] = result
        if row.state == "interrupted" or (row.deadline_at is not None and row.deadline_at <= now()):
            state = "interrupted"
        if state == "interrupted":
            payload = _interrupted(payload)
        elif error is not None:
            payload["error"] = error
        row.payload = _pack(payload)
        if state:
            row.state = state
            row.completed_at = now()
        db.add(row)
        await db.commit()
        return row.state == "running"


async def _execute(run_id, org_id, caller, plan, http, client_ip):
    async def persist(result):
        return await _persist(run_id, org_id, result)
    try:
        async with asyncio.timeout(RUN_SECONDS):
            result = await lens_execution.execute(caller, plan, run_id, http, client_ip, persist=persist)
        await _persist(run_id, org_id, result, state="completed")
    except (asyncio.CancelledError, TimeoutError):
        await asyncio.shield(_persist(run_id, org_id, state="interrupted"))
    except rules.LensError as exc:
        await _persist(run_id, org_id, state="failed", error=str(exc))
    except Exception:
        # No exception repr: provider responses or credentials must never reach product logs.
        log.error("event=lens_execution_failed run_id=%s", run_id)
        await asyncio.shield(_persist(run_id, org_id, state="interrupted"))


async def shutdown():
    tasks = list(_owners.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
