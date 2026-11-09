"""`/tasks/{call_id}`: the task an async call started, read and polled for the team that made it."""

from __future__ import annotations

import copy
import gzip
import json
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select

from conftest import verified_signup
from test_asynctasks import EP, _response, replicate_platform  # noqa: F401
from test_bounceban_async import (  # noqa: F401
    TASK, VERIFYING, Upstream, _finished, bounceban_on, fast_polls,
)

from treg import audit
from treg.application import tasks as tasks_app
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, CallRecord, CallTask, Hold, LedgerEntry

SUBMISSION = {"input": {"prompt": "A red kite over a beach.", "num_outputs": 1,
                        "aspect_ratio": "1:1", "output_format": "webp"}}


class Script:
    """A provider: the submission answer first, then each poll's answer (the last repeats)."""

    def __init__(self, submission: UpstreamResponse | dict, *polls: dict | tuple[int, object]):
        self.submission = submission
        self.polls = list(polls)
        self.seen: list[tuple[str, dict]] = []

    async def __call__(self, request, upstream_url, tool, secrets, client, **kwargs):
        if before := kwargs.get("before_outbound"):
            await before()
        headers = {k.decode().lower(): v.decode() for k, v in request.raw_headers}
        self.seen.append((upstream_url, headers))
        if len(self.seen) == 1:
            return (self.submission if isinstance(self.submission, UpstreamResponse)
                    else _response(201, self.submission))
        answer = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        status, document = answer if isinstance(answer, tuple) else (200, answer)
        return _response(status, document)


def _descriptor(**changes) -> dict:
    return copy.deepcopy(catalog_store.load().by_id[EP]["async"]) | changes


@pytest.fixture
def descriptor(monkeypatch):
    """Replace EP's async descriptor for one test."""
    def use(**changes) -> dict:
        value = _descriptor(**changes)
        monkeypatch.setitem(catalog_store.load().by_id[EP], "async", value)
        return value
    return use


async def _submit(clients: AsyncClient, monkeypatch, script: Script):
    monkeypatch.setattr(call_service, "relay", script)
    submitted = await clients.post(f"/call/{EP}", json=SUBMISSION)
    assert submitted.status_code in (200, 201), submitted.text
    return submitted


async def _entries(call_id: str) -> list[str]:
    async with session_maker() as db:
        return sorted(e.kind for e in (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id))).scalars().all())


async def test_a_platform_task_is_read_polled_and_settled_once(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    script = Script({"id": "t1", "status": "starting"},
                    {"id": "t1", "status": "processing"},
                    {"id": "t1", "status": "succeeded", "output": ["https://example.invalid/a.png"]})
    submitted = await _submit(clients, monkeypatch, script)
    call_id = submitted.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        row = await db.get(CallTask, call_id)
    assert (row.task_id, row.endpoint_id, row.poll_url) == ("t1", EP, None)

    first = await clients.post(f"/tasks/{call_id}", content=submitted.content)
    assert first.status_code == 200, first.text
    view = first.json()
    assert view["state"] == "pending" and view["status"] is None and view["error"] is None
    assert view["task_shown"] == "t1"
    assert view["resume"] == "treg call replicate.predictions.get -p id=t1"
    assert view["retry_after"] == 2 and view["max_wait"] == 86400
    assert first.headers["Retry-After"] == "2"
    assert len(script.seen) == 1  # reading the submission calls no provider

    polled = await clients.get(f"/tasks/{call_id}")
    view = polled.json()
    assert view["state"] == "pending" and view["status"] == "processing"
    assert view["warning"] == "unknown async status 'processing'; continuing to wait"
    url, headers = script.seen[1]
    assert url.endswith("/v1/predictions/t1") and headers["accept-encoding"] == "identity"
    assert await _entries(call_id) == ["reserve"]

    done = await clients.get(f"/tasks/{call_id}")
    view = done.json()
    assert view["state"] == "succeeded" and view["status"] == "succeeded"
    assert view["result"] == ["https://example.invalid/a.png"]
    assert view["result_text"] == '["https://example.invalid/a.png"]'
    assert view["result_url"] == "https://example.invalid/a.png"
    assert json.loads(view["response"]["body"])["status"] == "succeeded"
    assert view["response"]["http_status"] == 200 and view["response"]["cost_micro"] == 0
    assert "Retry-After" not in done.headers
    assert await _entries(call_id) == ["reserve", "settle"]
    async with session_maker() as db:
        assert (await db.get(AsyncTaskRecord, call_id)).status == "settled"
        assert await db.get(Hold, call_id) is None

    again = await clients.get(f"/tasks/{call_id}")
    assert again.json()["state"] == "succeeded"
    assert await _entries(call_id) == ["reserve", "settle"]
    await audit.drain()
    async with session_maker() as db:
        polls = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == view["response"]["call_id"]))).scalars().all()
    assert [p.kind for p in polls] == ["async_poll"]


@pytest.mark.parametrize("word, billed, entry", [("failed", False, "release"), ("billed", True, "settle")])
async def test_a_terminal_failure_closes_the_hold_once(
    clients: AsyncClient, monkeypatch, replicate_platform, descriptor, word, billed, entry,
):
    descriptor(status={"path": "status", "success": ["succeeded"], "failure": ["failed"],
                       "billed_failure": ["billed"]})
    submitted = await _submit(clients, monkeypatch, Script(
        {"id": "t2", "status": "starting"}, {"id": "t2", "status": word, "error": "no"}))
    call_id = submitted.headers["X-Treg-Call-Id"]
    for _ in range(2):
        view = (await clients.get(f"/tasks/{call_id}")).json()
        assert view["state"] == "failed" and view["billed"] is billed and view["status"] == word
        assert json.loads(view["response"]["body"])["error"] == "no"
    assert await _entries(call_id) == sorted(["reserve", entry])


async def test_a_declared_progress_word_is_not_a_warning(
    clients: AsyncClient, monkeypatch, replicate_platform, descriptor,
):
    descriptor(status={"path": "status", "success": ["succeeded"], "failure": ["failed"],
                       "progress": ["processing"]})
    submitted = await _submit(clients, monkeypatch, Script(
        {"id": "t3"}, {"status": "processing"}, {"status": "mystery"}, {"id": "t3"}))
    call_id = submitted.headers["X-Treg-Call-Id"]
    views = [(await clients.get(f"/tasks/{call_id}")).json() for _ in range(3)]
    assert [(v["state"], v["status"], v["warning"]) for v in views] == [
        ("pending", "processing", None),
        ("pending", "mystery", "unknown async status 'mystery'; continuing to wait"),
        ("pending", "None", "unknown async status 'None'; continuing to wait"),
    ]


async def test_past_the_descriptor_max_age_the_task_times_out_without_a_poll(
    clients: AsyncClient, monkeypatch, replicate_platform, descriptor,
):
    descriptor(max_age=60)
    script = Script({"id": "t4"}, {"status": "processing"})
    submitted = await _submit(clients, monkeypatch, script)
    call_id = submitted.headers["X-Treg-Call-Id"]
    assert (await clients.get(f"/tasks/{call_id}")).json()["state"] == "pending"
    async with session_maker() as db:
        row = await db.get(CallTask, call_id)
        row.created_at -= timedelta(seconds=61)
        await db.commit()
    seen = len(script.seen)
    view = (await clients.get(f"/tasks/{call_id}")).json()
    assert view["state"] == "error" and view["max_wait"] == 60
    assert view["error"]["kind"] == "expired"
    assert view["error"]["message"] == "timed out while waiting for the async task"
    assert len(script.seen) == seen


async def test_a_fetch_mode_result_is_a_command_and_a_missing_value_is_named(
    clients: AsyncClient, monkeypatch, replicate_platform, descriptor,
):
    descriptor(result={"fetch": "replicate.predictions.get", "ttl_note": "1h",
                       "fetch_param": {"in": "pathParams", "name": "id", "value_from": "output.file"}})
    submitted = await _submit(clients, monkeypatch, Script(
        {"id": "t5"}, {"status": "succeeded", "output": {"file": "x; rm -rf /"}}, {"status": "succeeded"}))
    call_id = submitted.headers["X-Treg-Call-Id"]
    view = (await clients.get(f"/tasks/{call_id}")).json()
    assert view["state"] == "succeeded" and view["result"] is None
    assert view["fetch_command"] == "treg call replicate.predictions.get -p 'id=x; rm -rf /'"
    assert view["ttl_note"] == "1h"
    missing = (await clients.get(f"/tasks/{call_id}")).json()
    assert missing["state"] == "error" and missing["ttl_note"] is None
    assert missing["error"]["kind"] == "result_not_retrievable"
    assert missing["error"]["message"] == "the terminal response has no 'output.file' for result retrieval"
    assert json.loads(missing["response"]["body"]) == {"status": "succeeded"}


async def test_a_routed_pending_answer_is_followed_by_the_parent_call_id(
    clients: AsyncClient, monkeypatch, bounceban_on, fast_polls,
):
    upstream = Upstream(VERIFYING)
    monkeypatch.setattr(call_service, "relay", upstream)
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.05)
    pending = await clients.post("/call/treg.people.email.verify",
                                 headers={"X-Treg-Route-Prefer": "bounceban"},
                                 json={"email": "dev@bounceban.com"})
    assert pending.status_code == 202, pending.text
    parent = pending.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        [task] = (await db.execute(select(AsyncTaskRecord))).scalars().all()
    assert task.call_id != parent and task.status == "pending"
    upstream.polls = [_finished("deliverable")]

    view = (await clients.post(f"/tasks/{parent}", content=pending.content)).json()
    assert view["state"] == "pending" and view["task_id"] == TASK
    assert view["endpoint_id"] == "bounceban.people.email.verify"
    assert view["resume"] == f"treg call bounceban.people.email.verify.status -p id={TASK}"
    polls = upstream.seen.count("poll")
    done = (await clients.get(f"/tasks/{parent}")).json()
    assert upstream.seen.count("poll") == polls + 1
    assert done["state"] == "succeeded" and done["result_text"] == "deliverable"
    async with session_maker() as db:
        settled = await db.get(AsyncTaskRecord, task.call_id)
        assert settled.status == "settled" and await db.get(Hold, task.call_id) is None
    assert await _entries(task.call_id) == ["reserve", "settle"]
    assert (await clients.get(f"/tasks/{parent}")).json()["state"] == "succeeded"
    assert await _entries(task.call_id) == ["reserve", "settle"]


async def test_another_team_cannot_read_or_poll_the_task(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    script = Script({"id": "t6"}, {"status": "succeeded", "output": ["u"]})
    submitted = await _submit(clients, monkeypatch, script)
    call_id = submitted.headers["X-Treg-Call-Id"]
    other = await verified_signup(clients, json={"email": "task-stranger@example.com"})
    stranger = {"X-Treg-Token": other.json()["token"]}
    for answer in (await clients.post(f"/tasks/{call_id}", content=submitted.content, headers=stranger),
                   await clients.get(f"/tasks/{call_id}", headers=stranger)):
        assert answer.status_code == 404
        assert answer.json()["detail"]["error"] == "task_not_found"
        assert answer.headers["X-Treg-Error"] == "1"
    assert len(script.seen) == 1 and await _entries(call_id) == ["reserve"]


async def test_the_posted_body_must_be_the_answer_treg_relayed(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    submitted = await _submit(clients, monkeypatch, Script({"id": "t7"}, {"status": "processing"}))
    call_id = submitted.headers["X-Treg-Call-Id"]
    forged = await clients.post(f"/tasks/{call_id}", content=b'{"id":"someone-else"}')
    assert forged.status_code == 409 and forged.json()["detail"]["error"] == "submission_mismatch"


async def test_an_own_key_task_is_recorded_from_a_compressed_answer_relayed_unchanged(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    await clients.post("/secrets", json={"name": "replicate", "value": "own-token"})
    packed = gzip.compress(json.dumps({"id": "own-1", "status": "starting"}).encode())

    async def stream():
        yield packed

    async def close():
        return None

    raw = UpstreamResponse(201, ((b"content-type", b"application/json"),
                                 (b"content-encoding", b"gzip")), stream(), close)
    script = Script(raw, {"id": "own-1", "status": "succeeded", "output": ["o"]})
    monkeypatch.setattr(call_service, "relay", script)
    submitted = await clients.post(f"/call/{EP}", json=SUBMISSION)
    assert submitted.status_code == 201 and "X-Treg-Cost-Micro" not in submitted.headers
    assert json.loads(submitted.content) == {"id": "own-1", "status": "starting"}
    call_id = submitted.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert (await db.get(CallTask, call_id)).task_id == "own-1"
    assert (await clients.post(f"/tasks/{call_id}", content=submitted.content)).json()["state"] == "pending"
    view = (await clients.get(f"/tasks/{call_id}")).json()
    assert view["state"] == "succeeded" and view["response"]["cost_micro"] is None


async def test_a_finished_submission_is_read_from_the_posted_answer(
    clients: AsyncClient, monkeypatch, bounceban_on,
):
    monkeypatch.setattr(call_service, "relay", Upstream(_finished("deliverable")))
    finished = await clients.get("/call/bounceban.people.email.verify?email=dev@bounceban.com")
    assert finished.status_code == 200, finished.text
    view = (await clients.post(f"/tasks/{finished.headers['X-Treg-Call-Id']}",
                               content=finished.content)).json()
    assert view["state"] == "succeeded" and view["result_text"] == "deliverable"
    assert view["response"]["source"] == "submission"
    assert view["response"]["body"] == finished.text


async def test_a_submission_naming_no_task_is_explained_and_not_polled(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    rejected = await _submit(clients, monkeypatch, Script({"oops": 1}, {}))
    call_id = rejected.headers["X-Treg-Call-Id"]
    view = (await clients.post(f"/tasks/{call_id}", content=rejected.content)).json()
    assert view["state"] == "error" and view["task_id"] is None and view["resume"] is None
    assert view["error"] == {"kind": "submission_rejected", "http_status": None, "retryable": False,
                             "by": None, "message": "submission response does not contain the async task id"}
    assert view["response"]["body"] == rejected.text
    unpollable = await clients.get(f"/tasks/{call_id}")
    assert unpollable.status_code == 409 and unpollable.json()["detail"]["error"] == "task_not_pollable"


async def test_a_refused_or_failed_poll_is_reported_as_the_polls_answer(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    submitted = await _submit(clients, monkeypatch, Script(
        {"id": "t8"}, (500, {"error": "busy"}), (404, {"error": "gone"})))
    call_id = submitted.headers["X-Treg-Call-Id"]
    busy = await clients.get(f"/tasks/{call_id}")
    view = busy.json()
    assert view["state"] == "error" and busy.headers["Retry-After"] == "2"
    assert view["error"] == {"kind": "poll_failed", "message": "polling returned HTTP 500",
                             "http_status": 500, "retryable": True, "by": "provider"}
    gone = (await clients.get(f"/tasks/{call_id}")).json()
    assert gone["error"]["retryable"] is False and gone["error"]["http_status"] == 404

    monkeypatch.setenv("TREG_PAUSED_PROVIDERS", "replicate")
    get_settings.cache_clear()
    paused = await clients.get(f"/tasks/{call_id}")
    view = paused.json()
    assert view["error"]["by"] == "treg" and view["error"]["http_status"] == 503
    assert json.loads(view["response"]["body"])["detail"]["error"] == "provider_paused"
    await audit.drain()
    async with session_maker() as db:
        refused = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == view["response"]["call_id"]))).scalar_one()
    assert refused.status_code == 503
    assert await _entries(call_id) == ["reserve"]
    monkeypatch.delenv("TREG_PAUSED_PROVIDERS")
    get_settings.cache_clear()


def test_a_result_strict_json_cannot_carry_is_left_to_its_text():
    task = CallTask(call_id="c", org_id=1, endpoint_id=EP, descriptor=_descriptor(), task_id="t")
    view = tasks_app.poll_view(task, _response(200, {}), b'{"status":"succeeded","output":NaN}', False, "p")
    assert view["result_text"] == "nan"
    assert json.loads(tasks_app.encode(view))["result"] is None
