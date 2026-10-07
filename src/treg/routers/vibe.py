"""Vibe-it's routes (docs/context/architecture/vibe-it.md): the page, the conversations, the draft.

Behind TREG_VIBE_ENABLED and the hub's own flag and lists: off, or off for this person, every route
answers 404. A signed-in person in the browser only: an agent token is refused, because the model
budget is a person's, and an agent already has the hub's own routes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..application import vibe as vibe_app
from ..application.vibe import agent as vibe_agent
from ..application.vibe import status as vibe_status
from ..config import get_settings
from ..domain.identity.access import Caller, _require_can_register, require_member
from ..infra import db as database
from ..infra import kv
from ..infra.db import get_session
from ..models import Org, VibeSession
from .auth_helpers import _same_origin
from .web import page_entry

app = APIRouter()
log = logging.getLogger("treg.vibe")

MESSAGES_PER_MINUTE = 12
_tasks: set[asyncio.Task] = set()   # the agents at work in this process (held so they are not collected)


def _require_vibe(request: Request, caller: Caller) -> None:
    if not vibe_app.enabled_for(caller.org.slug, caller.email):
        raise HTTPException(status_code=404, detail="Not Found")
    if request.headers.get("x-treg-token") or not request.cookies.get("treg_session"):
        raise HTTPException(status_code=403, detail={"error": "vibe_browser_only",
                                                     "message": "vibe-it is used signed in, in the browser"})


def _require_same_origin(request: Request) -> None:
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


async def _owned(db: AsyncSession, caller: Caller, session_id: int):
    row = await vibe_app.of_user(db, user_id=caller.user.id, session_id=session_id)
    if row is None or row.org_id != caller.org_id:
        raise HTTPException(status_code=404, detail="no such conversation")
    return row


async def _view(db: AsyncSession, row) -> dict[str, Any]:
    return {**vibe_app.view_session(row), "draft": vibe_app.clean_draft(row.draft), "summary": row.summary,
            "pending": row.pending, "auto_test": bool(row.auto_test),
            "messages": [vibe_app.view_message(m) for m in await vibe_app.messages(db, row.id)]}


def _working() -> HTTPException:
    return HTTPException(status_code=409, detail={"error": "vibe_working", "message": "the agent is still working on this conversation"})


class NewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=vibe_app.TITLE_MAX)
    from_tool: str | None = Field(default=None, max_length=200)
    from_draft: int | None = None


class MetaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=vibe_app.TITLE_MAX)
    pinned: bool | None = None
    auto_test: bool | None = None


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(max_length=vibe_app.ATTACH_MAX)


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=vibe_app.TEXT_MAX)
    attachments: list[Attachment] = Field(default_factory=list, max_length=5)


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict[str, Any] | None = None
    script: str | None = Field(default=None, max_length=200_000)
    check: dict[str, Any] | None = None
    readme: str | None = Field(default=None, max_length=4000)
    data: str | None = Field(default=None, max_length=vibe_app.DATA_MAX)


class ActionIn(BaseModel):
    """A button the maker pressed. `password` goes to the app and nowhere else: never stored, never
    shown to the model."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["test", "publish", "app", "password", "skip"]
    inputs: dict[str, Any] | None = None
    name: str | None = Field(default=None, max_length=48)
    password: str | None = Field(default=None, max_length=128)
    clear: bool = False           # a password action with no password removes it only when this says so
    always: bool = False          # a test run: let the agent test-run without asking from now on


@app.get("/vibe-it", include_in_schema=False)
async def vibe_page():
    """The page itself; it asks the API who is signed in, and says so when vibe-it is off for them."""
    if not get_settings().vibe_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return page_entry("vibe")


@app.get("/vibe/state")
async def vibe_state(request: Request, caller: Caller = Depends(require_member),
                     db: AsyncSession = Depends(get_session)) -> dict:
    """The budget left, and this person's conversations in this team."""
    _require_vibe(request, caller)
    rows = await vibe_app.list_for(db, user_id=caller.user.id, org_id=caller.org_id)
    return {"team": caller.org.slug, "email": caller.email,
            "budget_micro": vibe_app.budget_micro(), "left_micro": await vibe_app.left_micro(db, caller.user.id),
            "sessions": [vibe_app.view_session(r) for r in rows]}


@app.post("/vibe/sessions", status_code=201)
async def vibe_new(body: NewIn, request: Request, caller: Caller = Depends(require_member),
                   db: AsyncSession = Depends(get_session)) -> dict:
    """A new conversation, empty, started from one of the team's published tools (`from_tool`), or
    continuing a copy of one of this person's unpublished drafts (`from_draft`, a conversation id)."""
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await vibe_app.create(db, user_id=caller.user.id, org_id=caller.org_id, title=body.title)
    if body.from_draft:
        source = await vibe_app.of_user(db, user_id=caller.user.id, session_id=body.from_draft)
        if source is None or source.org_id != caller.org_id or not vibe_app.clean_draft(source.draft):
            await db.rollback()
            raise HTTPException(status_code=404, detail="no such draft")
        await vibe_app.copy_draft(db, row, source)
        await vibe_app.update_meta(db, row, title=f"{source.title[:vibe_app.TITLE_MAX - 7]} (copy)")
        await vibe_app.add_message(db, row, "event", {"kind": "load", "ok": True,
                                                      "summary": f"Continuing a copy of the draft “{source.title}”"})
    elif body.from_tool:
        tool = await vibe_agent.load_tool(db, row, caller.org, body.from_tool)
        if tool is None:
            await db.rollback()
            raise HTTPException(status_code=404, detail=f"no hub tool {body.from_tool!r} in this team")
        await vibe_app.update_meta(db, row, title=tool.name)
        await vibe_app.add_message(db, row, "event", {"kind": "load", "ok": True, "tool_id": tool.tool_id,
                                                      "version": tool.version, "status": tool.status,
                                                      "summary": f"Loaded {tool.tool_id} v{tool.version} to edit"})
    await db.commit()
    return await _view(db, row)


@app.get("/vibe/sessions/{session_id}")
async def vibe_get(session_id: int, request: Request, caller: Caller = Depends(require_member),
                   db: AsyncSession = Depends(get_session)) -> dict:
    _require_vibe(request, caller)
    return await _view(db, await _owned(db, caller, session_id))


@app.patch("/vibe/sessions/{session_id}")
async def vibe_meta(session_id: int, body: MetaIn, request: Request, caller: Caller = Depends(require_member),
                    db: AsyncSession = Depends(get_session)) -> dict:
    """Rename, pin, or let the agent test-run without asking."""
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await _owned(db, caller, session_id)
    await vibe_app.update_meta(db, row, title=body.title, pinned=body.pinned, auto_test=body.auto_test)
    await db.commit()
    return {**vibe_app.view_session(row), "auto_test": bool(row.auto_test)}


@app.delete("/vibe/sessions/{session_id}")
async def vibe_delete(session_id: int, request: Request, caller: Caller = Depends(require_member),
                      db: AsyncSession = Depends(get_session)) -> dict:
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await _owned(db, caller, session_id)
    if vibe_app.running(row):
        raise _working()
    await vibe_app.remove(db, row)
    await db.commit()
    return {"deleted": session_id}


@app.get("/vibe/sessions/{session_id}/status")
async def vibe_tool_status(session_id: int, request: Request, caller: Caller = Depends(require_member),
                           db: AsyncSession = Depends(get_session)) -> dict:
    """The published tool's state, and what to fix before a test run."""
    _require_vibe(request, caller)
    row = await _owned(db, caller, session_id)
    tool = await vibe_status.tool_status(db, caller.org, row.tool_id)
    headers = vibe_agent.maker_headers(dict(request.headers))
    warn = await vibe_status.warnings(db, caller.org, vibe_app.clean_draft(row.draft),
                                      lambda eid: vibe_agent.access_of(request.app, headers, eid))
    return {"tool": tool, "warnings": warn}


def _stream(run: Callable[[Callable[[dict], Awaitable[None]]], Awaitable[None]], session_id: int,
            token: str) -> StreamingResponse:
    """Run the agent as its own task and stream what it does as newline-delimited JSON. The task
    outlives the request: a closed tab does not stop the agent, and the page picks the conversation
    up again from the database."""
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=5000)

    async def emit(event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass                        # nobody reading: the database has it all

    async def work() -> None:
        try:
            await run(emit)
        except Exception:  # noqa: BLE001 - the page must hear the end
            log.exception("vibe agent failed")
            await emit({"type": "error", "message": "The agent stopped on an error. Your files are saved."})
        finally:
            async with database.session_maker() as db:
                await vibe_app.release_run(db, session_id, token)
                await db.commit()
            await emit({"type": "done"})

    task = asyncio.create_task(work())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    async def lines():
        while True:
            event = await queue.get()
            yield json.dumps(event, default=str) + "\n"
            if event.get("type") == "done":
                return

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _start(db: AsyncSession, request: Request, caller: Caller, row: VibeSession) -> str:
    if not get_settings().ai_gateway_api_key:
        raise HTTPException(status_code=503, detail={"error": "vibe_no_model", "message": "no model is configured on this registry"})
    if not await kv.store().take(f"vibe:msg:{caller.user.id}", MESSAGES_PER_MINUTE, 60):
        raise HTTPException(status_code=429, detail={"error": "vibe_busy", "message": "too many messages; wait a minute"})
    token = await vibe_app.claim_run(db, row.id)
    if token is None:
        raise _working()
    await db.commit()
    return token


def _runner(request: Request, caller: Caller, session_id: int, token: str, **kw: Any):
    org_id, user_id, app_, headers = caller.org_id, caller.user.id, request.app, dict(request.headers)

    async def run(emit) -> None:
        async with database.session_maker() as db:
            row = await db.get(VibeSession, session_id)
            org = await db.get(Org, org_id)
            await vibe_agent.send(db, row, org, user_id=user_id, app=app_, headers=headers, emit=emit,
                                  run_token=token, **kw)
    return run


@app.post("/vibe/sessions/{session_id}/messages")
async def vibe_message(session_id: int, body: MessageIn, request: Request, caller: Caller = Depends(require_member),
                       db: AsyncSession = Depends(get_session)) -> StreamingResponse:
    """The maker says something; the agent works (searching, writing files, asking for a test run)
    until it answers. Streams newline-delimited JSON events: `message` (a stored turn, step or
    event), `delta` (the answer's text as it comes), `step` (a tool starting), `draft`, `pending`,
    `budget`, `error`, and `done` last."""
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await _owned(db, caller, session_id)
    if sum(len(a.text) for a in body.attachments) > vibe_app.ATTACH_MAX:
        raise HTTPException(status_code=413, detail={"error": "vibe_attachments_too_big",
                                                     "message": f"attachments: at most {vibe_app.ATTACH_MAX:,} characters"})
    token = await _start(db, request, caller, row)
    return _stream(_runner(request, caller, row.id, token, text=body.text,
                           attachments=[a.model_dump() for a in body.attachments]), row.id, token)


@app.post("/vibe/sessions/{session_id}/regenerate")
async def vibe_regenerate(session_id: int, request: Request, caller: Caller = Depends(require_member),
                          db: AsyncSession = Depends(get_session)) -> StreamingResponse:
    """The agent answers the maker's last message again: its answer and steps go, the files go back
    to how they were when the message was sent, and the maker's own actions stay. Streams as
    `messages` does."""
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await _owned(db, caller, session_id)
    last = await vibe_app.last_user_message(db, row.id)
    if last is None:
        raise HTTPException(status_code=409, detail={"error": "vibe_nothing_to_redo", "message": "there is no message to answer again"})
    token = await _start(db, request, caller, row)
    try:
        await vibe_app.drop_answer(db, row.id, last.id)
        await vibe_app.set_draft(db, row, await vibe_app.files_before(db, row, last.id), author="restore",
                                 note="back to before the regenerated answer", replace=True)
        await vibe_app.set_pending(db, row, None)
        await db.commit()
    except Exception:
        await db.rollback()
        await vibe_app.release_run(db, row.id, token)       # never leave the conversation marked busy
        await db.commit()
        raise
    return _stream(_runner(request, caller, row.id, token, text=None), row.id, token)


@app.post("/vibe/sessions/{session_id}/stop")
async def vibe_stop(session_id: int, request: Request, caller: Caller = Depends(require_member),
                    db: AsyncSession = Depends(get_session)) -> dict:
    """Ask the agent to stop; it does within a second or two, keeping what it already did."""
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await _owned(db, caller, session_id)
    await vibe_app.request_stop(db, row.id)
    await db.commit()
    return {"stopping": vibe_app.running(row)}


@app.post("/vibe/sessions/{session_id}/actions")
async def vibe_action(session_id: int, body: ActionIn, request: Request, caller: Caller = Depends(require_member),
                      db: AsyncSession = Depends(get_session)) -> dict:
    """A button: run a test, publish, turn on the app, set its password, or say not now. The same
    hub routes as everywhere, as the maker; the result is stored as an event the agent reads."""
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await _owned(db, caller, session_id)
    if body.kind == "password" and not body.password and not body.clear:
        # an empty password would remove the lock: a button that meant "set one" must never do that
        raise HTTPException(status_code=422, detail={"error": "vibe_password_missing",
                                                     "message": "type a password, or ask to remove it"})
    if body.kind == "test" and body.always:
        await vibe_app.update_meta(db, row, auto_test=True)
    token = await vibe_app.claim_run(db, row.id)
    if token is None:
        raise _working()
    await db.commit()
    try:
        event = await vibe_agent.act(db, row, caller.org, kind=body.kind, app=request.app, headers=dict(request.headers),
                                     inputs=body.inputs, name=body.name, password=body.password)
    finally:
        await vibe_app.release_run(db, row.id, token)
        await db.commit()
    await db.refresh(row)
    return {"event": event, "tool_id": row.tool_id, "auto_test": bool(row.auto_test)}


@app.put("/vibe/sessions/{session_id}/draft")
async def vibe_draft(session_id: int, body: DraftIn, request: Request, caller: Caller = Depends(require_member),
                     db: AsyncSession = Depends(get_session)) -> dict:
    """The maker's own edits to the files; the agent reads them on its next turn. An empty `data`
    removes data.csv. Refused while the agent works: its next write would be made over the files
    as it last read them, and the edit would be lost."""
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await _owned(db, caller, session_id)
    if vibe_app.running(row):
        raise _working()
    files = body.model_dump(exclude_none=True)
    if files.get("data") == "":
        files.pop("data")
        files = {**{k: v for k, v in vibe_app.clean_draft(row.draft).items() if k != "data"}, **files}
        v = await vibe_app.set_draft(db, row, files, author="maker", replace=True)
    else:
        v = await vibe_app.set_draft(db, row, files, author="maker")
    problem = await vibe_agent.validate(db, row, caller.org)
    await db.commit()
    return {"draft": vibe_app.clean_draft(row.draft), "problem": problem, "version": v.n if v else None}


@app.post("/vibe/sessions/{session_id}/validate")
async def vibe_validate(session_id: int, request: Request, caller: Caller = Depends(require_member),
                        db: AsyncSession = Depends(get_session)) -> dict:
    _require_vibe(request, caller)
    row = await _owned(db, caller, session_id)
    return {"problem": await vibe_agent.validate(db, row, caller.org)}


@app.get("/vibe/sessions/{session_id}/versions")
async def vibe_versions(session_id: int, request: Request, caller: Caller = Depends(require_member),
                        db: AsyncSession = Depends(get_session)) -> dict:
    """Every version of the files, newest first: who made it, and what it became when published."""
    _require_vibe(request, caller)
    row = await _owned(db, caller, session_id)
    return {"versions": [vibe_app.view_version(v) for v in await vibe_app.versions(db, row.id)]}


@app.get("/vibe/sessions/{session_id}/versions/{n}")
async def vibe_version(session_id: int, n: int, request: Request, caller: Caller = Depends(require_member),
                       db: AsyncSession = Depends(get_session)) -> dict:
    """One version's files and the version before it (the page draws the diff)."""
    _require_vibe(request, caller)
    row = await _owned(db, caller, session_id)
    v = await vibe_app.version(db, row.id, n)
    if v is None:
        raise HTTPException(status_code=404, detail="no such version")
    prev = await vibe_app.version(db, row.id, n - 1) if n > 1 else None
    return {**vibe_app.view_version(v, await vibe_app.files_of(db, v)),
            "prev": await vibe_app.files_of(db, prev) if prev else {}}


@app.post("/vibe/sessions/{session_id}/versions/{n}/restore")
async def vibe_restore(session_id: int, n: int, request: Request, caller: Caller = Depends(require_member),
                       db: AsyncSession = Depends(get_session)) -> dict:
    """Bring back version `n` as the newest version (history only grows)."""
    _require_vibe(request, caller)
    _require_can_register(caller)
    _require_same_origin(request)
    row = await _owned(db, caller, session_id)
    if vibe_app.running(row):
        raise _working()
    if await vibe_app.version(db, row.id, n) is None:
        raise HTTPException(status_code=404, detail="no such version")
    v = await vibe_app.restore(db, row, n)
    if v is not None:
        await vibe_app.add_message(db, row, "event", {"kind": "restore", "ok": True, "version": n,
                                                      "summary": f"Restored draft {n} of the files"})
    problem = await vibe_agent.validate(db, row, caller.org)
    await db.commit()
    return {"draft": vibe_app.clean_draft(row.draft), "problem": problem, "version": v.n if v else None}
