"""Wait for a catalogued asynchronous call without teaching callers provider semantics.

The descriptor is the contract: submission id extraction, poll cadence and terminal status all
come from catalog data.  The supplied poll function must use the ordinary call application path,
so authentication, shared-resource ownership and terminal settlement stay in one place.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from ...domain import asynctasks
from .types import UpstreamResponse


@dataclass(frozen=True)
class AsyncResult:
    outcome: str  # success | failure | billed_failure | pending | error
    task_id: str
    response: UpstreamResponse | None = None
    raw: bytes = b""
    document: object | None = None
    detail: str = ""


Poll = Callable[[str, int], Awaitable[tuple[UpstreamResponse, bytes]]]


async def await_terminal(
    descriptor: dict,
    submission: bytes,
    poll: Poll,
    *,
    timeout_s: float,
) -> AsyncResult:
    """Poll until terminal or the caller's wait budget expires.

    A timeout is deliberately *pending*, not an error: the durable async worker still owns the
    original hold and will settle it later.  Poll transport/server failures are retried within the
    same deadline.  A caller-visible 4xx ends the foreground wait as pending because it cannot prove
    that the already-accepted provider task is terminal.
    """
    try:
        kickoff = json.loads(submission)
        extracted = asynctasks.extract_submission(descriptor, kickoff)
    except (UnicodeDecodeError, json.JSONDecodeError, asynctasks.ExtractionError) as exc:
        return AsyncResult("error", "", detail=str(exc))

    deadline = time.monotonic() + max(0.0, timeout_s)
    interval = max(0.05, float(descriptor.get("interval") or 2))
    attempt = 0
    last_detail = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return AsyncResult("pending", extracted.task_id, detail=last_detail)
        await asyncio.sleep(min(interval, remaining))
        if time.monotonic() >= deadline:
            return AsyncResult("pending", extracted.task_id, detail=last_detail)
        attempt += 1
        try:
            response, raw = await poll(extracted.task_id, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the durable worker remains the settlement backstop
            last_detail = str(exc)[:160]
            continue
        if not 200 <= response.status < 300:
            last_detail = f"poll returned HTTP {response.status}"
            if 400 <= response.status < 500 and response.status not in (408, 429):
                return AsyncResult("pending", extracted.task_id, response, raw, detail=last_detail)
            continue
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            last_detail = "poll returned invalid JSON"
            continue
        outcome = asynctasks.classify_terminal(descriptor, document)
        if outcome in {"success", "failure", "billed_failure"}:
            return AsyncResult(outcome, extracted.task_id, response, raw, document)
        last_detail = "provider is still processing"
