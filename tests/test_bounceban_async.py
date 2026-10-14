"""BounceBan single verification: an immediate answer settles at once, a `verifying` one is waited for."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from treg import archive, audit, cli
from treg.application import asynctasks as async_task_app
from treg.application.call import async_bridge
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import ArchiveSnapshot, AsyncTaskRecord, CallRecord, Hold

from test_aigc_pr_b import FakeClock, response as cli_response

VERIFY = "bounceban.people.email.verify"
STATUS = "bounceban.people.email.verify.status"
EMAIL = "dev@bounceban.com"  # BounceBan's documented test address
TASK = "bb-task-1"


def _finished(result: str | None, **flags) -> dict:
    return {"id": TASK, "status": "success", "email": EMAIL, "result": result, "score": 99,
            "is_accept_all": False, "credits_consumed": 1, **flags}


VERIFYING = {"id": TASK, "status": "verifying",
             "msg": "The email verification process is not yet complete.", "try_again_at": 1}


def _upstream(status: int, doc: dict) -> UpstreamResponse:
    payload = json.dumps(doc).encode()

    async def stream():
        yield payload

    async def close():
        return None

    return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)


class Upstream:
    """A scripted BounceBan (and a second verifier that must never be asked)."""

    def __init__(self, submission: dict, polls: list[dict] | None = None):
        self.submission = submission
        self.polls = list(polls or [])
        self.seen: list[str] = []

    async def __call__(self, request, upstream_url, tool, secrets, client, **kwargs):
        if "bounceban" not in upstream_url:
            self.seen.append("other")
            return _upstream(200, {"data": {"status": "valid", "score": 90}})
        if upstream_url.endswith("/v1/verify/single/status"):
            self.seen.append("poll")
            # The last scripted answer repeats: BounceBan keeps a finished result for 90 days.
            doc = self.polls.pop(0) if len(self.polls) > 1 else (self.polls or [VERIFYING])[0]
            return _upstream(200, doc)
        self.seen.append("submit")
        return _upstream(200, self.submission)


async def _balance(clients) -> int:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


async def _tasks() -> list[AsyncTaskRecord]:
    async with session_maker() as db:
        return list((await db.execute(select(AsyncTaskRecord))).scalars().all())


async def _row(call_ref: str) -> CallRecord:
    await audit.drain()
    async with session_maker() as db:
        return (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == call_ref, CallRecord.endpoint_id == VERIFY))).scalar_one()


@pytest.fixture
def bounceban_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_BOUNCEBAN", "PLATFORM-BOUNCEBAN")
    monkeypatch.setenv("TREG_PLATFORM_KEY_HUNTER", "PLATFORM-HUNTER")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "bounceban,hunter")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fast_polls(monkeypatch):
    descriptor = catalog_store.load().by_id[VERIFY]["async"]
    monkeypatch.setitem(descriptor, "interval", 0.01)


async def test_every_finished_answer_is_charged_one_call(clients, monkeypatch, bounceban_on):
    """BounceBan bills every finished check, so treg does too: a verdict, `unknown`, and a finished
    answer with no verdict alike, whether it came at once or after `verifying`. None settles at 0."""
    charges = []
    for result in ("deliverable", "undeliverable", "risky", "unknown", None):
        upstream = Upstream(_finished(result))
        monkeypatch.setattr(call_service, "relay", upstream)
        before = await _balance(clients)
        answer = await clients.get(f"/call/{VERIFY}", params={"email": EMAIL})
        assert answer.status_code == 200
        assert answer.content == json.dumps(_finished(result)).encode()  # byte for byte
        assert upstream.seen == ["submit"]  # no wait, no poll
        charges.append(before - await _balance(clients))
        assert int(answer.headers["x-treg-cost-micro"]) == charges[-1]
    assert await _tasks() == []  # no pending task for an immediate answer
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []

    # The same price for a finished answer with no verdict that arrived after `verifying`.
    monkeypatch.setattr(call_service, "relay", Upstream(VERIFYING, [_finished(None)]))
    before = await _balance(clients)
    submitted = await clients.get(f"/call/{VERIFY}", params={"email": EMAIL})
    assert submitted.content == json.dumps(VERIFYING).encode()
    await clients.get(f"/call/{STATUS}", params={"id": TASK})
    [task] = await _tasks()
    assert task.status == "settled"
    charges.append(before - await _balance(clients))
    assert task.settled_micro == charges[-1]

    assert charges[0] > 0 and set(charges) == {charges[0]}


async def test_direct_verifying_then_success_settles_once_with_hit_and_verdict(
    clients, monkeypatch, bounceban_on,
):
    upstream = Upstream(VERIFYING, [VERIFYING, _finished("deliverable")])
    monkeypatch.setattr(call_service, "relay", upstream)
    before = await _balance(clients)
    submitted = await clients.get(f"/call/{VERIFY}", params={"email": EMAIL})
    assert submitted.status_code == 200
    assert submitted.content == json.dumps(VERIFYING).encode()
    assert json.loads(submitted.headers["x-treg-async"])["terminal_on_submission"] is True
    call_ref = submitted.headers["x-treg-call-id"]
    [task] = await _tasks()
    assert task.status == "pending" and task.task_id == TASK and task.call_id == call_ref
    async with session_maker() as db:
        assert await db.get(Hold, call_ref) is not None
    assert (await _row(call_ref)).hit is None

    still = await clients.get(f"/call/{STATUS}", params={"id": TASK})
    done = await clients.get(f"/call/{STATUS}", params={"id": TASK})
    again = await clients.get(f"/call/{STATUS}", params={"id": TASK})  # a late reread is free
    assert still.json()["status"] == "verifying"
    assert done.json()["status"] == again.json()["status"] == "success"
    assert upstream.seen == ["submit", "poll", "poll", "poll"]

    [task] = await _tasks()
    assert task.status == "settled" and task.settled_micro == task.reserved_micro > 0
    assert task.hit is True and task.verdict == "valid"
    assert before - await _balance(clients) == task.settled_micro
    async with session_maker() as db:
        assert await db.get(Hold, call_ref) is None
    original = await _row(call_ref)
    assert original.hit is True and original.verdict == "valid"


async def test_routed_immediate_answer_is_judged_at_once(clients, monkeypatch, bounceban_on):
    upstream = Upstream(_finished("risky", is_accept_all=True))
    monkeypatch.setattr(call_service, "relay", upstream)
    result = await clients.post(
        "/call/treg.people.email.verify",
        headers={"X-Treg-Route-Prefer": "bounceban"}, json={"email": EMAIL})
    assert result.status_code == 200, result.text
    meta = result.json()["_treg"]
    assert meta["served_by"] == VERIFY and meta["outcome"] == "hit"
    assert result.json()["output"]["status"] == "risky"
    assert upstream.seen == ["submit"]
    assert await _tasks() == []
    await audit.drain()
    async with session_maker() as db:
        row = (await db.execute(select(CallRecord).where(CallRecord.endpoint_id == VERIFY))).scalar_one()
    assert row.hit is True and row.verdict == "catch_all"


async def test_routed_verifying_is_waited_for_not_a_miss(
    clients, monkeypatch, bounceban_on, fast_polls,
):
    upstream = Upstream(VERIFYING, [VERIFYING, _finished("undeliverable")])
    monkeypatch.setattr(call_service, "relay", upstream)
    before = await _balance(clients)
    result = await clients.post(
        "/call/treg.people.email.verify",
        headers={"X-Treg-Route-Prefer": "bounceban"}, json={"email": EMAIL})
    assert result.status_code == 200, result.text
    meta = result.json()["_treg"]
    assert meta["served_by"] == VERIFY and meta["outcome"] == "hit"
    assert result.json()["output"] == {"valid": False, "status": "undeliverable", "score": 99}
    assert upstream.seen == ["submit", "poll", "poll"]  # the second verifier was never asked
    [task] = await _tasks()
    assert task.status == "settled" and task.settled_micro > 0
    assert task.hit is True and task.verdict == "invalid"
    assert before - await _balance(clients) == task.settled_micro
    row = await _row(task.call_id)
    assert row.hit is True and row.verdict == "invalid"


async def test_routed_verifying_past_the_wait_is_pending_and_the_worker_finishes_it(
    clients, monkeypatch, bounceban_on, fast_polls,
):
    upstream = Upstream(VERIFYING)
    monkeypatch.setattr(call_service, "relay", upstream)
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.05)
    before = await _balance(clients)
    result = await clients.post(
        "/call/treg.people.email.verify",
        headers={"X-Treg-Route-Prefer": "bounceban"}, json={"email": EMAIL})
    assert result.status_code == 202, result.text
    meta = result.json()["_treg"]
    assert meta["outcome"] == "pending" and meta["charged_micro"] is None
    assert "other" not in upstream.seen  # no second verifier charged
    [task] = await _tasks()
    assert task.status == "pending"
    reserved = task.reserved_micro
    assert before - await _balance(clients) == reserved

    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, task.call_id)
        row.next_check_at = row.created_at
        await db.commit()

    async def finished_poll(row, client):
        assert row.task_id == TASK
        return 200, json.dumps(_finished("deliverable")).encode()

    monkeypatch.setattr(async_task_app, "_poll", finished_poll)
    assert (await async_task_app.settle_due()).settled == 1
    [task] = await _tasks()
    assert task.status == "settled" and task.settled_micro == reserved
    assert task.hit is True and task.verdict == "valid"
    assert before - await _balance(clients) == reserved
    async with session_maker() as db:
        assert await db.get(Hold, task.call_id) is None
    row = await _row(task.call_id)
    assert row.hit is True and row.verdict == "valid"


async def test_worker_polls_inside_the_window_then_releases(clients, monkeypatch, bounceban_on):
    """BounceBan: at most 10 polls per id in five minutes, then stop. The worker's own schedule
    (first check after 60 s, then 40, 50, 60 s) polls five times; at `max_age` it stops, the task
    times out and the team's hold is released in full."""
    clock = {"now": datetime(2026, 1, 1, 12, 0, 0)}
    monkeypatch.setattr(async_task_app, "utcnow_naive", lambda: clock["now"])
    monkeypatch.setattr(call_service, "relay", Upstream(VERIFYING))
    before = await _balance(clients)
    await clients.get(f"/call/{VERIFY}", params={"email": EMAIL})
    [task] = await _tasks()
    created = task.created_at
    polls: list[float] = []

    async def verifying_poll(row, client):
        polls.append((clock["now"] - created).total_seconds())
        return 200, json.dumps(VERIFYING).encode()

    monkeypatch.setattr(async_task_app, "_poll", verifying_poll)
    for second in range(0, 24 * 3600, 5):
        clock["now"] = created + timedelta(seconds=second)
        await async_task_app.settle_due()
        if (await _tasks())[0].status != "pending":
            break
    [task] = await _tasks()
    assert task.status == "timed_out" and task.settled_micro == 0
    assert polls and len(polls) <= 5 and max(polls) < 300
    assert (clock["now"] - created).total_seconds() <= 300
    assert await _balance(clients) == before
    async with session_maker() as db:
        assert await db.get(Hold, task.call_id) is None


async def test_foreground_waits_stop_at_max_age():
    descriptor = dict(catalog_store.load().by_id[VERIFY]["async"], interval=0.05, max_age=0.3)
    polls = 0

    async def poll(task_id, attempt):
        nonlocal polls
        polls += 1
        return _upstream(200, VERIFYING), json.dumps(VERIFYING).encode()

    waited = await async_bridge.await_terminal(
        descriptor, json.dumps(VERIFYING).encode(), poll, timeout_s=60)
    assert waited.outcome == "pending" and 0 < polls < 10


def test_cli_await_uses_the_row_interval_and_window():
    descriptor = catalog_store.load().by_id[VERIFY]["async"]
    clock = FakeClock()
    polled = []

    def call_fn(target, params):
        polled.append((target, params))
        return cli_response(200, VERIFYING)

    outcome = cli.await_async_task(descriptor, cli_response(200, VERIFYING), call_fn, clock, 900)
    assert outcome["code"] == 3
    assert polled[0] == (STATUS, [("id", TASK)])
    assert set(clock.sleeps) == {30} and len(polled) <= 10 and clock.now <= 300

    count = len(polled)
    immediate = cli.await_async_task(
        descriptor, cli_response(200, _finished("deliverable")), call_fn, FakeClock(), 900)
    assert immediate["code"] == 0 and immediate["result"] == "deliverable"
    assert len(polled) == count  # the finished submission was not polled


async def test_a_verifying_answer_is_never_archived(clients, monkeypatch, bounceban_on):
    """A `verifying` first answer is a task id, not an answer: replayed later it would be a wrong
    hit. Only the finished answer is kept."""
    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    monkeypatch.setitem(catalog_store.load().by_id[VERIFY], "cache", "transient")
    monkeypatch.setattr(call_service, "relay", Upstream(VERIFYING))
    await clients.get(f"/call/{VERIFY}", params={"email": EMAIL})
    monkeypatch.setattr(call_service, "relay", Upstream(_finished("deliverable")))
    await clients.get(f"/call/{VERIFY}", params={"email": "other@bounceban.com"})
    await archive.drain()
    async with session_maker() as db:
        snapshots = (await db.execute(select(ArchiveSnapshot))).scalars().all()
    assert len(snapshots) == 1


@pytest.mark.parametrize("cost_type,terminal,when", [
    ("per_call", True, "terminal"),      # owed for the finished answer
    ("per_success", True, "response"),   # unchanged: an async per_success row without a table
    ("per_call", False, "response"),     # unchanged: a synchronous row
])
def test_only_a_per_call_async_price_waits_for_the_finished_answer(cost_type, terminal, when):
    from treg.domain.money import settlement

    basis = settlement.derive_basis(
        {"type": cost_type, "value": 1}, request={}, input_schema={}, unit_micro=4000,
        terminal=terminal, response_estimate_micro=4000)
    assert basis["when"] == when
    assert settlement.settle(basis, {"terminal": {"status": "success"}}) == 4000
