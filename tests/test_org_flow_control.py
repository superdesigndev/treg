"""Published Org admission precedes new root work, never accepted-request cleanup."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from treg import audit
from treg.api import app
from treg.application.call import service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.infra import kv, money_admission, org_concurrency, org_flow_control
from treg.infra.db import session_maker
from treg.models import CallRecord, LedgerEntry
from treg.routers import call as routes
from test_marketplace_call import platform_on  # noqa: F401


def _pause():
    return org_flow_control.PauseRejected(retry_after_s=30, version=7)


def _success():
    async def body():
        yield b'{"ok":true}'
    return UpstreamResponse(200, ((b"content-type", b"application/json"),), body(), AsyncMock())


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
async def test_root_refusal_precedes_body_concurrency_money_and_provider(
    clients, monkeypatch, path,
):
    status, error, retry = 429, "org_traffic_paused", 30
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    check = AsyncMock(side_effect=_pause())
    acquire = AsyncMock(side_effect=AssertionError("refusal acquired a root slot"))
    execute = AsyncMock(side_effect=AssertionError("refusal executed a call"))
    reserve = AsyncMock(side_effect=AssertionError("refusal reserved money"))
    monkeypatch.setattr(org_flow_control, "check", check)
    monkeypatch.setattr(org_concurrency, "acquire", acquire)
    monkeypatch.setattr(routes, "execute_call", execute)
    monkeypatch.setattr(service.ledger, "reserve", reserve)
    monkeypatch.setattr(money_admission, "admit", lambda *a, **k: pytest.fail("money admission"))
    monkeypatch.setattr(routes._HttpRequestBody, "read", AsyncMock(
        side_effect=AssertionError("refusal read the body")))

    response = await clients.post(path + "example.operation", content=b"ignored", headers={
        "X-Treg-Call-Id": "caller-invented-id",
        "X-Treg-Org-Id": "999999",
    })
    assert response.status_code == status, response.text
    assert response.headers["Retry-After"] == str(retry)
    assert response.headers["X-Treg-Error"] == "1"
    call_ref = response.headers["X-Treg-Call-Id"]
    assert call_ref and call_ref != "caller-invented-id"
    detail = response.json()["detail"]
    assert detail["error"] == error
    assert detail["retry_after_s"] == retry
    assert detail["config_version"] == 7
    check.assert_awaited_once_with(org_id, call_ref)
    acquire.assert_not_awaited()
    execute.assert_not_awaited()
    reserve.assert_not_awaited()

    await audit.drain()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(
            CallRecord.status_code == status))).scalars().all()
    assert len(rows) == 1
    assert rows[0].org_id == org_id
    assert rows[0].call_ref == call_ref


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
@pytest.mark.parametrize("result", [None, kv.FlowControlReadFailure.UNAVAILABLE,
                                  kv.FlowControlReadFailure.INVALID, "invalid json"])
async def test_reader_failures_allow_root_calls(clients, monkeypatch, path, result):
    monkeypatch.setattr(get_settings(), "org_flow_control_enabled", True)
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    monkeypatch.setattr(org_flow_control, "_reader", org_flow_control.Reader())
    monkeypatch.setattr(kv.LocalStore, "read_org_flow_control", AsyncMock(return_value=result))
    execute = AsyncMock(return_value=_success())
    monkeypatch.setattr(routes, "execute_call", execute)
    response = await clients.get(path + "example/path")
    assert response.status_code == 200, response.text
    execute.assert_awaited_once()


async def test_cancellation_during_reader_does_not_become_http_failure(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "org_flow_control_enabled", True)
    reader = org_flow_control.Reader()
    monkeypatch.setattr(org_flow_control, "_reader", reader)
    started = asyncio.Event()

    async def read(_org):
        started.set()
        await asyncio.Event().wait()

    read_mock = AsyncMock(side_effect=read)
    monkeypatch.setattr(kv.LocalStore, "read_org_flow_control", read_mock)
    execute = AsyncMock(return_value=_success())
    monkeypatch.setattr(routes, "execute_call", execute)
    task = asyncio.create_task(clients.get("/call/example/path"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    execute.assert_not_awaited()
    read_mock.side_effect = None
    read_mock.return_value = None
    assert (await clients.get("/call/example/path")).status_code == 200
    assert read_mock.await_count == 2


async def test_untrusted_caller_never_enters_org_pause(clients, monkeypatch):
    check = AsyncMock(side_effect=AssertionError("unauthenticated caller reached Org policy"))
    monkeypatch.setattr(org_flow_control, "check", check)
    response = await clients.get("/call/example/path", headers={"X-Treg-Token": "invalid"})
    assert response.status_code == 401
    check.assert_not_awaited()


async def test_pause_applies_to_every_api_key_for_the_org(clients, monkeypatch):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    key = (await clients.post(
        f"/orgs/{org_id}/api-keys", json={"name": "second"})).json()["secret"]
    check = AsyncMock(side_effect=_pause())
    monkeypatch.setattr(org_flow_control, "check", check)
    for token in (clients.headers["X-Treg-Token"], key):
        response = await clients.get("/call/example/path", headers={"X-Treg-Token": token})
        assert response.status_code == 429, response.text
    assert check.await_count == 2
    assert all(call.args[0] == org_id for call in check.await_args_list)


@pytest.mark.parametrize("path,tool,args", [
    ("/mcp/", "call", {"endpoint_id": "tikhub.tiktok.video.comments"}),
    ("/mcp/v2/", "catalog_call_read", {"endpoint_id": "tikhub.tiktok.video.comments"}),
    ("/mcp/v2/", "catalog_call_write", {"endpoint_id": "dataforseo.web.page.audit"}),
    ("/mcp/", "call_media", {
        "endpoint_id": "fishaudio.tts.s2-1-pro", "body": {"text": "test"},
    }),
])
@pytest.mark.parametrize("refusal", ["pause", "rate"])
async def test_mcp_roots_preserve_admission_retry_and_call_identity(
    clients, monkeypatch, path, tool, args, refusal,
):
    from test_mcp_directory import _call_tool, paired_mcp_session

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    snapshot = org_flow_control.Snapshot(
        version=7, pause_until=None, canonical="{}",
        rate_limit=org_flow_control.RateLimit(rps=0.5, burst=2))
    check = AsyncMock(side_effect=_pause()) if refusal == "pause" else AsyncMock(return_value=snapshot)
    acquire = AsyncMock(side_effect=org_concurrency.AdmissionRejected(
        retry_after_s=3, version=7, rate_limited=True))
    monkeypatch.setattr(org_flow_control, "check", check)
    monkeypatch.setattr(org_concurrency, "acquire", acquire)
    async with paired_mcp_session() as client:
        out = await _call_tool(
            client, tool, args, clients.headers["X-Treg-Token"], path=path)
    assert out["status"] == 429, out
    retry = 30 if refusal == "pause" else 3
    assert out["retry_after"] == str(retry)
    assert out["body"]["detail"]["error"] == (
        "org_traffic_paused" if refusal == "pause" else "org_rate_limit")
    assert out["body"]["detail"]["retry_after_s"] == retry
    assert out["body"]["detail"]["config_version"] == 7
    check.assert_awaited_once_with(org_id, out["call_id"])
    if refusal == "pause":
        acquire.assert_not_awaited()
    else:
        acquire.assert_awaited_once_with(
            org_id, limit=None, version=7, rate_limit=snapshot.rate_limit)


async def test_new_pause_does_not_interrupt_an_accepted_stream(clients, monkeypatch):
    check = AsyncMock(return_value=None)
    monkeypatch.setattr(org_flow_control, "check", check)
    started, proceed = asyncio.Event(), asyncio.Event()
    closed = AsyncMock()

    async def body():
        started.set()
        yield b"first"
        await proceed.wait()
        yield b"last"

    execute = AsyncMock(return_value=UpstreamResponse(200, (), body(), closed))
    monkeypatch.setattr(routes, "execute_call", execute)
    task = asyncio.create_task(clients.get("/call/example/path"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        check.side_effect = _pause()
        refused = await clients.get("/call/example/path")
        assert refused.status_code == 429
        proceed.set()
        assert (await task).content == b"firstlast"
        execute.assert_awaited_once()
        closed.assert_awaited_once()
        assert check.await_count == 2
    finally:
        proceed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("ending", ["settlement", "cancellation"])
async def test_pause_does_not_block_accepted_financial_cleanup(
    clients, platform_on, monkeypatch, ending,
):
    from test_call_cancellation import (
        _BlockingProviderStream, _BlockingProviderTransport, _funded_org,
        _open_holds, _release_entries, _wait_for_gate,
    )

    org_id, balance = await _funded_org(clients)
    check = AsyncMock(return_value=None)
    monkeypatch.setattr(org_flow_control, "check", check)
    stream = _BlockingProviderStream()
    async with httpx.AsyncClient(transport=_BlockingProviderTransport(stream)) as http:
        monkeypatch.setattr(app.state, "http", http)
        task = asyncio.create_task(clients.get(
            "/call/tikhub.tiktok.video.comments?aweme_id=test"))
        try:
            await _wait_for_gate(stream.body_started, task, "provider body")
            [hold] = await _open_holds(org_id)
            check.side_effect = _pause()
            assert (await clients.get("/call/tikhub.tiktok.video.comments")).status_code == 429
            assert len(await _open_holds(org_id)) == 1
            if ending == "cancellation":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert len(await _release_entries(org_id, hold.id)) == 1
                assert (await clients.get(
                    f"/orgs/{org_id}/balance")).json()["balance_micro"] == balance
            else:
                stream.allow_body.set()
                response = await task
                assert response.status_code == 200, response.text
                async with session_maker() as db:
                    settled = (await db.execute(select(LedgerEntry).where(
                        LedgerEntry.org_id == org_id, LedgerEntry.call_id == hold.id,
                        LedgerEntry.kind == "settle"))).scalars().all()
                assert len(settled) == 1
                remaining = (await clients.get(
                    f"/orgs/{org_id}/balance")).json()["balance_micro"]
                assert balance - remaining == int(response.headers["X-Treg-Cost-Micro"])
            assert await _open_holds(org_id) == []
            assert stream.close_calls == 1
            assert check.await_count == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
@pytest.mark.parametrize("snapshot", [
    None,
    org_flow_control.Snapshot(version=3, pause_until=None, canonical="{}"),
    org_flow_control.Snapshot(version=4, pause_until=None, canonical="{}", max_in_flight=7),
    org_flow_control.Snapshot(
        version=5, pause_until=None, canonical="{}",
        rate_limit=org_flow_control.RateLimit(rps=0.5, burst=2)),
    org_flow_control.Snapshot(
        version=6, pause_until=None, canonical="{}", max_in_flight=3,
        rate_limit=org_flow_control.RateLimit(rps=2, burst=5)),
])
async def test_root_forwards_single_published_snapshot_to_admission(
    clients, monkeypatch, path, snapshot,
):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    check = AsyncMock(return_value=snapshot)
    acquire = AsyncMock(return_value=None)
    monkeypatch.setattr(org_flow_control, "check", check)
    monkeypatch.setattr(org_concurrency, "acquire", acquire)
    execute = AsyncMock(return_value=_success())
    monkeypatch.setattr(routes, "execute_call", execute)
    response = await clients.get(path + "example/path")
    assert response.status_code == 200, response.text
    execute.assert_awaited_once()
    context = execute.await_args.args[0]
    assert context.call_ref
    check.assert_awaited_once_with(org_id, context.call_ref)
    acquire.assert_awaited_once_with(
        org_id, limit=snapshot.max_in_flight if snapshot else None,
        version=snapshot.version if snapshot else 0,
        rate_limit=snapshot.rate_limit if snapshot else None)


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
@pytest.mark.parametrize("limit", [None, 3])
async def test_rate_refusal_preserves_http_headers_audit_and_precedes_body_and_money(
    clients, monkeypatch, path, limit,
):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    snapshot = org_flow_control.Snapshot(
        version=7, pause_until=None, canonical="{}", max_in_flight=limit,
        rate_limit=org_flow_control.RateLimit(rps=0.5, burst=2))
    check = AsyncMock(return_value=snapshot)
    acquire = AsyncMock(side_effect=org_concurrency.AdmissionRejected(
        retry_after_s=3, version=7, rate_limited=True))
    execute = AsyncMock(side_effect=AssertionError("rate refusal executed a call"))
    reserve = AsyncMock(side_effect=AssertionError("rate refusal reserved money"))
    monkeypatch.setattr(org_flow_control, "check", check)
    monkeypatch.setattr(org_concurrency, "acquire", acquire)
    monkeypatch.setattr(routes, "execute_call", execute)
    monkeypatch.setattr(service.ledger, "reserve", reserve)
    monkeypatch.setattr(money_admission, "admit", lambda *a, **k: pytest.fail("money admission"))
    monkeypatch.setattr(routes._HttpRequestBody, "read", AsyncMock(
        side_effect=AssertionError("rate refusal read the body")))
    response = await clients.post(path + "example.operation", content=b"ignored", headers={
        "X-Treg-Call-Id": "caller-invented-id", "X-Treg-Org-Id": "999999",
    })
    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == "3"
    assert response.headers["X-Treg-Error"] == "1"
    call_ref = response.headers["X-Treg-Call-Id"]
    assert call_ref and call_ref != "caller-invented-id"
    detail = response.json()["detail"]
    assert detail["error"] == "org_rate_limit"
    assert detail["retry_after_s"] == 3
    assert detail["config_version"] == 7
    check.assert_awaited_once_with(org_id, call_ref)
    acquire.assert_awaited_once_with(
        org_id, limit=limit, version=7, rate_limit=snapshot.rate_limit)
    execute.assert_not_awaited()
    reserve.assert_not_awaited()
    await audit.drain()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(
            CallRecord.status_code == 429))).scalars().all()
    assert len(rows) == 1
    assert rows[0].org_id == org_id
    assert rows[0].call_ref == call_ref
