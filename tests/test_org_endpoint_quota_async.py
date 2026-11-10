"""Admitted async submissions keep their handle and settlement through quota faults."""

import json
from collections import Counter

import pytest

from treg.application import arena
from treg.application import asynctasks as async_task_app
from treg.application.call import async_bridge, service
from treg.application.call.types import CallFailure
from treg.config import get_settings
from treg.infra import org_endpoint_quota
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, Hold

from test_bounceban_async import (
    EMAIL, STATUS, TASK, VERIFY, VERIFYING, Upstream, _balance, _finished, _tasks,
    _upstream, bounceban_on, fast_polls,  # noqa: F401
)
from test_enrich_arena import finish


@pytest.fixture(autouse=True)
async def close_arena():
    yield
    await arena.shutdown()


def _quota(monkeypatch, poll_outcome):
    monkeypatch.setattr(get_settings(), "org_endpoint_quota_enabled", True)
    attempts = Counter()

    async def admit(org_id, endpoint_id):
        attempts[endpoint_id] += 1
        return poll_outcome if endpoint_id == STATUS else "allowed"

    async def admit_many(targets):
        for target in dict.fromkeys(targets):
            outcome = await admit(*target)
            if outcome != "allowed":
                return outcome, target
        return "allowed", None

    monkeypatch.setattr(org_endpoint_quota, "admit", admit)
    monkeypatch.setattr(org_endpoint_quota, "admit_many", admit_many)
    return attempts


async def _call(clients, surface):
    if surface == "route":
        response = await clients.post("/call/treg.people.email.verify", json={"email": EMAIL},
                                      headers={"X-Treg-Route-Prefer": "bounceban"})
        assert response.status_code in (200, 202), response.text
        meta = response.json()["_treg"]
        return {"state": meta["outcome"], "task_id": meta.get("async", {}).get("task_id"),
                "charged_micro": meta["charged_micro"], "reserved_micro": meta.get("reserved_micro")}
    response = await clients.post("/arena/plans", json={
        "capability": "people.email.verify", "identity": {"email": EMAIL},
        "mode": "compare", "providers": ["bounceban"], "max_cost_micro": 1_000_000})
    assert response.status_code == 200, response.text
    return (await finish(clients, response.json()))["results"][0]


@pytest.mark.parametrize("surface", ["route", "arena"])
@pytest.mark.parametrize("poll_outcome", ["exceeded", "unavailable"])
async def test_admitted_internal_polls_do_not_read_or_consume_quota(
    clients, monkeypatch, bounceban_on, fast_polls, surface, poll_outcome,
):
    attempts = _quota(monkeypatch, poll_outcome)
    upstream = Upstream(VERIFYING, [VERIFYING, _finished("deliverable")])
    monkeypatch.setattr(service, "relay", upstream)
    before = await _balance(clients)
    result = await _call(clients, surface)
    assert result["state"] == "hit"
    assert upstream.seen == ["submit", "poll", "poll"]
    assert attempts[VERIFY] == 1 and attempts[STATUS] == 0
    [task] = await _tasks()
    assert task.status == "settled" and task.settled_micro > 0
    assert result["charged_micro"] == task.settled_micro
    assert before - await _balance(clients) == task.settled_micro
    async with session_maker() as db:
        assert await db.get(Hold, task.call_id) is None

    direct = await clients.get(f"/call/{STATUS}", params={"id": TASK})
    assert direct.status_code == (429 if poll_outcome == "exceeded" else 200), direct.text
    assert attempts[STATUS] == 1
    assert upstream.seen == ["submit", "poll", "poll"] + (
        ["poll"] if poll_outcome == "unavailable" else [])
    assert before - await _balance(clients) == task.settled_micro


@pytest.mark.parametrize("surface", ["route", "arena"])
@pytest.mark.parametrize("poll_outcome", ["exceeded", "unavailable"])
async def test_pending_handle_survives_poll_quota_and_worker_settles_once(
    clients, monkeypatch, bounceban_on, fast_polls, surface, poll_outcome,
):
    attempts = _quota(monkeypatch, poll_outcome)
    upstream = Upstream(VERIFYING)

    async def not_ready(request, upstream_url, tool, secrets, client, **kwargs):
        if upstream_url.endswith("/v1/verify/single/status"):
            if before_outbound := kwargs.get("before_outbound"):
                await before_outbound()
            upstream.seen.append("poll")
            return _upstream(404, {"message": "not ready"})
        return await upstream(request, upstream_url, tool, secrets, client, **kwargs)

    monkeypatch.setattr(service, "relay", not_ready)
    before = await _balance(clients)
    result = await _call(clients, surface)
    assert result["state"] == "pending" and result["task_id"] == TASK
    assert result["charged_micro"] is None
    assert attempts[VERIFY] == 1 and attempts[STATUS] == 0
    assert upstream.seen == ["submit", "poll"]
    [task] = await _tasks()
    assert task.status == "pending" and task.task_id == TASK
    reserved = task.reserved_micro
    assert result["reserved_micro"] == reserved
    assert before - await _balance(clients) == reserved
    async with session_maker() as db:
        assert await db.get(Hold, task.call_id) is not None
        row = await db.get(AsyncTaskRecord, task.call_id)
        row.next_check_at = row.created_at
        await db.commit()

    async def finished_poll(row, client):
        assert row.task_id == TASK
        return 200, json.dumps(_finished("deliverable")).encode()

    monkeypatch.setattr(async_task_app, "_poll", finished_poll)
    assert (await async_task_app.settle_due()).settled == 1
    assert (await async_task_app.settle_due()).settled == 0
    [task] = await _tasks()
    assert task.status == "settled" and task.settled_micro == reserved
    assert before - await _balance(clients) == reserved
    async with session_maker() as db:
        assert await db.get(Hold, task.call_id) is None


@pytest.mark.parametrize("kind,status", [
    ("connect_failed", 502), ("read_timeout", 504),
])
async def test_bridge_preserves_task_id_when_a_poll_raises_transport_failure(kind, status):
    descriptor = dict(arena.catalog_store.load().by_id[VERIFY]["async"], interval=0.05)
    attempts = []

    async def poll(task_id, attempt):
        attempts.append((task_id, attempt))
        raise CallFailure(kind, status_code=status, detail={"error": kind})

    result = await async_bridge.await_terminal(
        descriptor, json.dumps(VERIFYING).encode(), poll, timeout_s=0.11)
    assert result.outcome == "pending" and result.task_id == TASK
    assert attempts and all(task_id == TASK for task_id, _ in attempts)
