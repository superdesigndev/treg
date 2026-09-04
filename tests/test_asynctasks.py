"""Deferred settlement for asynchronous metered catalog calls."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg.application import asynctasks as task_app
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg import archive, audit, reconcile
from treg.domain import asynctasks
from treg.domain import money as ledger
from treg.domain.money import settlement
from treg.infra.db import session_maker
from treg.models import (
    ArchiveKey, ArchiveSnapshot, AsyncResourceRecord, AsyncTaskRecord, Hold, LedgerEntry,
)
from treg.timeutil import utcnow_naive


EP = "replicate.image-gen.flux-schnell"


def _response(status: int, document: object) -> UpstreamResponse:
    body = json.dumps(document).encode()

    async def stream():
        yield body

    async def close():
        return None

    return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)


@pytest.fixture
def replicate_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_REPLICATE", "test-platform-token")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "replicate")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _submit(clients: AsyncClient, monkeypatch, document: dict):
    async def fake_relay(*args, **kwargs):
        return _response(201, document)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    return await clients.post(f"/call/{EP}", json={"input": {
        "prompt": "A red kite over a beach.", "num_outputs": 1,
        "aspect_ratio": "1:1", "output_format": "webp",
    }})


async def test_settle_fork_keeps_hold_and_writes_pending_row(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    response = await _submit(clients, monkeypatch, {
        "id": "prediction-1", "urls": {"get": "https://api.replicate.com/v1/predictions/1"}})
    assert response.status_code == 201
    assert response.headers["X-Treg-Cost-Micro"] == "3000"
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
    assert row is not None and hold is not None
    assert (row.task_id, row.poll_url, row.status) == ("prediction-1", None, "pending")
    assert row.settlement_basis["when"] == "terminal"


async def test_a_2xx_without_a_task_id_settles_at_zero_on_the_request_path(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    """No task in the answer means nothing to poll and nothing to charge: closed now, not parked
    until the 24-hour deadline (which is what an extraction failure used to do)."""
    response = await _submit(clients, monkeypatch, {})
    assert response.status_code == 201
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert await db.get(AsyncTaskRecord, call_id) is None
        assert await db.get(Hold, call_id) is None
        entries = {e.kind: e.amount_micro for e in (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id))).scalars().all()}
    assert entries == {"reserve": -3000, "settle": 0}


async def test_a_2xx_that_is_not_json_settles_at_zero_on_the_request_path(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    async def fake_relay(*args, **kwargs):
        body = b"<html>WAF challenge</html>"

        async def stream():
            yield body

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"text/html"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    response = await clients.post(f"/call/{EP}", json={"input": {"prompt": "x", "num_outputs": 1}})
    assert response.status_code == 200 and response.headers["X-Treg-Cost-Micro"] == "0"
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert await db.get(AsyncTaskRecord, call_id) is None
        assert await db.get(Hold, call_id) is None


async def test_one_failing_row_does_not_abort_the_tick(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    """relay() raises GatewayFailed (an unset platform key, an SSRF refusal), which is not an
    httpx or JSON error: it must back that row off and let the tick serve the others."""
    from treg.application.call.types import GatewayFailed
    broken = await _due_submission(clients, monkeypatch, {"status": "succeeded", "output": ["u"]})
    fine = await _due_submission(clients, monkeypatch, {"status": "succeeded", "output": ["u"]})

    async def poll(row, client):
        if row.call_id == broken:
            raise GatewayFailed("injection_failed", status_code=502, detail="no platform key")
        return 200, json.dumps({"status": "succeeded", "output": ["u"]}).encode()

    monkeypatch.setattr(task_app, "_poll", poll)
    result = await task_app.settle_due()
    assert (result.claimed, result.settled, result.backed_off) == (2, 1, 1)
    async with session_maker() as db:
        assert (await db.get(AsyncTaskRecord, broken)).status == "pending"
        assert (await db.get(AsyncTaskRecord, fine)).status == "settled"


async def test_pending_row_write_failure_releases_the_hold_and_alerts(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    async def fail_persistence(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(task_app, "defer_submission", fail_persistence)
    response = await _submit(clients, monkeypatch, {
        "id": "prediction-untracked",
        "urls": {"get": "https://api.replicate.com/v1/predictions/untracked"},
    })
    assert response.status_code == 201
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert await db.get(AsyncTaskRecord, call_id) is None
        assert await db.get(Hold, call_id) is None
        entry = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "release"))).scalar_one()
    # treg's own failure is treg's cost: the whole reserve goes back, nothing is settled.
    assert entry.amount_micro == 3000
    assert entry.meta.get("reason") == "async_task_not_recorded"


@pytest.fixture
def minimax_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_MINIMAX", "test-platform-token")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "minimax")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def openrouter_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_OPENROUTER", "test-platform-token")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "openrouter")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def legacy_async_platform(monkeypatch):
    for provider in ("apify", "brightdata", "companyenrich", "oceanio"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{provider.upper()}", "test-platform-token")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "apify,brightdata,companyenrich,oceanio")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_platform_openrouter_model_is_bound_to_the_selected_catalog_row(
    clients: AsyncClient, monkeypatch, openrouter_platform,
):
    relayed = []

    async def fake_relay(*args, **kwargs):
        relayed.append(True)
        return _response(201, {"id": "video-owned"})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    endpoint = "/call/openrouter.x.google-veo-3-1-lite"
    body = {"model": "black-forest-labs/flux-3-video", "prompt": "A paper boat.",
            "duration": 4, "resolution": "720p", "generate_audio": True}
    refused = await clients.post(endpoint, json=body)
    assert refused.status_code == 400
    assert refused.json()["detail"]["parameter"] == "body.model"
    assert relayed == []
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []

    body["model"] = "google/veo-3.1-lite"
    accepted = await clients.post(endpoint, json=body)
    assert accepted.status_code == 201 and relayed == [True]
    async with session_maker() as db:
        row = (await db.execute(select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_id == "video-owned"))).scalar_one()
    assert row.endpoint_id == "openrouter.x.google-veo-3-1-lite"


async def test_platform_model_selector_rejects_duplicate_json_keys(
    clients: AsyncClient, monkeypatch, openrouter_platform,
):
    async def must_not_relay(*args, **kwargs):
        raise AssertionError("ambiguous model reached the provider")

    monkeypatch.setattr(call_service, "relay", must_not_relay)
    body = (b'{"model":"google/veo-3.1-lite",'
            b'"model":"black-forest-labs/flux-3-video","prompt":"x"}')
    response = await clients.post(
        "/call/openrouter.x.google-veo-3-1-lite", content=body,
        headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert "repeats JSON field" in response.json()["detail"]


async def test_byok_openrouter_remains_a_faithful_relay_for_model_choice(
    clients: AsyncClient, monkeypatch, openrouter_platform,
):
    await clients.post("/secrets", json={"name": "openrouter", "value": "own-token"})
    relayed = []

    async def fake_relay(*args, **kwargs):
        relayed.append(True)
        return _response(201, {"id": "byok-video"})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    response = await clients.post("/call/openrouter.x.google-veo-3-1-lite", json={
        "model": "black-forest-labs/flux-3-video", "prompt": "A paper boat."})
    assert response.status_code == 201 and relayed == [True]
    async with session_maker() as db:
        assert (await db.execute(select(AsyncTaskRecord))).scalars().all() == []


async def test_platform_task_status_requires_same_org_submission(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    submitted = await _submit(clients, monkeypatch, {
        "id": "prediction-owned",
        "urls": {"get": "https://api.replicate.com/v1/predictions/owned"},
    })
    assert submitted.status_code == 201
    relayed = []

    async def fake_status(*args, **kwargs):
        relayed.append(True)
        return _response(200, {"id": "prediction-owned", "status": "processing"})

    monkeypatch.setattr(call_service, "relay", fake_status)
    own = await clients.get("/call/replicate.predictions.get?id=prediction-owned")
    assert own.status_code == 200 and relayed == [True]

    unknown = await clients.get("/call/replicate.predictions.get?id=prediction-unknown")
    assert unknown.status_code == 403 and relayed == [True]
    assert unknown.json()["detail"]["error"] == "async_resource_not_owned"

    ambiguous = await clients.get(
        "/call/replicate.predictions.get",
        params=[("id", "prediction-owned"), ("id", "prediction-unknown")])
    assert ambiguous.status_code == 400 and relayed == [True]

    other = await clients.post("/users", json={"email": "task-stranger@example.com"})
    stranger = {"X-Treg-Token": other.json()["token"]}
    denied = await clients.get(
        "/call/replicate.predictions.get?id=prediction-owned", headers=stranger)
    assert denied.status_code == 403 and relayed == [True]
    assert denied.json()["detail"] == unknown.json()["detail"]


async def test_byok_task_status_keeps_direct_provider_object_access(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    await clients.post("/secrets", json={"name": "replicate", "value": "own-token"})

    async def fake_status(*args, **kwargs):
        return _response(200, {"id": "arbitrary-own-account-id", "status": "processing"})

    monkeypatch.setattr(call_service, "relay", fake_status)
    response = await clients.get(
        "/call/replicate.predictions.get?id=arbitrary-own-account-id")
    assert response.status_code == 200


@pytest.mark.parametrize(("start", "payload", "created", "owned_calls"), [
    (
        "/call/apify.web.scrape.job.start?actor_id=apify~hello-world", {},
        {"data": {"id": "run-owned", "defaultDatasetId": "dataset-owned"}},
        [
            "/call/apify.web.scrape.job.status?run_id=run-owned",
            "/call/apify.web.scrape.job.results?dataset_id=dataset-owned&limit=1",
        ],
    ),
    (
        "/call/brightdata.web.scrape.job.start?dataset_id=gd_test", [{"url": "https://example.com"}],
        {"snapshot_id": "snapshot-owned"},
        [
            "/call/brightdata.web.scrape.job.status?snapshot_id=snapshot-owned",
            "/call/brightdata.web.scrape.job.results?snapshot_id=snapshot-owned&format=json",
        ],
    ),
    (
        "/call/companyenrich.companies.enrich.bulk.start", {"domains": ["example.com"]},
        {"job_id": "job-owned", "status": "pending"},
        ["/call/companyenrich.companies.enrich.bulk.status?jobId=job-owned"],
    ),
    (
        "/call/companyenrich.companies.search.async.start",
        {"count": 1, "search": {"countries": ["US"]}},
        {"job_id": "company-search-owned", "status": "pending"},
        ["/call/companyenrich.companies.search.async.status?jobId=company-search-owned"],
    ),
    (
        "/call/companyenrich.people.email.bulk.start",
        {"items": [{"person_id": 1, "domain": "example.com"}]},
        {"job_id": "people-email-owned", "status": "pending"},
        ["/call/companyenrich.people.email.bulk.status?jobId=people-email-owned"],
    ),
    (
        "/call/companyenrich.people.search.async.start", {"count": 1, "domains": ["example.com"]},
        {"job_id": "people-search-owned", "status": "pending"},
        ["/call/companyenrich.people.search.async.status?jobId=people-search-owned"],
    ),
    (
        "/call/oceanio.companies.segment.create", {"domains": ["example.com"]},
        {"segmentationId": 12345},
        ["/call/oceanio.companies.segment.get?segmentation_id=12345"],
    ),
])
async def test_legacy_platform_async_resources_are_recorded_and_authorized(
    clients: AsyncClient, monkeypatch, legacy_async_platform,
    start: str, payload: object, created: dict, owned_calls: list[str],
):
    responses = [created, {"status": "running"}, []]

    async def fake_relay(*args, **kwargs):
        return _response(200, responses.pop(0))

    monkeypatch.setattr(call_service, "relay", fake_relay)
    submitted = await clients.post(start, json=payload)
    assert submitted.status_code == 200
    for url in owned_calls:
        assert (await clients.get(url)).status_code == 200

    other = await clients.post("/users", json={"email": "legacy-stranger@example.com"})
    denied = await clients.get(owned_calls[0], headers={"X-Treg-Token": other.json()["token"]})
    assert denied.status_code == 403

    async with session_maker() as db:
        records = (await db.execute(select(AsyncResourceRecord))).scalars().all()
    assert records


@pytest.mark.parametrize("url", [
    "/call/apify.web.scrape.job.status?run_id=unknown",
    "/call/apify.web.scrape.job.results?dataset_id=unknown",
    "/call/brightdata.web.scrape.job.status?snapshot_id=unknown",
    "/call/brightdata.web.scrape.job.results?snapshot_id=unknown",
    "/call/companyenrich.companies.enrich.bulk.status?jobId=unknown",
    "/call/companyenrich.companies.search.async.status?jobId=unknown",
    "/call/companyenrich.people.email.bulk.status?jobId=unknown",
    "/call/companyenrich.people.search.async.status?jobId=unknown",
    "/call/oceanio.companies.segment.get?segmentation_id=99999",
])
async def test_legacy_platform_async_utilities_deny_unknown_ids_before_relay(
    clients: AsyncClient, monkeypatch, legacy_async_platform, url: str,
):
    async def must_not_relay(*args, **kwargs):
        raise AssertionError("unowned async resource reached the shared provider account")

    monkeypatch.setattr(call_service, "relay", must_not_relay)
    response = await clients.get(url)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "async_resource_not_owned"


async def test_legacy_platform_async_mutation_denies_unknown_resource_before_relay(
    clients: AsyncClient, monkeypatch, legacy_async_platform,
):
    async def must_not_relay(*args, **kwargs):
        raise AssertionError("unowned segmentation reached the shared provider account")

    monkeypatch.setattr(call_service, "relay", must_not_relay)
    response = await clients.post(
        "/call/oceanio.companies.segment.mark_domains?segmentation_id=99999",
        json={"domains": ["example.com"], "type": "positive"},
    )
    assert response.status_code == 403


async def test_legacy_byok_async_utility_remains_unrestricted(
    clients: AsyncClient, monkeypatch, legacy_async_platform,
):
    await clients.post("/secrets", json={"name": "apify", "value": "own-token"})

    async def fake_relay(*args, **kwargs):
        return _response(200, {"data": {"id": "own-account-run"}})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    response = await clients.get("/call/apify.web.scrape.job.status?run_id=arbitrary")
    assert response.status_code == 200


async def _submit_minimax(clients: AsyncClient, monkeypatch, task_id: str) -> str:
    async def fake_submit(*args, **kwargs):
        return _response(200, {"task_id": task_id, "base_resp": {"status_code": 0}})

    monkeypatch.setattr(call_service, "relay", fake_submit)
    response = await clients.post("/call/minimax.video-gen.from_text", json={
        "model": "MiniMax-Hailuo-2.3", "prompt": "A paper boat.", "duration": 6,
        "resolution": "768P"})
    assert response.status_code == 200
    return response.headers["X-Treg-Call-Id"]


async def test_owned_terminal_poll_teaches_result_id_before_fetch(
    clients: AsyncClient, monkeypatch, minimax_platform,
):
    call_id = await _submit_minimax(clients, monkeypatch, "minimax-task-owned")
    relay_calls = []

    async def fake_relay(*args, **kwargs):
        relay_calls.append(True)
        if len(relay_calls) == 1:
            return _response(200, {"status": "Success", "file_id": "minimax-file-owned"})
        return _response(200, {"file": {"download_url": "https://example.invalid/video.mp4"}})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    polled = await clients.get(
        "/call/minimax.video-gen.task.status?task_id=minimax-task-owned")
    assert polled.status_code == 200
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
    assert row.result_id == "minimax-file-owned"

    fetched = await clients.get(
        "/call/minimax.video-gen.result.retrieve?file_id=minimax-file-owned")
    assert fetched.status_code == 200 and len(relay_calls) == 2

    other = await clients.post("/users", json={"email": "file-stranger@example.com"})
    denied = await clients.get(
        "/call/minimax.video-gen.result.retrieve?file_id=minimax-file-owned",
        headers={"X-Treg-Token": other.json()["token"]})
    assert denied.status_code == 403 and len(relay_calls) == 2


async def test_worker_terminal_success_persists_fetch_result_ownership(
    clients: AsyncClient, monkeypatch, minimax_platform,
):
    call_id = await _submit_minimax(clients, monkeypatch, "minimax-task-worker")
    outcome = await task_app._finish(
        call_id, "success", {"status": "Success", "file_id": "minimax-file-worker"},
        utcnow_naive())
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
    assert outcome == "settled" and row.result_id == "minimax-file-worker"


async def test_a_2xx_that_fails_the_expect_rule_releases_and_is_not_deferred(
    clients: AsyncClient, monkeypatch, minimax_platform,
):
    """MiniMax answers HTTP 200 with the error in the envelope (live 2026-09-02: base_resp 2013,
    "model MiniMax-Hailuo-2.3-Fast does not support Text-to-Video mode"). No task exists, so
    nothing may be deferred and nothing may be charged."""
    async def fake_relay(*args, **kwargs):
        return _response(200, {"task_id": "", "base_resp": {
            "status_code": 2013, "status_msg": "invalid params"}})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    response = await clients.post("/call/minimax.video-gen.from_text", json={
        "model": "MiniMax-Hailuo-2.3", "prompt": "A paper boat.", "duration": 6,
        "resolution": "768P"})
    assert response.status_code == 200
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert await db.get(AsyncTaskRecord, call_id) is None
        assert await db.get(Hold, call_id) is None
        entries = {e.kind: e.amount_micro for e in (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id))).scalars().all()}
    # A failed envelope is a per_success miss: settled at zero, the whole reserve given back.
    assert entries == {"reserve": -280000, "settle": 0}


async def _due_submission(clients, monkeypatch, document: dict) -> str:
    response = await _submit(clients, monkeypatch, {
        "id": "prediction-worker",
        "urls": {"get": "https://api.replicate.com/v1/predictions/worker"},
    })
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        row.next_check_at = utcnow_naive() - timedelta(seconds=1)
        await db.commit()

    async def fake_poll(row, client):
        return 200, json.dumps(document).encode()

    monkeypatch.setattr(task_app, "_poll", fake_poll)
    return call_id


async def test_worker_settles_terminal_success(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "succeeded", "output": ["url"]})
    result = await task_app.settle_due()
    assert result.settled == 1
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
        entry = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "settle"))).scalar_one()
    assert row.status == "settled" and row.settled_micro == 3000
    assert hold is None and entry.amount_micro == -3000
    async with session_maker() as db:
        key = (await db.execute(select(ArchiveKey).where(
            ArchiveKey.req_url == f"treg://asynctasks/{call_id}"))).scalar_one()
        snapshot = (await db.execute(select(ArchiveSnapshot).where(
            ArchiveSnapshot.key_id == key.id))).scalar_one()
        report = await reconcile.async_task_settlement(
            db, utcnow_naive() - timedelta(hours=1))
    assert json.loads(snapshot.body)["status"] == "succeeded"
    assert report["providers"] == [{
        "provider": "replicate", "successes": 1, "failures": 0,
        "settled_micro": 3000, "tasks": 1, "success_rate": 1.0,
        "settled_usd": 0.003,
    }]


async def test_worker_releases_terminal_failure(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "failed", "error": "rejected"})
    result = await task_app.settle_due()
    assert result.released == 1
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
    assert row.status == "released" and row.settled_micro == 0 and hold is None


async def test_worker_backs_off_unknown_status(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "provider_added_a_state"})
    before = utcnow_naive()
    result = await task_app.settle_due()
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
    assert result.backed_off == 1
    assert row.status == "pending" and row.next_check_at > before and hold is not None
    async with session_maker() as db:
        hold = await db.get(Hold, call_id)
        hold.created_at = utcnow_naive() - timedelta(seconds=ledger.hold_ttl_s() + 1)
        await db.commit()
        assert await ledger.reap_stale_holds(db, org_id=row.org_id) == 0
        assert await db.get(Hold, call_id) is not None


async def test_worker_timeout_releases_the_hold_and_flags_it_for_review(
    clients: AsyncClient, monkeypatch, replicate_platform, caplog,
):
    """An outcome nobody observed is the platform's cost, never the customer's."""
    call_id = await _due_submission(clients, monkeypatch, {"status": "processing"})
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        row.created_at = utcnow_naive() - asynctasks.MAX_AGE - timedelta(seconds=1)
        row.next_check_at = utcnow_naive() - timedelta(seconds=1)
        await db.commit()
    with caplog.at_level("ERROR", logger="treg.asynctasks"):
        result = await task_app.settle_due()
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
        entry = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "release"))).scalar_one()
    assert result.timed_out == 1
    assert row.status == "timed_out" and row.settled_micro == 0 and row.reserved_micro == 3000
    assert hold is None and entry.meta.get("reconcile_review") is True
    assert any("ASYNC TASK TIMED OUT" in rec.message for rec in caplog.records)
    async with session_maker() as db:
        report = await reconcile.async_task_settlement(
            db, utcnow_naive() - timedelta(hours=1))
    assert [item["call_id"] for item in report["absorbed_timeouts"]] == [call_id]
    assert report["absorbed_timeouts"][0]["reserved_micro"] == 3000


def test_basis_derivation_and_settlement_table_vs_usage():
    table_cost = {"table": [{"when": {"body.n": 2}, "value": 0.01}],
                  "fallback": {"value": 0.04}, "settle": "table"}
    table = settlement.derive_basis(
        table_cost, request={"body": {"n": 2}}, input_schema={}, unit_micro=1_000_000,
        terminal=True)
    assert table["amount"]["kind"] == "table"
    assert settlement.settle(table, {"terminal": {}}) == 10_000

    usage_cost = {"settle": "usage", "usage": {"path": "usage.cost", "unit": "usd"},
                  "fallback": {"value": 1.0}}
    usage = settlement.derive_basis(
        usage_cost, request={}, input_schema={}, unit_micro=1_000_000, terminal=True)
    assert usage["amount"]["kind"] == "usage"
    assert settlement.settle(usage, {"terminal": {"usage": {"cost": 0.125}}}) == 125_000
    # No table row matched and no usage figure: the reserve is the fallback, and a success with
    # no usage evidence settles at that reserve, never above it.
    assert usage["reserve_micro"] == usage["fallback_micro"] == 1_000_000
    assert settlement.usage_evidence(usage, {"terminal": {"status": "completed"}}) is None
    assert settlement.settle(usage, {"terminal": {"status": "completed"}}) == 1_000_000

    # A usage row reserves what the rate card says THIS request costs, not the matrix ceiling.
    rate_card = {"settle": "usage", "usage": {"path": "usage.cost", "unit": "usd"},
                 "table": [{"when": {"body.resolution": "480p"}, "value": 0.05, "times": "body.duration"},
                           {"when": {"body.resolution": "1080p"}, "value": 0.20, "times": "body.duration"}],
                 "fallback": {"value": 6.0}}
    schema = {"body": {"resolution": {"type": "string"}, "duration": {"type": "integer", "max": 30}}}
    cheap = settlement.derive_basis(
        rate_card, request={"body": {"resolution": "480p", "duration": 2}}, input_schema=schema,
        unit_micro=1_000_000, terminal=True)
    assert cheap["reserve_micro"] == 100_000 and cheap["fallback_micro"] == 6_000_000
    # The provider's reported cost settles even when it exceeds the reserve (Wan 3.0's minimum).
    assert settlement.settle(cheap, {"terminal": {"usage": {"cost": 0.2125}}}) == 212_500

    request = settlement.request_evidence(
        [("id", "42"), ("count", "2")], b"{}", path_names={"id"})
    path_table = {"table": [{"when": {"pathParams.id": 42}, "value": 0.01,
                             "times": "queryParams.count"}],
                  "fallback": {"value": 0.10}, "settle": "table"}
    schema = {"pathParams": {"id": {"type": "integer"}},
              "queryParams": {"count": {"type": "integer"}}}
    basis = settlement.derive_basis(
        path_table, request=request, input_schema=schema, unit_micro=1_000_000, terminal=True)
    assert basis["reserve_micro"] == 20_000


def test_terminal_classification_coerces_status_values_and_treats_none_as_progress():
    descriptor = {
        "status": {
            "path": "task.status",
            "success": [2],
            "failure": ["3"],
        },
    }

    assert asynctasks.classify_terminal(descriptor, {"task": {"status": "2"}}) == "success"
    assert asynctasks.classify_terminal(descriptor, {"task": {"status": 3}}) == "failure"
    assert asynctasks.classify_terminal(descriptor, {"task": {"status": None}}) == "progress"
    assert asynctasks.classify_terminal(descriptor, {"task": {}}) == "progress"


async def _activity_row(clients: AsyncClient, call_id: str) -> dict:
    await audit.drain()
    await archive.drain()
    rows = (await clients.get("/calls")).json()
    return next(row for row in rows if row["call_ref"] == call_id)


async def test_activity_reports_task_state_and_artifact(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    """The audit row froze the reserve as the charge; the feed must show what actually happened."""
    call_id = await _due_submission(clients, monkeypatch, {
        "status": "succeeded", "output": ["https://replicate.delivery/out.webp"],
        # Keep the terminal envelope above archive._COMPRESS_MIN_BYTES. Production video status
        # bodies are compressed, which is where the activity reader once returned encoded bytes
        # to json.loads and silently lost the artifact.
        "provider_metadata": "x" * 512,
    })
    pending = await _activity_row(clients, call_id)
    assert pending["cost_charged_micro"] is None
    assert pending["async_task"]["status"] == "pending"
    assert pending["async_task"]["reserved_micro"] == 3000
    assert pending["async_task"]["result_url"] is None

    assert (await task_app.settle_due()).settled == 1
    settled = await _activity_row(clients, call_id)
    assert settled["cost_charged_micro"] == 3000
    task = settled["async_task"]
    assert task["status"] == "settled" and task["settled_micro"] == 3000
    assert task["result_url"] == "https://replicate.delivery/out.webp"
    assert task["completed_at"] is not None

    one = (await clients.get(f"/calls/{call_id}")).json()
    assert one["async_task"]["result_url"] == "https://replicate.delivery/out.webp"
    assert one["call"]["cost_charged_micro"] == 3000 and one["charged_micro"] == 3000


async def test_activity_reports_refund_after_failure(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "failed", "error": "nsfw"})
    assert (await task_app.settle_due()).released == 1
    row = await _activity_row(clients, call_id)
    assert row["cost_charged_micro"] == 0
    assert row["async_task"]["status"] == "released"
    assert row["async_task"]["result_url"] is None


def test_artifact_reads_both_result_modes():
    by_path = {"result": {"path": "task.content.url", "ttl_note": "time-limited"}}
    found = asynctasks.artifact(by_path, {"task": {"content": {"url": "https://x.invalid/v.mp4"}}})
    assert found["result_url"] == "https://x.invalid/v.mp4" and found["ttl_note"] == "time-limited"
    assert found["fetch"] is None
    by_fetch = {"result": {"fetch": "minimax.video-gen.result.retrieve",
                           "fetch_param": {"in": "queryParams", "name": "file_id",
                                           "value_from": "file_id"},
                           "ttl_note": "9h"}}
    found = asynctasks.artifact(by_fetch, {"status": "Success", "file_id": "f-1"})
    assert found["result_url"] is None
    assert found["fetch"] == {"endpoint": "minimax.video-gen.result.retrieve",
                              "name": "file_id", "value": "f-1"}
    assert asynctasks.artifact(by_fetch, {"status": "Success"})["fetch"] is None
    assert asynctasks.artifact({}, {"anything": 1})["result_url"] is None


async def test_query_parameter_poll_travels_as_query_items(monkeypatch):
    """MiniMax v1 polls `GET /v1/query/video_generation?task_id=…`. The relay builds the upstream
    query from `query_items` only, so the id must ride there (live 2026-09-02: appended to the URL it
    arrived empty and the provider answered 2013 "invalid params" on every tick)."""
    seen = {}

    async def fake_relay(request, url, tool, *args, **kwargs):
        seen["url"], seen["query"] = url, request.query_items

        async def stream():
            yield b'{"status": "Success"}'

        async def close():
            return None

        return UpstreamResponse(200, (), stream(), close)

    monkeypatch.setattr(task_app, "relay", fake_relay)
    row = AsyncTaskRecord(
        call_id="q-1", org_id=1, provider="minimax", endpoint_id="minimax.video-gen.from_text",
        task_id="437372532953204", reserved_micro=1, next_check_at=utcnow_naive(),
        descriptor={"poll": {"endpoint": "minimax.video-gen.task.status",
                             "param": {"in": "queryParams", "name": "task_id"}}})
    status, body = await task_app._poll(row, None)
    assert status == 200 and body == b'{"status": "Success"}'
    assert seen["url"] == "https://api.minimax.io/v1/query/video_generation"
    assert seen["query"] == (("task_id", "437372532953204"),)


async def test_reconcile_lists_usage_overruns_and_platform_absorbed_shortfalls(clients: AsyncClient):
    """The two places a usage-settled task can cost more than its reserve, made visible: the team
    paid the overrun from its balance; the platform absorbed whatever its blocks could not cover."""
    now = utcnow_naive()
    async with session_maker() as db:
        db.add(AsyncTaskRecord(
            call_id="over-1", org_id=1, provider="openrouter",
            endpoint_id="openrouter.video-gen.wan-3-0.from_text", task_id="t", reserved_micro=100_000,
            settled_micro=212_500, status="settled", created_at=now, next_check_at=now,
            completed_at=now, descriptor={}, settlement_basis={}))
        db.add(AsyncTaskRecord(
            call_id="even-1", org_id=1, provider="openrouter",
            endpoint_id="openrouter.x.google-veo-3-1", task_id="u", reserved_micro=800_000,
            settled_micro=800_000, status="settled", created_at=now, next_check_at=now,
            completed_at=now, descriptor={}, settlement_basis={}))
        db.add(LedgerEntry(
            id="le-over-1", org_id=1, kind="settle", amount_micro=-120_000, call_id="over-1",
            endpoint_id="openrouter.video-gen.wan-3-0.from_text", created_at=now,
            meta={"settled_micro": 212_500, "consumed_micro": 120_000, "block_shortfall_micro": 92_500}))
        db.add(LedgerEntry(
            id="le-even-1", org_id=1, kind="settle", amount_micro=-800_000, call_id="even-1",
            endpoint_id="openrouter.x.google-veo-3-1", created_at=now,
            meta={"settled_micro": 800_000, "consumed_micro": 800_000, "block_shortfall_micro": 0}))
        await db.commit()
        report = await reconcile.async_task_settlement(db, now - timedelta(hours=1))
    assert [o["call_id"] for o in report["overruns"]] == ["over-1"]
    assert report["overruns"][0]["overrun_micro"] == 112_500 and report["overruns"][0]["ratio"] == 2.125
    assert report["overruns_by_endpoint"] == [{
        "endpoint_id": "openrouter.video-gen.wan-3-0.from_text", "provider": "openrouter",
        "tasks": 1, "overrun_micro": 112_500, "max_ratio": 2.125}]
    assert [s["call_id"] for s in report["absorbed_shortfalls"]] == ["over-1"]
    assert report["absorbed_shortfall_micro"] == 92_500


def test_times_multiplier_is_bounded_by_the_input_schema():
    """A caller cannot reserve zero with duration 0 or bill past the ceiling with duration 100:
    an out-of-range, non-finite or non-positive multiplier matches no row and prices at the fallback."""
    cost = {"table": [{"when": {"body.input.resolution": "480p"}, "value": 0.05,
                       "times": "body.input.duration"}],
            "fallback": {"value": 6.0}}
    schema = {"body": {"input": {"type": "object", "properties": {
        "resolution": {"type": "string"},
        "duration": {"type": "integer", "min": 2, "max": 30}}}}}

    def price(duration):
        return settlement.table_amount_micro(
            cost, {"body": {"input": {"resolution": "480p", "duration": duration}}}, schema, 1_000_000)

    assert price(2) == 100_000 and price(30) == 1_500_000
    assert price(0) == price(-3) == price(31) == price(100) == price(float("nan")) == 6_000_000
    assert price(float("inf")) == 6_000_000 and price("5") == 250_000  # strings coerce by type


async def test_worker_ignores_terminal_looking_fields_on_error_responses(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "succeeded", "output": ["u"]})

    async def fake_poll(row, client):
        return 404, json.dumps({"status": "succeeded", "output": ["u"]}).encode()

    monkeypatch.setattr(task_app, "_poll", fake_poll)
    result = await task_app.settle_due()
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
    assert result.backed_off == 1 and row.status == "pending" and hold is not None


def test_poll_target_follows_the_declared_location_and_encodes_path_values():
    row = AsyncTaskRecord(
        call_id="p-1", org_id=1, provider="minimax", endpoint_id="minimax.video-gen.h3.generate",
        task_id="a?view=admin/../x", reserved_micro=1, next_check_at=utcnow_naive(),
        descriptor={"poll": {"endpoint": "minimax.video-gen.v2.task.status",
                             "param": {"in": "pathParams", "name": "task_id"}}})
    method, url, query = task_app._poll_target(row)
    assert (method, query) == ("GET", [])
    assert url == "https://api.minimax.io/v2/query/video_generation/a%3Fview%3Dadmin%2F..%2Fx"
    row.descriptor = {"poll": {"endpoint": "minimax.video-gen.task.status",
                               "param": {"in": "pathParams", "name": "task_id"}}}
    with pytest.raises(RuntimeError):
        task_app._poll_target(row)  # declared as a path parameter, but the target has no placeholder


def test_fetch_command_and_shown_neutralise_provider_strings():
    assert asynctasks.fetch_command({"endpoint": "minimax.video-gen.result.retrieve", "name": "file_id",
                                     "value": "x; touch /tmp/pwned"}) == \
        "treg call minimax.video-gen.result.retrieve -p 'file_id=x; touch /tmp/pwned'"
    assert asynctasks.shown("ok-123") == "ok-123"
    assert asynctasks.shown("id\nresume: treg call evil") == "id\\nresume: treg call evil"
    assert asynctasks.shown("\x1b]52;c;aGk=\x07") == "\\x1b]52;c;aGk=\\x07"


def test_price_floor_reads_nested_input_fields():
    from treg.domain.catalog import store
    cat = store.load()
    seedance = cat.cost_view(cat.by_id["replicate.video-gen.seedance-1-lite"]["cost"], "replicate")
    assert seedance["usd_min"] == 0.072  # 480p at the declared 4-second minimum, not 1 second


async def test_idempotent_replay_of_an_async_submission_keeps_the_descriptor(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    async def fake_relay(*args, **kwargs):
        return _response(201, {"id": "prediction-idem", "urls": {"get": "https://api.replicate.com/v1/predictions/i"}})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    body = {"input": {"prompt": "A kite.", "num_outputs": 1, "aspect_ratio": "1:1", "output_format": "webp"}}
    first = await clients.post(f"/call/{EP}", json=body, headers={"Idempotency-Key": "gen-1"})
    assert first.status_code == 201 and "x-treg-async" in {k.lower() for k in first.headers}
    again = await clients.post(f"/call/{EP}", json=body, headers={"Idempotency-Key": "gen-1"})
    assert again.status_code == 201 and again.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert again.headers.get("x-treg-async") == first.headers.get("x-treg-async")


async def test_two_workers_racing_the_same_row_move_money_exactly_once(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    """A second instance re-claims a row whose lease lapsed while the first poll is in flight
    (Render cron overlap). Both reach _finish; the row lock and the once-only hold claim leave one
    settle entry and one terminal state. Meaningful on Postgres (FOR UPDATE SKIP LOCKED)."""
    call_id = await _due_submission(clients, monkeypatch, {"status": "succeeded", "output": ["u"]})
    first_polling = asyncio.Event()
    release_first = asyncio.Event()
    polls = 0

    async def slow_poll(row, client):
        nonlocal polls
        polls += 1
        if polls == 1:
            async with session_maker() as db:  # the lease lapses while this poll is in flight
                live = await db.get(AsyncTaskRecord, row.call_id)
                live.next_check_at = utcnow_naive() - timedelta(seconds=1)
                await db.commit()
            first_polling.set()
            await release_first.wait()
        return 200, json.dumps({"status": "succeeded", "output": ["u"]}).encode()

    monkeypatch.setattr(task_app, "_poll", slow_poll)
    first = asyncio.create_task(task_app.settle_due())
    await asyncio.wait_for(first_polling.wait(), 10)
    second = await task_app.settle_due()          # re-claims the lapsed lease and settles
    release_first.set()
    first_result = await first
    assert second.claimed == 1 and first_result.claimed == 1
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        settles = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "settle"))).scalars().all()
        hold = await db.get(Hold, call_id)
    assert row.status == "settled" and row.settled_micro == 3000
    assert len(settles) == 1 and settles[0].amount_micro == -3000 and hold is None


async def test_cancellation_at_the_pending_row_commit_boundary_leaves_a_coherent_outcome(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    """The request is cancelled the instant the pending row commits: the request path releases the
    hold it still owns, and the worker must then record the row as released, not settle it at zero."""
    real_defer = task_app.defer_submission

    async def defer_then_cancel(mk, body, org_id):
        await real_defer(mk, body, org_id)
        mk.call_id = mk.call_id or None
        raise asyncio.CancelledError()

    monkeypatch.setattr(task_app, "defer_submission", defer_then_cancel)
    # The request path re-raises CancelledError after compensating; the ASGI client surfaces it.
    with pytest.raises(BaseException):
        await _submit(clients, monkeypatch, {
            "id": "prediction-cancel", "urls": {"get": "https://api.replicate.com/v1/predictions/c"}})
    async with session_maker() as db:
        row = (await db.execute(select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_id == "prediction-cancel"))).scalar_one()
        hold = await db.get(Hold, row.call_id)
        kinds = sorted(e.kind for e in (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == row.call_id))).scalars().all())
        row.next_check_at = utcnow_naive() - timedelta(seconds=1)
        await db.commit()
        call_id = row.call_id
    assert row.status == "pending"
    if hold is None:  # compensation released the hold while the row was already durable
        assert kinds == ["release", "reserve"]

        async def fake_poll(row, client):
            return 200, json.dumps({"status": "succeeded", "output": ["u"]}).encode()

        monkeypatch.setattr(task_app, "_poll", fake_poll)
        await task_app.settle_due()
        async with session_maker() as db:
            row = await db.get(AsyncTaskRecord, call_id)
            kinds = sorted(e.kind for e in (await db.execute(select(LedgerEntry).where(
                LedgerEntry.call_id == call_id))).scalars().all())
        assert row.status == "released" and row.settled_micro == 0
        assert kinds == ["release", "reserve"], "no settle may follow a released hold"
    else:  # the hold survived with its row: the worker owns it from here, nothing was double-moved
        assert kinds == ["reserve"]


async def test_another_org_cannot_read_a_task_by_its_call_ref(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "succeeded", "output": ["u"]})
    assert (await task_app.settle_due()).settled == 1
    assert call_id in await task_app.views_for(1, [call_id])
    assert await task_app.views_for(2, [call_id]) == {}
    other = await clients.post("/users", json={"email": "someone-else@example.com"})
    assert other.status_code == 200
    stranger = {"X-Treg-Token": other.json()["token"]}
    assert (await clients.get(f"/calls/{call_id}", headers=stranger)).status_code == 404
    assert call_id not in {r.get("call_ref") for r in (await clients.get("/calls", headers=stranger)).json()}
