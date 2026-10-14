"""Root admission, transport lifetime and explicit outage policy; no live services."""
import asyncio
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from treg import audit
from treg.api import app
from treg.application.call import service
from treg.application.call.types import ResolutionFailed, UpstreamResponse
from treg.config import Settings, get_settings
from treg.infra import kv, money_admission, org_concurrency as guard
from treg.infra.db import session_maker
from treg.models import CallRecord
from treg.routers import call as routes
from test_marketplace_call import platform_on  # noqa: F401
from test_routing import enrichment_on  # noqa: F401


class Slots:
    def __init__(self):
        self.owners = {}
        self.acquires = 0
        self.renewed = asyncio.Event()
        self.acquire_result = None
        self.renew_result = None
        self.release_result = None

    async def acquire_slot(self, key, token, ttl_ms, limit):
        self.acquires += 1
        if self.acquire_result:
            return self.acquire_result
        owners = self.owners.setdefault(key, {})
        if len(owners) >= limit:
            return "busy"
        owners[token] = True
        return "acquired"

    async def renew_slot(self, key, token, ttl_ms):
        self.renewed.set()
        return self.renew_result or ("renewed" if token in self.owners.get(key, {}) else "lost")

    async def release_slot(self, key, token):
        if self.release_result:
            return self.release_result
        self.owners.get(key, {}).pop(token, None)
        return "released"

    @property
    def count(self):
        return sum(map(len, self.owners.values()))


@pytest.fixture
def gate(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "org_concurrency_enabled", True)
    monkeypatch.setattr(settings, "org_concurrency_limits", {1: 1})
    monkeypatch.setattr(settings, "org_concurrency_exempt_org_ids", [])
    monkeypatch.setattr(settings, "org_concurrency_redis_failure", "fail_closed")
    monkeypatch.setattr(settings, "org_concurrency_lease_s", 0.12)
    store = Slots()
    monkeypatch.setattr(kv, "store", lambda: store)
    return settings, store


def test_default_disabled_and_explicit_enable_validation():
    assert not Settings(_env_file=None).org_concurrency_enabled
    with pytest.raises(ValidationError, match="explicit org_concurrency_redis_failure"):
        Settings(_env_file=None, org_concurrency_enabled=True, kv_url="redis://localhost")
    with pytest.raises(ValidationError, match="requires shared Redis"):
        Settings(_env_file=None, org_concurrency_enabled=True,
                 org_concurrency_redis_failure="fail_closed", kv_url="")
    with pytest.raises(ValidationError, match="valid Redis kv_url"):
        Settings(_env_file=None, org_concurrency_enabled=True,
                 org_concurrency_redis_failure="fail_closed", kv_url="http://localhost")


@pytest.mark.parametrize("field,value", [
    ("limits", {"0": 1}), ("limits", {"-1": 1}), ("limits", {"org": 1}),
    ("limits", {"01": 1}), ("limits", {True: 1}), ("limits", {"1": 0}),
    ("limits", {"1": -1}), ("limits", {"1": True}), ("limits", {"1": 1.5}),
    ("limits", {"1": "2"}), ("limits", []), ("exempt_org_ids", [True]),
    ("exempt_org_ids", [0]), ("exempt_org_ids", ["1"]), ("lease_s", 2),
    ("lease_s", 301), ("redis_failure", "automatic"), ("retry_after_s", 0),
])
def test_invalid_configuration(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{f"org_concurrency_{field}": value})


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("TREG_ORG_CONCURRENCY_ENABLED", "true")
    monkeypatch.setenv("TREG_ORG_CONCURRENCY_LIMITS", '{"123":2,"456":4}')
    monkeypatch.setenv("TREG_ORG_CONCURRENCY_EXEMPT_ORG_IDS", '[456]')
    monkeypatch.setenv("TREG_ORG_CONCURRENCY_REDIS_FAILURE", "fail_open")
    monkeypatch.setenv("TREG_KV_URL", "redis://localhost/15")
    settings = Settings(_env_file=None)
    assert settings.org_concurrency_limits == {123: 2, 456: 4}
    assert settings.org_concurrency_exempt_org_ids == [456]


@pytest.mark.parametrize("bypass", ["disabled", "unconfigured", "exempt"])
async def test_bypass_never_contacts_redis(gate, monkeypatch, bypass):
    settings, _ = gate
    if bypass == "disabled":
        settings.org_concurrency_enabled = False
    elif bypass == "unconfigured":
        settings.org_concurrency_limits = {}
    else:
        settings.org_concurrency_exempt_org_ids = [1]
    monkeypatch.setattr(kv, "store", lambda: pytest.fail("bypassed request touched Redis"))
    assert await guard.acquire(1) is None


async def test_limit_and_org_isolation(gate):
    settings, store = gate
    settings.org_concurrency_limits = {1: 2, 2: 1}
    slots = [await guard.acquire(1), await guard.acquire(1), await guard.acquire(2)]
    try:
        assert store.count == 3
        assert len({slot.token for slot in slots}) == 3
        with pytest.raises(guard.AdmissionRejected) as caught:
            await guard.acquire(1)
        assert caught.value.status_code == 429
    finally:
        await asyncio.gather(*(slot.finish() for slot in slots))
    assert store.count == 0


@pytest.mark.parametrize("policy", ["fail_open", "fail_closed"])
async def test_redis_outage_policy_is_not_over_limit(gate, policy, caplog):
    settings, store = gate
    settings.org_concurrency_redis_failure = policy
    store.acquire_result = "unavailable"
    if policy == "fail_open":
        assert await guard.acquire(1) is None
    else:
        with pytest.raises(guard.AdmissionRejected) as caught:
            await guard.acquire(1)
        assert caught.value.status_code == 503
        assert caught.value.detail["error"] == "org_concurrency_unavailable"
    assert policy in caplog.text


@pytest.mark.parametrize("policy", ["fail_open", "fail_closed"])
async def test_redis_client_creation_failure_honors_policy(gate, monkeypatch, policy, caplog):
    settings, _ = gate
    settings.org_concurrency_redis_failure = policy

    def broken_client():
        raise ValueError("invalid Redis URL option: must-not-log-url")

    monkeypatch.setattr(kv, "store", broken_client)
    if policy == "fail_open":
        assert await guard.acquire(1) is None
    else:
        with pytest.raises(guard.AdmissionRejected) as caught:
            await guard.acquire(1)
        assert caught.value.status_code == 503
    assert policy in caplog.text
    assert "must-not-log-url" not in caplog.text


@pytest.mark.parametrize("outcome", ["lost", "unavailable"])
async def test_renewal_loss_keeps_accepted_cleanup_running(gate, caplog, outcome):
    _, store = gate
    slot = await guard.acquire(1)
    store.renew_result = outcome
    await asyncio.wait_for(store.renewed.wait(), 1)
    await asyncio.sleep(0)
    assert "cap is not guaranteed" in caplog.text
    cleaned = AsyncMock()
    await slot.finish(cleaned)
    cleaned.assert_awaited_once()
    assert store.count == 0


async def test_cancellation_during_ambiguous_acquire_releases_its_owner(gate, monkeypatch):
    _, store = gate
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = store.acquire_slot

    async def delayed(*args):
        result = await original(*args)
        entered.set()
        await proceed.wait()
        return result

    monkeypatch.setattr(store, "acquire_slot", delayed)
    task = asyncio.create_task(guard.acquire(1))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # cleanup must survive a second cancellation too
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.count == 0


@pytest.mark.parametrize("reply", ["unavailable", "late"])
async def test_unconfirmed_acquisition_does_not_enter_work_and_cleans_token(gate, monkeypatch, reply):
    _, store = gate
    original = store.acquire_slot

    async def ambiguous(*args):
        await original(*args)
        if reply == "late":
            await asyncio.sleep(0.15)
            return "acquired"
        return "unavailable"

    monkeypatch.setattr(store, "acquire_slot", ambiguous)
    with pytest.raises(guard.AdmissionRejected) as caught:
        await guard.acquire(1)
    assert caught.value.status_code == 503
    assert store.count == 0


async def test_release_failure_is_reported_without_changing_accepted_result(gate, caplog):
    _, store = gate
    slot = await guard.acquire(1)
    store.release_result = "unavailable"
    await slot.finish()
    assert "release_unavailable" in caplog.text
    assert slot.renewal.done()


async def test_close_failure_cannot_mask_original_error_or_leak_slot(gate, caplog):
    _, store = gate
    slot = await guard.acquire(1)
    with pytest.raises(ValueError, match="original"):
        try:
            raise ValueError("original")
        finally:
            await slot.finish(AsyncMock(side_effect=RuntimeError("close failed")))
    assert store.count == 0
    assert "upstream_close_failed" in caplog.text


async def test_long_cleanup_renews_and_survives_anyio_cancellation(gate):
    _, store = gate
    slot = await guard.acquire(1)

    async def close():
        await asyncio.sleep(0.3)
        assert store.count == 1

    with anyio.CancelScope() as scope:
        scope.cancel()
        await slot.finish(close)
    assert store.renewed.is_set()
    assert store.count == 0


async def _configure_org(clients, gate):
    settings, store = gate
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    settings.org_concurrency_limits = {org_id: 1}
    return org_id, store


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
async def test_rejection_before_money_provider_or_body_with_existing_audit(
    clients, gate, monkeypatch, path,
):
    org, store = await _configure_org(clients, gate)
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    held = await guard.acquire(org)
    execute = AsyncMock(side_effect=AssertionError("rejected call executed"))
    reserve = AsyncMock(side_effect=AssertionError("rejected call reserved"))
    monkeypatch.setattr(routes, "execute_call", execute)
    monkeypatch.setattr(money_admission, "admit", lambda *a, **k: pytest.fail("money admission"))
    monkeypatch.setattr(service.ledger, "reserve", reserve)
    monkeypatch.setattr(routes._HttpRequestBody, "read", AsyncMock(side_effect=AssertionError("body read")))
    try:
        response = await clients.post(path + "example.operation", content=b"ignored")
        assert response.status_code == 429, response.text
        assert response.headers["Retry-After"] == "1"
        assert response.headers["X-Treg-Error"] == "1"
        call_ref = response.headers["X-Treg-Call-Id"]
        assert response.json()["detail"]["error"] == "org_concurrency_limit"
        await audit.drain()
        async with session_maker() as db:
            rows = (await db.execute(select(CallRecord).where(CallRecord.status_code == 429))).scalars().all()
        assert len(rows) == 1 and rows[0].org_id == org
        assert rows[0].call_ref == call_ref
        execute.assert_not_awaited()
        reserve.assert_not_awaited()
    finally:
        await held.finish()
    assert store.count == 0


async def test_all_api_keys_share_the_org_slot(clients, gate, monkeypatch):
    org, _ = await _configure_org(clients, gate)
    key = (await clients.post(f"/orgs/{org}/api-keys", json={"name": "second"})).json()["secret"]
    held = await guard.acquire(org)
    try:
        for token in (clients.headers["X-Treg-Token"], key):
            response = await clients.get("/call/example/path", headers={"X-Treg-Token": token})
            assert response.status_code == 429
    finally:
        await held.finish()


@pytest.mark.parametrize("failure", [
    ResolutionFailed("target_not_found", status_code=404, detail="missing"), RuntimeError("test fault"),
])
async def test_execution_exception_releases(clients, gate, monkeypatch, failure):
    _, store = await _configure_org(clients, gate)
    monkeypatch.setattr(routes, "execute_call", AsyncMock(side_effect=failure))
    if isinstance(failure, ResolutionFailed):
        response = await clients.get("/call/example/path")
        assert response.status_code == 404
    else:
        with pytest.raises(RuntimeError, match="test fault"):
            await clients.get("/call/example/path")
    assert store.count == 0


@pytest.mark.parametrize("policy,status", [("fail_closed", 503), ("fail_open", 202)])
async def test_http_redis_policy_and_async_submission_completion(clients, gate, monkeypatch, policy, status):
    _, store = await _configure_org(clients, gate)
    gate[0].org_concurrency_redis_failure = policy
    store.acquire_result = "unavailable"

    async def body():
        yield b'{"job_id":"test-job"}'

    execute = AsyncMock(return_value=UpstreamResponse(202, (), body(), AsyncMock()))
    monkeypatch.setattr(routes, "execute_call", execute)
    response = await clients.post("/call/example/submit")
    assert response.status_code == status
    assert execute.await_count == (1 if policy == "fail_open" else 0)
    if policy == "fail_closed":
        assert response.headers["Retry-After"] == "1"
        assert response.json()["detail"]["error"] == "org_concurrency_unavailable"
    assert store.count == 0


async def test_accepted_async_submission_releases_after_response(clients, gate, monkeypatch):
    _, store = await _configure_org(clients, gate)

    async def body():
        yield b'{"job_id":"still-running-remotely"}'

    execute = AsyncMock(side_effect=lambda *args: UpstreamResponse(202, (), body(), AsyncMock()))
    monkeypatch.setattr(routes, "execute_call", execute)
    for _ in range(2):
        assert (await clients.post("/call/example/submit")).status_code == 202
        assert store.count == 0


@pytest.mark.parametrize("ending", ["success", "stream_error", "cancel"])
async def test_stream_owns_slot_through_completion(clients, gate, monkeypatch, ending):
    org, store = await _configure_org(clients, gate)
    started, proceed = asyncio.Event(), asyncio.Event()
    closed = AsyncMock()

    async def body():
        started.set()
        yield b"first"
        await proceed.wait()
        if ending == "stream_error":
            raise RuntimeError("stream fault")
        yield b"last"

    monkeypatch.setattr(routes, "execute_call", AsyncMock(return_value=UpstreamResponse(
        200, ((b"content-type", b"text/plain"),), body(), closed)))
    task = asyncio.create_task(clients.get("/call/example/path"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.3)  # Outlive multiple leases before the stream ends.
        assert store.renewed.is_set() and store.count == 1
        response = await clients.get("/call/example/path")
        assert response.status_code == 429
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            proceed.set()
            if ending == "stream_error":
                with pytest.raises(RuntimeError, match="stream fault"):
                    await task
            else:
                assert (await task).content == b"firstlast"
        closed.assert_awaited_once()
        assert store.count == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_send_disconnect_before_first_body_releases(gate):
    from starlette.requests import ClientDisconnect
    from starlette.responses import StreamingResponse

    _, store = gate
    slot = await guard.acquire(1)
    closed = AsyncMock()

    async def body():
        yield b"chunk"

    async def send(message):
        raise OSError("client gone")

    response = routes._LeasedStreamingResponse(StreamingResponse(body()), slot, closed)
    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
    closed.assert_awaited_once()
    assert store.count == 0


async def test_table_finish_failure_closes_once_and_releases(clients, gate, monkeypatch):
    from treg.routers import table

    _, store = await _configure_org(clients, gate)
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    closed = AsyncMock()

    async def body():
        yield b"{}"

    monkeypatch.setattr(routes, "execute_call", AsyncMock(return_value=UpstreamResponse(
        200, (), body(), closed)))
    monkeypatch.setattr(table, "_table_answer", AsyncMock(side_effect=RuntimeError("table fault")))
    with pytest.raises(RuntimeError, match="table fault"):
        await clients.get("/table/example/path")
    closed.assert_awaited_once()
    assert store.count == 0


async def test_routing_children_take_no_extra_root_slots(clients, enrichment_on, gate, monkeypatch):
    from test_routing import _relay_by_provider, ROUTED

    _, store = await _configure_org(clients, gate)
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "person@example.com", "score": 99,
                                  "verification": {"status": "valid"}}})]}, seen))
    response = await clients.post(f"/call/{ROUTED}", json={"full_name": "Test Person", "domain": "example.com"})
    assert response.status_code == 200, response.text
    assert seen and store.acquires == 1
    assert store.count == 0


async def test_funded_cancellation_keeps_cleanup_inside_slot(clients, platform_on, gate, monkeypatch):
    import httpx
    from treg.infra import org_traffic
    from test_call_cancellation import (
        _BlockingProviderStream, _BlockingProviderTransport, _funded_org,
        _open_holds, _release_entries, _wait_for_gate,
    )

    counter = org_traffic.Counters()
    monkeypatch.setattr(org_traffic, "counters", counter)
    org, balance = await _funded_org(clients)
    _, store = await _configure_org(clients, gate)
    stream = _BlockingProviderStream()
    entered, proceed = asyncio.Event(), asyncio.Event()
    original_cleanup = service._finish_cancelled_call

    async def cleanup(*args, **kwargs):
        entered.set()
        await proceed.wait()
        assert store.count == 1
        await original_cleanup(*args, **kwargs)

    monkeypatch.setattr(service, "_finish_cancelled_call", cleanup)
    async with httpx.AsyncClient(transport=_BlockingProviderTransport(stream)) as http:
        monkeypatch.setattr(app.state, "http", http)
        task = asyncio.create_task(clients.get("/call/tikhub.tiktok.video.comments?aweme_id=test"))
        try:
            await _wait_for_gate(stream.body_started, task, "provider body")
            [hold] = await _open_holds(org)
            task.cancel()
            await asyncio.wait_for(entered.wait(), 2)
            assert counter.snapshot()["top"][0]["in_flight_max"] == 1
            assert counter.orgs[org].live == 1
            await asyncio.sleep(0.3)
            assert store.count == 1 and store.renewed.is_set()
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await _open_holds(org) == []
            assert len(await _release_entries(org, hold.id)) == 1
            assert (await clients.get(f"/orgs/{org}/balance")).json()["balance_micro"] == balance
            assert store.count == 0
            [traffic_row] = counter.snapshot()["top"]
            assert traffic_row["requests"] == 0 and traffic_row["completed"] == 1
            assert traffic_row["in_flight_max"] == 1 and not counter.orgs
        finally:
            proceed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("path,tool,args", [
    ("/mcp/", "call", {"endpoint_id": "tikhub.tiktok.video.comments"}),
    ("/mcp/v2/", "catalog_call_read", {"endpoint_id": "tikhub.tiktok.video.comments"}),
    ("/mcp/v2/", "catalog_call_write", {"endpoint_id": "dataforseo.web.page.audit"}),
    ("/mcp/", "call_media", {"endpoint_id": "fishaudio.tts.s2-1-pro", "body": {"text": "test"}}),
])
async def test_mcp_surfaces_preserve_retry_information(clients, gate, path, tool, args):
    from test_mcp_directory import paired_mcp_session, _call_tool

    org, _ = await _configure_org(clients, gate)
    held = await guard.acquire(org)
    try:
        async with paired_mcp_session() as client:
            out = await _call_tool(client, tool, args, clients.headers["X-Treg-Token"], path=path)
        assert out["status"] == 429, out
        assert out["retry_after"] == "1"
        assert out["body"]["detail"]["retry_after_s"] == 1
    finally:
        await held.finish()
