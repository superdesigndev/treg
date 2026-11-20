"""Wiza provider registration, bounded platform pricing, BYOK and routing."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlmodel import select

from treg import audit
from treg.application import asynctasks as async_task_app
from treg.application.call import service as call_service
from treg.application.call import route as call_route
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.capacity import collectors, policy
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, CallRecord, Hold


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
    polls = 0

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        nonlocal polls
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        calls.append((request.method, upstream_url, json.loads(body) if body else None))
        if request.method == "POST":
            doc = {"data": {"id": 321, "status": "queued"}}
        else:
            polls += 1
            doc = ({"data": {"id": 321, "status": "resolving"}} if polls == 1 else
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
    assert polls == 2
    await audit.drain()
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().first()
        assert task.status == "settled" and task.settled_micro == 50_000
        assert await db.get(Hold, task.call_id) is None
        child = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == task.call_id,
            CallRecord.endpoint_id == "wiza.people.email.find"))).scalar_one()
        assert child.hit is True
        assert child.verdict == task.verdict == "verified"


# A finder's verdict is its own claim: Wiza's `risky` is a found address it did not confirm.
@pytest.mark.parametrize("endpoint,level,terminal,expected_hit,expected_verdict", [
    ("wiza.people.email.find", "partial",
     {"email": "person@sample.example", "email_status": "risky"}, True, "unverified"),
    ("wiza.people.phone.find", "phone", {"phone_status": "unfound"}, False, None),
])
async def test_wiza_direct_hit_waits_for_terminal_result(
    clients, monkeypatch, wiza_platform_on, endpoint, level, terminal, expected_hit, expected_verdict,
):
    await audit.drain()
    polls = 0

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        nonlocal polls
        if request.method == "POST":
            doc = {"data": {"id": 4321, "status": "queued"}}
        else:
            polls += 1
            doc = ({"data": {"id": 4321, "status": "resolving"}} if polls == 1 else
                   {"data": {"id": 4321, "status": "finished", **terminal,
                             "credits": {"api_credits": {"total": 0}}}})
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    response = await clients.post(f"/call/{endpoint}", json={
        "individual_reveal": {"full_name": "Person Example", "domain": "sample.example"},
        "enrichment_level": level,
        **({"email_options": {"accept_work": True, "accept_personal": False,
                               "accept_generic": False}} if level == "partial" else {}),
    })
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "queued"
    call_ref = response.headers["x-treg-call-id"]
    await audit.drain()
    async with session_maker() as db:
        original = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == call_ref, CallRecord.endpoint_id == endpoint))).scalar_one()
        assert original.hit is None and original.verdict is None

    first = await clients.get("/call/wiza.people.reveal.get", params={"id": 4321})
    second = await clients.get("/call/wiza.people.reveal.get", params={"id": 4321})
    assert first.json()["data"]["status"] == "resolving"
    assert second.json()["data"]["status"] == "finished"
    await audit.drain()
    async with session_maker() as db:
        original = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == call_ref, CallRecord.endpoint_id == endpoint))).scalar_one()
        task = await db.get(AsyncTaskRecord, call_ref)
        assert original.hit is expected_hit
        assert task.hit is expected_hit
        assert original.verdict == task.verdict == expected_verdict
        assert task.status == "settled" and task.settled_micro == 0
    assert await _balance(clients) == before


async def test_wiza_terminal_hit_precedes_audit_insert(clients, monkeypatch, wiza_platform_on):
    await audit.drain()
    # Simulate a busy audit writer: the terminal poll completes before its queued insert.
    monkeypatch.setattr(audit, "_schedule", lambda coro: coro.close())
    polls = 0

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        nonlocal polls
        if request.method == "POST":
            doc = {"data": {"id": 9876, "status": "queued"}}
        else:
            polls += 1
            doc = ({"data": {"id": 9876, "status": "resolving"}} if polls == 1 else
                   {"data": {"id": 9876, "status": "finished",
                             "email": "person@sample.example", "email_status": "risky",
                             "credits": {"api_credits": {"total": 0}}}})
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post("/call/wiza.people.email.find", json={
        "individual_reveal": {"full_name": "Person Example", "domain": "sample.example"},
        "enrichment_level": "partial",
        "email_options": {"accept_work": True, "accept_personal": False,
                          "accept_generic": False},
    })
    assert response.status_code == 200
    call_ref = response.headers["x-treg-call-id"]
    await clients.get("/call/wiza.people.reveal.get", params={"id": 9876})
    await clients.get("/call/wiza.people.reveal.get", params={"id": 9876})
    await audit.drain()
    async with session_maker() as db:
        original = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == call_ref,
            CallRecord.endpoint_id == "wiza.people.email.find"))).scalar_one()
        assert original.hit is True
        assert original.verdict == "unverified"


async def test_wiza_terminal_poll_does_not_wait_for_audit_writer(
    clients, monkeypatch, wiza_platform_on,
):
    await audit.drain()
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()
    real_session_maker = audit.background_session_maker

    @asynccontextmanager
    async def gated_session_maker():
        writer_entered.set()
        await release_writer.wait()
        async with real_session_maker() as session:
            yield session

    monkeypatch.setattr(audit, "background_session_maker", gated_session_maker)

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        doc = ({"data": {"id": 5432, "status": "queued"}} if request.method == "POST" else
               {"data": {"id": 5432, "status": "finished", "email": "person@sample.example",
                         "email_status": "risky", "credits": {"api_credits": {"total": 0}}}})
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    call_ref = None
    try:
        submitted = await asyncio.wait_for(clients.post("/call/wiza.people.email.find", json={
            "individual_reveal": {"full_name": "Person Example", "domain": "sample.example"},
            "enrichment_level": "partial",
            "email_options": {"accept_work": True, "accept_personal": False,
                              "accept_generic": False},
        }), timeout=5)
        assert submitted.status_code == 200
        call_ref = submitted.headers["x-treg-call-id"]
        await asyncio.wait_for(writer_entered.wait(), timeout=5)
        polled = await asyncio.wait_for(clients.get(
            "/call/wiza.people.reveal.get", params={"id": 5432}), timeout=5)
        assert polled.status_code == 200
        async with session_maker() as db:
            assert (await db.get(AsyncTaskRecord, call_ref)).hit is True
    finally:
        release_writer.set()
        await audit.drain()
    async with session_maker() as db:
        original = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == call_ref,
            CallRecord.endpoint_id == "wiza.people.email.find"))).scalar_one()
        assert original.hit is True
        assert original.verdict == "unverified"


@pytest.mark.parametrize("capability,terminal,expected_hit", [
    ("email", {"email": "person@sample.example", "email_status": "risky"}, True),
    ("phone", {"phone_status": "unfound"}, False),
])
async def test_wiza_routed_child_records_terminal_verdict(
    clients, monkeypatch, wiza_platform_on, capability, terminal, expected_hit,
):
    endpoint = f"wiza.people.{capability}.find"
    polls = 0

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        nonlocal polls
        if request.method == "POST":
            doc = {"data": {"id": 7654, "status": "queued"}}
        else:
            polls += 1
            doc = ({"data": {"id": 7654, "status": "resolving"}} if polls == 1 else
                   {"data": {"id": 7654, "status": "finished", **terminal,
                             "credits": {"api_credits": {"total": 0}}}})
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    result = await clients.post(
        f"/call/treg.people.{capability}.find",
        headers={"X-Treg-Route-Prefer": "wiza", "X-Treg-Route-Waterfall": "0"},
        json={"full_name": "Person Example", "domain": "sample.example"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["_treg"]["served_by"] == endpoint
    assert result.json()["_treg"]["outcome"] == ("hit" if expected_hit else "miss")
    assert polls == 2
    await audit.drain()
    async with session_maker() as db:
        child = (await db.execute(select(CallRecord).where(
            CallRecord.endpoint_id == endpoint))).scalar_one()
        assert child.hit is expected_hit
        task = await db.get(AsyncTaskRecord, child.call_ref)
        assert task.hit is expected_hit and task.settled_micro == 0


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
        assert task.hit is False
        child = (await db.execute(select(CallRecord).where(
            CallRecord.call_ref == task.call_id,
            CallRecord.endpoint_id == "wiza.people.email.find"))).scalar_one()
        assert child.hit is False
        assert await db.get(Hold, task.call_id) is None
