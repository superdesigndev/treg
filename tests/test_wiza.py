"""Wiza provider registration, bounded platform pricing, BYOK and routing."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlmodel import select

from treg.application import asynctasks as async_task_app
from treg.application.call import service as call_service
from treg.application.call import route as call_route
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.capacity import collectors, policy
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, Hold


async def _balance(clients) -> int:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


@pytest.fixture
def wiza_platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_WIZA", "PLATFORM-WIZA")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "wiza")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("remaining", [5000, 0, 12.5])
async def test_wiza_capacity_reads_finite_api_credits(remaining):
    def serve(request):
        assert request.url.path == "/api/meta/credits"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"credits": {"api_credits": remaining}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        row = await collectors._wiza(upstream, "test-key")
    assert row["value"] == remaining
    assert row["unit"] == "API credits"
    assert "auto-top-up is not enabled" in row["note"]
    configured = policy.default_policy("wiza", has_key=True)
    assert configured.capacity_type == "credits"
    assert configured.funding_mode == "manual"
    assert configured.auto_funding_enabled is False
    assert configured.rate_limit == {"limit": 30, "window_s": 60, "source": "docs"}


@pytest.mark.parametrize("remaining", [None, True, -1, "unlimited"])
async def test_wiza_capacity_rejects_unclear_balances(remaining):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"credits": {"api_credits": remaining}}))) as upstream:
        with pytest.raises(ValueError, match="valid API credit balance"):
            await collectors._wiza(upstream, "test-key")


async def test_wiza_capacity_rejects_non_finite_balance():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"credits": {"api_credits": float("inf")}}

    class Client:
        async def get(self, *args, **kwargs):
            return Response()

    with pytest.raises(ValueError, match="valid API credit balance"):
        await collectors._wiza(Client(), "test-key")


async def test_wiza_routed_email_waits_for_terminal_result_and_settles_exact_usage(
    clients, monkeypatch, wiza_platform_on,
):
    calls = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        calls.append((request.method, upstream_url, json.loads(body) if body else None))
        doc = ({"data": {"id": 321, "status": "queued"}}
               if request.method == "POST" else
               {"data": {"id": 321, "status": "finished", "name": "Jane Example",
                         "email": "jane@example.com", "email_status": "valid",
                         "credits": {"api_credits": {"total": 2}}}})
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    result = await clients.post(
        "/call/treg.people.email.find",
        headers={"X-Treg-Route-Prefer": "wiza"},
        json={"full_name": "Jane Example", "domain": "example.com"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["output"] == {
        "email": "jane@example.com", "first_name": "Jane", "last_name": "Example",
        "verified": True,
    }
    assert result.json()["_treg"]["served_by"] == "wiza.people.email.find"
    assert result.headers["x-treg-cost-micro"] == "50000"
    assert await _balance(clients) == before - 50_000
    assert calls[0][2] == {
        "individual_reveal": {"full_name": "Jane Example", "domain": "example.com"},
        "enrichment_level": "partial",
        "email_options": {"accept_work": True, "accept_personal": False, "accept_generic": False},
    }
    assert calls[1][0] == "GET" and calls[1][1].endswith("/api/individual_reveals/321")
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().first()
        assert task.status == "settled" and task.settled_micro == 50_000
        assert await db.get(Hold, task.call_id) is None


async def test_wiza_routed_timeout_is_pending_keeps_hold_and_does_not_resubmit(
    clients, monkeypatch, wiza_platform_on,
):
    submissions = 0

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        nonlocal submissions
        if request.method == "POST":
            submissions += 1
        payload = json.dumps({"data": {"id": 654, "status": "queued"}}).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.01)
    headers = {"X-Treg-Route-Prefer": "wiza", "Idempotency-Key": "wiza-pending-once"}
    first = await clients.post(
        "/call/treg.people.phone.find", headers=headers,
        json={"linkedin_url": "https://www.linkedin.com/in/example"},
    )
    second = await clients.post(
        "/call/treg.people.phone.find", headers=headers,
        json={"linkedin_url": "https://www.linkedin.com/in/example"},
    )
    assert first.status_code == second.status_code == 202
    assert submissions == 1
    meta = first.json()["_treg"]
    assert meta["outcome"] == "pending" and meta["charged_micro"] is None
    assert meta["reserved_micro"] == 150_000 and meta["call_ref"]
    assert "x-treg-cost-micro" not in first.headers
    assert first.headers["x-treg-reserved-micro"] == "150000"
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().first()
        assert task.status == "pending" and task.reserved_micro == 150_000
        assert await db.get(Hold, task.call_id) is not None


async def test_wiza_routed_poll_404_stays_pending_without_paid_fallback(
    clients, monkeypatch, wiza_platform_on,
):
    monkeypatch.setenv("TREG_PLATFORM_KEY_HUNTER", "PLATFORM-HUNTER")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "wiza,hunter")
    get_settings.cache_clear()
    endpoint = catalog_store.load().by_id["wiza.people.email.find"]
    monkeypatch.setitem(endpoint["async"], "interval", 0.01)
    calls = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        def response(status, doc):
            payload = json.dumps(doc).encode()

            async def stream():
                yield payload

            async def close():
                return None

            return UpstreamResponse(
                status, ((b"content-type", b"application/json"),), stream(), close)

        provider = "wiza" if "wiza.co" in upstream_url else "hunter"
        calls.append((provider, request.method))
        if provider == "hunter":
            return response(200, {"data": {"email": "fallback@example.com", "score": 90}})
        if request.method == "POST":
            return response(200, {"data": {"id": 655, "status": "queued"}})
        return response(404, {"status": {"code": 404, "message": "Not ready"}})

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    result = await clients.post(
        "/call/treg.people.email.find",
        headers={"X-Treg-Route-Prefer": "wiza"},
        json={"full_name": "Jane Example", "domain": "example.com"},
    )
    assert result.status_code == 202, result.text
    assert result.json()["_treg"]["outcome"] == "pending"
    assert result.json()["_treg"]["charged_micro"] is None
    assert calls == [("wiza", "POST"), ("wiza", "GET")]
    assert await _balance(clients) == before - 75_000

    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().one()
        call_id = task.call_id
        task.next_check_at = task.created_at
        db.add(task)
        await db.commit()
        assert task.status == "pending" and await db.get(Hold, call_id) is not None

    async def terminal_poll(row, client):
        return 200, json.dumps({
            "data": {"id": 655, "status": "finished", "name": "Jane Example",
                     "email": "jane@example.com", "email_status": "valid",
                     "credits": {"api_credits": {"total": 2}}},
        }).encode()

    monkeypatch.setattr(async_task_app, "_poll", terminal_poll)
    settled = await async_task_app.settle_due()
    assert settled.settled == 1
    assert await _balance(clients) == before - 50_000
    async with session_maker() as db:
        task = await db.get(AsyncTaskRecord, call_id)
        assert task.status == "settled" and task.settled_micro == 50_000
        assert await db.get(Hold, call_id) is None


async def test_wiza_failed_reveal_releases_hold_and_is_a_waterfall_miss(
    clients, monkeypatch, wiza_platform_on,
):
    endpoint = catalog_store.load().by_id["wiza.people.email.find"]
    monkeypatch.setitem(endpoint["async"], "interval", 0.01)
    answers = [
        {"data": {"id": 987, "status": "queued"}},
        {"data": {"id": 987, "status": "failed",
                  "credits": {"api_credits": {"total": 0}}}},
    ]

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        payload = json.dumps(answers.pop(0)).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    result = await clients.post(
        "/call/treg.people.email.find", headers={"X-Treg-Route-Prefer": "wiza"},
        json={"full_name": "Nobody Here", "domain": "example.com"},
    )
    assert result.status_code == 200
    assert result.json()["_treg"]["outcome"] == "miss"
    assert result.headers["x-treg-cost-micro"] == "0"
    assert await _balance(clients) == before
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().first()
        assert task.status == "released" and task.settled_micro == 0
        assert await db.get(Hold, task.call_id) is None
