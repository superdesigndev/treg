"""DataForSEO Standard tasks on treg's shared key: a /task_post is an async submission whose tasks
the submitting team owns, read back through the family's free task_get, and settled at the charge
DataForSEO reported when the task was posted (it bills at task_post; task_get reports no cost)."""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlmodel import select
from test_asynctasks import _response

from treg import archive
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import AsyncResourceRecord, AsyncTaskRecord
from conftest import verified_signup

POST = "dataforseo.x.business-data-google-reviews-task-post"
GET = "dataforseo.x.business-data-google-reviews-task-get"
TASK_A, TASK_B = "10151200-0000-0000-0000-00000000000a", "10151200-0000-0000-0000-00000000000b"


def _task(task_id: str, status: int, cost: float, result=None) -> dict:
    return {"id": task_id, "status_code": status, "status_message": "", "cost": cost,
            "result_count": len(result or []), "result": result}


def _envelope(tasks: list[dict]) -> dict:
    return {"status_code": 20000, "status_message": "Ok.", "cost": sum(t["cost"] for t in tasks),
            "tasks_count": len(tasks), "tasks_error": 0, "tasks": tasks}


@pytest.fixture
def dfs_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAFORSEO", "login:password")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dataforseo")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_every_shared_key_task_submission_has_an_owned_reader():
    """No platform-eligible /task_post may take a team's money for a task nothing can read."""
    catalog = catalog_store.load()
    dead_ends = []
    for ep in catalog.endpoints:
        if not (ep.get("path", "").endswith("/task_post") and catalog.platform_eligible(ep)):
            continue
        produced = {item["kind"] for item in
                    (ep.get("resource_ownership") or {}).get("produces") or []}
        readers = [other for other in catalog.endpoints
                   if other.get("provider") == ep.get("provider")
                   and ((other.get("resource_ownership") or {}).get("requires") or {}).get("kind")
                   in produced and catalog.platform_eligible(other)]
        if not readers:
            dead_ends.append(ep["id"])
    assert dead_ends == []


async def test_a_task_post_is_owned_polled_and_settled_at_the_submission_charge(
    clients: AsyncClient, monkeypatch, dfs_platform,
):
    answers = [
        _response(200, _envelope([_task(TASK_A, 20100, 0.00375), _task(TASK_B, 20100, 0.00375)])),
        _response(200, _envelope([_task(TASK_B, 40602, 0)])),
        _response(200, _envelope([_task(TASK_A, 20000, 0, result=[{"items": []}])])),
    ]

    async def fake_relay(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    submitted = await clients.post(f"/call/{POST}", json=[
        {"keyword": "hedonism wines", "location_name": "London,England,United Kingdom"},
        {"keyword": "berry bros", "location_name": "London,England,United Kingdom"},
    ])
    assert submitted.status_code == 200
    descriptor = json.loads(submitted.headers["X-Treg-Async"])
    assert descriptor["poll"]["endpoint"] == GET

    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalar_one()
        owned = {r.resource_id for r in (await db.execute(select(AsyncResourceRecord))).scalars()}
    assert task.task_id == TASK_A and task.status == "pending"
    assert task.settlement_basis["submission_charge_micro"] == 7_500
    assert owned == {TASK_A, TASK_B}  # every task of the batch, not only the tracked first one

    # The second task of the batch is the team's too, and reading it costs nothing.
    second = await clients.get(f"/call/{GET}", params={"id": TASK_B})
    assert second.status_code == 200

    # The owner's own poll sees the tracked task finish and settles the hold at what DataForSEO
    # reported for the submission, not at the catalog estimate.
    finished = await clients.get(f"/call/{GET}", params={"id": TASK_A})
    assert finished.status_code == 200
    async with session_maker() as db:
        task = await db.get(AsyncTaskRecord, task.call_id)
    assert task.status == "settled" and task.settled_micro == 7_500

    other = await verified_signup(clients, json={"email": "dfs-stranger@example.com"})
    stranger = {"X-Treg-Token": other.json()["token"]}

    async def must_not_relay(*args, **kwargs):
        raise AssertionError("another team's task reached treg's DataForSEO account")

    monkeypatch.setattr(call_service, "relay", must_not_relay)
    for task_id in (TASK_A, TASK_B):
        denied = await clients.get(f"/call/{GET}", params={"id": task_id}, headers=stranger)
        assert denied.status_code == 403
        assert denied.json()["detail"]["error"] == "async_resource_not_owned"


async def test_a_task_that_fails_after_posting_still_bills_what_dataforseo_charged(
    clients: AsyncClient, monkeypatch, dfs_platform,
):
    answers = [
        _response(200, _envelope([_task(TASK_A, 20100, 0.00375)])),
        _response(200, _envelope([_task(TASK_A, 40102, 0)])),
    ]

    async def fake_relay(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    submitted = await clients.post(f"/call/{POST}", json=[{"keyword": "nothing here"}])
    assert submitted.status_code == 200
    assert (await clients.get(f"/call/{GET}", params={"id": TASK_A})).status_code == 200
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalar_one()
    assert task.status == "settled" and task.settled_micro == 3_750


async def test_a_batch_whose_first_task_was_refused_bills_the_tasks_dataforseo_accepted(
    clients: AsyncClient, monkeypatch, dfs_platform,
):
    """The accepted task is owned and readable, so the call pays what DataForSEO charged for it."""
    answers = [
        _response(200, _envelope([_task(TASK_A, 40501, 0), _task(TASK_B, 20100, 0.00375)])),
        _response(200, _envelope([_task(TASK_B, 20000, 0, result=[{"items": []}])])),
    ]

    async def fake_relay(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    submitted = await clients.post(f"/call/{POST}", json=[{"keyword": ""}, {"keyword": "tea"}])
    assert submitted.status_code == 200
    assert submitted.headers["X-Treg-Cost-Micro"] == "3750"
    assert (await clients.get(f"/call/{GET}", params={"id": TASK_B})).status_code == 200
    async with session_maker() as db:
        assert (await db.execute(select(AsyncTaskRecord))).first() is None


async def test_an_on_page_crawl_is_readable_only_by_the_team_that_paid_for_it(
    clients: AsyncClient, monkeypatch, dfs_platform,
):
    answers = [
        _response(200, _envelope([_task(TASK_A, 20100, 0.018)])),
        _response(200, _envelope([_task(TASK_A, 20000, 0, result=[{"crawl_progress": "finished"}])])),
        _response(200, _envelope([_task(TASK_A, 20000, 0, result=[{"items": []}])])),
    ]

    async def fake_relay(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    crawl = await clients.post("/call/dataforseo.x.on-page-task-post",
                               json=[{"target": "example.com", "max_crawl_pages": 10}])
    assert crawl.status_code == 200
    summary = await clients.get("/call/dataforseo.x.on-page-summary-id", params={"id": TASK_A})
    assert summary.status_code == 200
    pages = await clients.post("/call/dataforseo.x.on-page-pages", json=[{"id": TASK_A, "limit": 10}])
    assert pages.status_code == 200

    async def must_not_relay(*args, **kwargs):
        raise AssertionError("an unowned crawl reached treg's DataForSEO account")

    monkeypatch.setattr(call_service, "relay", must_not_relay)
    smuggled = await clients.post("/call/dataforseo.x.on-page-pages",
                                  json=[{"id": TASK_A}, {"id": TASK_B}])
    assert smuggled.status_code == 400
    other = await verified_signup(clients, json={"email": "crawl-stranger@example.com"})
    denied = await clients.post("/call/dataforseo.x.on-page-pages", json=[{"id": TASK_A}],
                                headers={"X-Treg-Token": other.json()["token"]})
    assert denied.status_code == 403


@pytest.mark.parametrize("endpoint, body", [
    (POST, [{"keyword": "hedonism wines", "location_name": "London,England,United Kingdom"}]),
    ("dataforseo.x.on-page-task-post", [{"target": "example.com", "max_crawl_pages": 10}]),
])
async def test_a_task_submission_is_never_answered_from_the_archive(
    clients: AsyncClient, monkeypatch, dfs_platform, endpoint, body,
):
    """A submission creates something the asking team owns: a replay would sell it another
    team's task id. Every submission reaches the provider, async descriptor or not."""
    settings = get_settings()
    monkeypatch.setattr(settings, "archive_mode", "serve")
    monkeypatch.setattr(settings, "archive_serve_endpoints", endpoint)
    monkeypatch.setattr(settings, "archive_serve_percent", 100)
    posted = []

    async def fake_relay(*args, **kwargs):
        posted.append(TASK_B if posted else TASK_A)
        return _response(200, _envelope([_task(posted[-1], 20100, 0.00375)]))

    monkeypatch.setattr(call_service, "relay", fake_relay)
    for _ in range(2):
        submitted = await clients.post(f"/call/{endpoint}", json=body)
        await archive.drain()
        assert submitted.status_code == 200 and submitted.headers.get("x-treg-cache") != "hit"
    assert posted == [TASK_A, TASK_B]
