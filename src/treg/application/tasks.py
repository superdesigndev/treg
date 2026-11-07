"""`/tasks/{call_id}`: the task an async call started, read and followed for the team that made it.

The call runtime records the task when it relays the async answer (`CallTask`). A look at the task
is one ordinary poll on the call road (`routers.call.run_call_input`), so its gates, credential,
hold, settle, audit row and owned-poll settlement are exactly a `/call/<poll endpoint>`'s; nothing
here can charge, refund or hold anything. The descriptor is read with `domain.asynctasks` only.
"""

from __future__ import annotations

import hashlib
import json
import math
import shlex
from urllib.parse import urlencode

from sqlalchemy import select

from .. import jsontext
from ..domain import asynctasks
from ..domain.governance.access import pinned_tag_predicates
from ..infra.db import session_maker
from ..models import CallTask
from ..timeutil import utcnow_naive
from .call.evidence import _decompress_for_scan
from .call.types import CallerSnapshot, CallInput, EmptyBody, UpstreamResponse

MAX_POLL_ANSWER_BYTES = 8 * 1024 * 1024
DEFAULT_INTERVAL_S = 10.0
TIMED_OUT = "timed out while waiting for the async task"

# What a look at the task never forwards: the look's own body, transport encodings (the answer
# must be read), and a replay label that would freeze every later poll on its first answer.
_DROPPED_LOOK_HEADERS = frozenset({
    b"content-type", b"content-length", b"content-encoding", b"transfer-encoding", b"expect",
    b"accept-encoding", b"x-treg-body-encoding", b"idempotency-key",
})


async def find(call_id: str, org_id: int, pinned_tags: dict | None) -> CallTask | None:
    """The team's task for `call_id`; a pinned caller sees only tasks made under its pin."""
    async with session_maker() as db:
        row = (await db.execute(select(CallTask).where(
            CallTask.call_id == call_id, CallTask.org_id == org_id,
            *pinned_tag_predicates(CallTask.tags, pinned_tags)))).scalar_one_or_none()
        return row.model_copy() if row is not None else None


def same_submission(task: CallTask, body: bytes) -> bool:
    """The posted body is the answer treg relayed for the call (a routed record has no digest)."""
    return (task.submission_sha256 is None
            or hashlib.sha256(body).hexdigest() == task.submission_sha256)


def pollable(task: CallTask) -> bool:
    return task.task_id not in (None, "")


def expired(task: CallTask) -> bool:
    return asynctasks.expired(task.created_at, utcnow_naive(), task.descriptor)


def interval(descriptor: dict) -> float:
    return float(descriptor.get("interval") or DEFAULT_INTERVAL_S)


def retry_after(view: dict) -> str | None:
    """`Retry-After` for an answer worth asking again: pending, or a poll the provider failed."""
    error = view.get("error") or {}
    if view["state"] == "pending" or error.get("retryable"):
        return str(max(1, math.ceil(view["retry_after"])))
    return None


def _resume(task: CallTask) -> str | None:
    """The `treg call` line that polls the task by hand, as the CLI has always printed it."""
    if not pollable(task):
        return None
    poll = (task.descriptor or {}).get("poll") or {}
    if poll.get("endpoint"):
        name = str((poll.get("param") or {}).get("name"))
        return f"treg call {poll['endpoint']} -p {shlex.quote(name + '=' + str(task.task_id))}"
    if task.poll_url:
        return f"treg call {shlex.quote(task.poll_url)}"
    return None


def _base(task: CallTask) -> dict:
    descriptor = task.descriptor or {}
    return {
        "call_id": task.call_id, "endpoint_id": task.endpoint_id,
        "task_id": task.task_id,
        "task_shown": asynctasks.shown(task.task_id) if pollable(task) else None,
        "state": "pending", "status": None, "status_shown": None, "billed": None, "warning": None,
        "response": None,
        "result": None, "result_text": None, "result_url": None, "fetch_command": None,
        "ttl_note": None,
        "resume": _resume(task),
        "retry_after": interval(descriptor),
        "max_wait": asynctasks.max_age(descriptor).total_seconds(),
        "error": None,
    }


def _error(view: dict, kind: str, message: str, **extra) -> dict:
    view["state"] = "error"
    view["error"] = {"kind": kind, "message": message, "http_status": None, "retryable": False,
                     "by": None} | extra
    return view


def _rendered(value: object) -> str | None:
    """How a path-mode result prints on a terminal: JSON for a container, else its text with any
    lone surrogate escaped so a UTF-8 stream can write it."""
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return jsontext.dumps(value)
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


def _terminal(task: CallTask, document: object, response: dict) -> dict:
    """One answer read with the descriptor: finished (with what to retrieve), failed, or pending."""
    descriptor = task.descriptor or {}
    rule = descriptor.get("status") or {}
    status = str(asynctasks.json_path(document, str(rule.get("path") or "")))
    view = _base(task) | {"status": status, "status_shown": asynctasks.shown(status),
                          "response": response}
    outcome = asynctasks.classify_terminal(descriptor, document)
    if outcome == "success":
        view["state"] = "succeeded"
        found = asynctasks.artifact(descriptor, document)
        result_rule = descriptor.get("result") or {}
        if result_rule.get("path"):
            view["result"] = found["result"]
            view["result_text"] = _rendered(found["result"])
            view["result_url"] = found["result_url"]
        elif found["fetch"] is None:
            value_from = (result_rule.get("fetch_param") or {}).get("value_from")
            return _error(view, "result_not_retrievable",
                          f"the terminal response has no {value_from!r} for result retrieval")
        else:
            view["fetch_command"] = asynctasks.fetch_command(found["fetch"])
        view["ttl_note"] = found["ttl_note"]
        return view
    if outcome in ("failure", "billed_failure"):
        view["state"] = "failed"
        view["billed"] = outcome == "billed_failure"
        return view
    if status not in {str(item) for item in rule.get("progress", [])}:
        view["warning"] = f"unknown async status {asynctasks.shown(status)!r}; continuing to wait"
    return view


def _text(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip().strip('"')
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def submission_view(task: CallTask, body: bytes) -> dict:
    """The first look, from the submission itself: no provider call. The answer may already be the
    finished one (`terminal_on_submission`) or name no task; otherwise the task is pending."""
    view = _base(task)
    if task.submission_sha256 is None:
        return view
    response = {"source": "submission", "http_status": None, "content_type": None,
                "body": _text(body, ""), "call_id": task.call_id, "cost_micro": None}
    try:
        document = json.loads(body)
    except ValueError:
        return _error(view, "submission_not_json", "the async submission response is not JSON")
    try:
        asynctasks.extract_submission(task.descriptor or {}, document)
    except asynctasks.ExtractionError as exc:
        view["response"] = response
        return _error(view, "submission_rejected", str(exc))
    if asynctasks.finished_on_submission(task.descriptor, document):
        return _terminal(task, document, response)
    return view


def expired_view(task: CallTask) -> dict:
    return _error(_base(task), "expired", TIMED_OUT)


def poll_input(task: CallTask, headers: tuple[tuple[bytes, bytes], ...], caller: CallerSnapshot,
               client_ip: str) -> CallInput:
    """The poll a look makes, as `treg call` would send it: the catalog poll endpoint with the task
    id (a path parameter is filled from the query), or the provider's own poll URL."""
    poll = (task.descriptor or {}).get("poll") or {}
    if poll.get("endpoint"):
        target = str(poll["endpoint"])
        query = ((str((poll.get("param") or {}).get("name")), str(task.task_id)),)
    else:
        target, query = str(task.poll_url), ()
    kept = tuple((k, v) for k, v in headers if k.lower() not in _DROPPED_LOOK_HEADERS)
    return CallInput(method="GET", raw_rest=target,
                     raw_headers=kept + ((b"accept-encoding", b"identity"),),
                     query_items=query, raw_query=urlencode(query), body=EmptyBody(),
                     caller=caller, client_ip=client_ip)


async def read_poll_answer(upstream: UpstreamResponse) -> tuple[bytes, bool]:
    """The poll's whole answer, at most MAX_POLL_ANSWER_BYTES; (bytes, too_large). Always closed."""
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in upstream.body_stream:
            size += len(chunk)
            if size > MAX_POLL_ANSWER_BYTES:
                return b"", True
            chunks.append(chunk)
    finally:
        await upstream.close()
    return b"".join(chunks), False


def _header(headers: tuple[tuple[bytes, bytes], ...], name: bytes) -> str:
    return next((v.decode("latin-1") for k, v in headers if k.lower() == name), "")


def poll_view(task: CallTask, upstream: UpstreamResponse, raw: bytes, too_large: bool,
              poll_call_id: str) -> dict:
    """A look's answer from the poll the call road made."""
    status = upstream.status
    content_type = _header(upstream.raw_headers, b"content-type")
    cost = _header(upstream.raw_headers, b"x-treg-cost-micro")
    view = _base(task)
    if too_large:
        return _error(view, "poll_too_large",
                      f"the polling response is larger than {MAX_POLL_ANSWER_BYTES} bytes; "
                      "poll it by hand with the resume command")
    decoded = _decompress_for_scan(raw, _header(upstream.raw_headers, b"content-encoding"))
    response = {"source": "poll", "http_status": status, "content_type": content_type or None,
                "body": _text(decoded if decoded is not None else raw, content_type),
                "call_id": poll_call_id, "cost_micro": int(cost) if cost.isdigit() else None}
    view["response"] = response
    if status >= 400:
        return _error(view, "poll_failed", f"polling returned HTTP {status}", http_status=status,
                      retryable=status >= 500, by="provider")
    try:
        document = json.loads(decoded if decoded is not None else raw)
    except ValueError:
        return _error(view, "poll_not_json", "a polling response was not JSON")
    return _terminal(task, document, response)


def refusal_view(task: CallTask, status: int, detail: object, poll_call_id: str | None) -> dict:
    """A look whose poll treg itself refused, reported as the poll's HTTP answer."""
    view = _base(task)
    view["response"] = {"source": "poll", "http_status": status, "content_type": "application/json",
                        "body": jsontext.dumps({"detail": detail}), "call_id": poll_call_id,
                        "cost_micro": None}
    return _error(view, "poll_failed", f"polling returned HTTP {status}", http_status=status,
                  retryable=status >= 500, by="treg")


def encode(view: dict) -> bytes:
    """The answer as JSON. A provider result holding NaN or infinity, which strict JSON cannot
    carry, is left out of `result`; `result_text` still shows it."""
    try:
        return jsontext.dumps_bytes(view, allow_nan=False, separators=(",", ":"))
    except ValueError:
        return jsontext.dumps_bytes(view | {"result": None}, separators=(",", ":"))
