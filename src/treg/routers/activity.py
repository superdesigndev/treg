"""A team's activity: its calls, server runs and local runs, as rows and as one paged feed.

`/calls` and `/runs` (in `api.py`) serve each history on its own. `/activity` is the dashboard's
feed: the three sources merged newest first and paged by one cursor, so a page never leaves a gap
that a later page fills in above rows already shown.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlmodel import select

from ..application import asynctasks as async_task_app
from ..domain.governance.access import pinned_tag_predicates
from ..domain.identity.access import Caller, require_member
from ..infra.db import get_session
from ..models import CallRecord, RunRecord

app = APIRouter()

# Audit kinds that are not a call of their own: owned free polling of an async task, and a local
# run, which is audited as its GRANT and shown as a run.
NOT_A_CALL = ("async_poll", "local_run")

# The feed's order is (created_at, rank, id), newest first. Rank breaks a timestamp tie between
# sources, whose ids share no space; the letter is the prefix a run's id already carries.
_RANK = {"c": 2, "l": 1, "s": 0}


def async_charged(c: CallRecord, task: dict | None) -> int | None:
    """What hit the balance, once the task record is known: nothing yet while pending, the settled
    figure at a terminal state. Without a task record the audit row's own column stands."""
    if task is None:
        return c.cost_charged_micro
    return None if task["status"] == "pending" else task["settled_micro"]


def call_row(c: CallRecord, task: dict | None) -> dict:
    return {
        "id": c.id,
        "user_email": c.user_email,
        "tool_name": c.tool_name,
        "method": c.method,
        "path": c.path,
        "status_code": c.status_code,
        "kind": c.kind,
        "client": c.client,
        "api_key_id": c.api_key_id,
        "api_key_name": c.api_key_name,
        "api_key_prefix": c.api_key_prefix,
        # Marketplace telemetry — all null for a plain tool call (see models.CallRecord). Kept in
        # the same row a caller already reads, so "what did this cost me" needs no second endpoint.
        "endpoint_id": c.endpoint_id,
        "provider": c.provider,
        "credential_tier": c.credential_tier,
        # The archive answered instead of the vendor; the money columns are still identical to
        # a live call on purpose (docs/context/architecture/archive.md).
        "cached": c.cached,
        # The archive holds this call's answer: `GET /calls/{id}/result` can show it. False
        # for own-key/own-tool calls (never stored), failures, and calls made while recording
        # was off.
        "has_result": c.archive_key_hash is not None,
        "cost_estimated_micro": c.cost_estimated_micro,
        "cost_observed_micro": c.cost_observed_micro,
        "cost_charged_micro": async_charged(c, task),
        "duration_ms": c.duration_ms,
        "response_bytes": c.response_bytes,
        "params_hash": c.params_hash,
        # non-null = treg said no before anything went upstream (see models.CallRecord) — the
        # one field that tells "the provider failed" apart from "we refused" in `treg audit`.
        "refused_by": c.refused_by,
        # The caller's own tags (X-Treg-Meta), for a builder reconciling this row against their
        # records. Money is NOT invoiced from here — see the ledger-backed usage endpoint.
        "call_ref": c.call_ref,
        "budget_dim": c.budget_dim,
        "budget_val": c.budget_val,
        "tags": c.tags,
        "created_at": c.created_at.isoformat(),
        # Present only on a metered async submission: settlement state, and the artifact once the
        # task succeeded (a time-limited URL, or the CLI command that retrieves it).
        "async_task": task,
    }


def server_run_row(r: RunRecord) -> dict:
    return {"id": f"s{r.id}", "user_email": r.user_email, "tool": r.bundle_name,  # bundle_name = tool (historical)
            "argv": r.argv, "exit_code": r.exit_code, "duration_ms": r.duration_ms,
            "where": "server", "client": r.client, "api_key_id": r.api_key_id,
            "api_key_name": r.api_key_name, "api_key_prefix": r.api_key_prefix,
            "created_at": r.created_at.isoformat()}


def local_run_row(c: CallRecord) -> dict:
    """A local run is audited as its GRANT (kind="local_run"); the redacted argv lives in `path`.
    Local successes carry no exit code (only failures report back), so `exit_code` is null."""
    return {"id": f"l{c.id}", "user_email": c.user_email, "tool": c.tool_name,
            "argv": (c.path or "").split(), "exit_code": None, "duration_ms": None,
            "where": "local", "client": c.client, "api_key_id": c.api_key_id,
            "api_key_name": c.api_key_name, "api_key_prefix": c.api_key_prefix,
            "created_at": c.created_at.isoformat()}


def _parse_cursor(before: str) -> tuple[datetime, str, int]:
    try:
        at, source, row_id = before.rsplit("~", 2)
        cursor = datetime.fromisoformat(at), source, int(row_id)
    except ValueError:
        cursor = None
    if cursor is None or cursor[1] not in _RANK or cursor[0].tzinfo is not None:
        raise HTTPException(status_code=400, detail="before is not a cursor this feed returned")
    return cursor


def _older(created_at, row_id, source: str, cursor: tuple[datetime, str, int] | None) -> list:
    """Rows of `source` that come after `cursor` in the feed's newest-first order."""
    if cursor is None:
        return []
    at, cursor_source, cursor_id = cursor
    if _RANK[source] < _RANK[cursor_source]:
        return [created_at <= at]
    if _RANK[source] > _RANK[cursor_source]:
        return [created_at < at]
    # `created_at <= at` is implied by the pair, and is there for the index: an OR alone is not a
    # range the planner can seek to, so it would filter every newer row of the team instead.
    return [created_at <= at, or_(created_at < at, row_id < cursor_id)]


@app.get("/activity")
async def activity_feed(
    limit: int = 100, before: str | None = None, api_key_id: int | None = None,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """This team's calls and runs, newest first, `limit` at a time. `next` is the cursor for the
    page after this one (pass it as `before`), null on the last page. Analytics, not an invoice:
    see `/calls`."""
    limit = max(1, min(limit, 500))
    cursor = _parse_cursor(before) if before is not None else None
    pins = caller.membership.pinned_tags
    calls = (select(CallRecord)
             .options(defer(CallRecord.error_request), defer(CallRecord.error_response))
             .where(CallRecord.org_id == caller.org_id, CallRecord.kind.notin_(NOT_A_CALL),
                    *pinned_tag_predicates(CallRecord.tags, pins),
                    *_older(CallRecord.created_at, CallRecord.id, "c", cursor))
             .order_by(CallRecord.created_at.desc(), CallRecord.id.desc()))
    local = (select(CallRecord)
             .where(CallRecord.org_id == caller.org_id, CallRecord.kind == "local_run",
                    CallRecord.method == "GRANT", *pinned_tag_predicates(CallRecord.tags, pins),
                    *_older(CallRecord.created_at, CallRecord.id, "l", cursor))
             .order_by(CallRecord.created_at.desc(), CallRecord.id.desc()))
    server = (select(RunRecord)
              .where(RunRecord.org_id == caller.org_id, *pinned_tag_predicates(RunRecord.tags, pins),
                     *_older(RunRecord.created_at, RunRecord.id, "s", cursor))
              .order_by(RunRecord.created_at.desc(), RunRecord.id.desc()))
    if api_key_id is not None:
        calls = calls.where(CallRecord.api_key_id == api_key_id)
        local = local.where(CallRecord.api_key_id == api_key_id)
        server = server.where(RunRecord.api_key_id == api_key_id)
    # One more than a page from each source: the merged page is exact, and anything past it means
    # there is a next page.
    found = [(r.created_at, "c", r.id, r) for r in (await db.execute(calls.limit(limit + 1))).scalars()]
    found += [(r.created_at, "l", r.id, r) for r in (await db.execute(local.limit(limit + 1))).scalars()]
    found += [(r.created_at, "s", r.id, r) for r in (await db.execute(server.limit(limit + 1))).scalars()]
    found.sort(key=lambda f: (f[0], _RANK[f[1]], f[2]), reverse=True)
    page = found[:limit]
    await db.close()  # release the request session before terminal archive object I/O
    tasks = await async_task_app.views_for(
        caller.org_id, [r.call_ref for _, s, _, r in page if s == "c" and r.call_ref
                        and r.credential_tier == "platform"], pinned_tags=pins)
    rows = [({"source": "call"} | call_row(r, tasks.get(r.call_ref))) if s == "c"
            else ({"source": "run"} | (local_run_row(r) if s == "l" else server_run_row(r)))
            for _, s, _, r in page]
    last = page[-1] if len(found) > limit else None
    return {"rows": rows, "next": f"{last[0].isoformat()}~{last[1]}~{last[2]}" if last else None}
