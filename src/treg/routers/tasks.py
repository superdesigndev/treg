"""HTTP adapter for `/tasks/{call_id}`: the task an async call started, for the team that made it.

`POST` with the call's answer as the body reads the submission and makes no provider call; `GET`
makes one poll through `run_call_input`, the call road `/call/` itself uses
(docs/context/interface/api.md, "Async tasks").
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..application import tasks as tasks_app
from ..application.call.types import CallerSnapshot
from ..domain.identity.access import Caller, require_member
from .auth import _client_ip
from .call import _stamp_call_exit, enter_call, run_call_input

app = APIRouter()
router = app


def _answer(view: dict) -> Response:
    response = Response(tasks_app.encode(view), media_type="application/json",
                        headers={"Cache-Control": "no-store"})
    if wait := tasks_app.retry_after(view):
        response.headers["Retry-After"] = wait
    return response


def _refused(status: int, error: str, call_id: str, message: str) -> Response:
    return Response(tasks_app.encode({"detail": {"error": error, "call_id": call_id, "message": message}}),
                    status_code=status, media_type="application/json",
                    headers={"X-Treg-Error": "1", "Cache-Control": "no-store"})


async def _task(call_id: str, caller: Caller):
    return await tasks_app.find(call_id, caller.org_id, caller.membership.pinned_tags)


def _not_found(call_id: str) -> Response:
    return _refused(404, "task_not_found", call_id,
                    f"this team has no async task for call id {call_id}")


@app.post("/tasks/{call_id}")
async def task_submission(call_id: str, request: Request,
                          caller: Caller = Depends(require_member)) -> Response:
    """Read the task an async call's answer names. The body is that answer, as received."""
    task = await _task(call_id, caller)
    if task is None:
        return _not_found(call_id)
    body = await request.body()
    if not tasks_app.same_submission(task, body):
        return _refused(409, "submission_mismatch", call_id,
                        f"the body is not the answer treg relayed for call id {call_id}")
    return _answer(tasks_app.submission_view(task, body))


@app.get("/tasks/{call_id}")
async def task_look(call_id: str, request: Request,
                    caller: Caller = Depends(require_member)) -> Response:
    """Poll the task once, as the team, and read the answer with the task's descriptor."""
    task = await _task(call_id, caller)
    if task is None:
        return _not_found(call_id)
    if not tasks_app.pollable(task):
        return _refused(409, "task_not_pollable", call_id,
                        f"the answer to call id {call_id} named no task to poll")
    if tasks_app.expired(task):
        return _answer(tasks_app.expired_view(task))
    enter_call(request, caller)
    poll = tasks_app.poll_input(task, tuple(request.headers.raw), CallerSnapshot.capture(caller),
                                _client_ip(request))

    async def finish(request, context, upstream, rest) -> Response:
        raw, too_large = await tasks_app.read_poll_answer(upstream)
        return _answer(tasks_app.poll_view(task, upstream, raw, too_large, context.call_ref))

    try:
        return await run_call_input(request, caller, poll, finish=finish, rest=poll.raw_rest)
    except HTTPException as exc:
        # treg refused the poll: audit it as `/call/` would, then report it as the poll's answer.
        await _stamp_call_exit(request, Response(), exc.status_code)
        return _answer(tasks_app.refusal_view(task, exc.status_code, exc.detail,
                                              getattr(request.state, "call_ref", None)))
