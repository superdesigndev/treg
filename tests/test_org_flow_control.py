"""Published Org admission precedes new root work, never accepted-request cleanup."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from treg.api import app
from treg.config import get_settings
from treg.infra import kv, org_flow_control
from treg.infra.db import session_maker
from treg.models import LedgerEntry
from test_marketplace_call import EP, _balance, platform_on  # noqa: F401
from test_org_concurrency import _gated, _upstream, gate, refused_before_any_work  # noqa: F401

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
PAUSED_30_S = "2030-01-01T00:00:30Z"


async def _publish(clients, gate, monkeypatch, *, limit=None, rate=None, pause_until=None, version=7):
    _, store = gate
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    monkeypatch.setattr(org_flow_control, "_utcnow", lambda: NOW)
    store.publish(org_id, limit, version, rate=rate, pause_until=pause_until)
    return org_id, store


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
async def test_a_paused_org_is_refused_before_its_body_money_admission_or_provider(
    clients, platform_on, gate, monkeypatch, path,
):
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    org_id, store = await _publish(clients, gate, monkeypatch, rate={"rps": 1, "burst": 2},
                                   limit=1, pause_until=PAUSED_30_S)
    seen = _upstream(monkeypatch)
    response, row = await refused_before_any_work(clients, path, seen=seen, before=await _balance(clients))
    assert response.headers["Retry-After"] == "30"
    assert response.json()["detail"] == response.json()["detail"] | {
        "error": "org_traffic_paused", "retry_after_s": 30, "config_version": 7}
    assert row.org_id == org_id
    assert store.acquires == store.charges == 0 and not store.admissions


@pytest.mark.parametrize("path,limit", [
    ("/call/", None), ("/call/", 3), ("/catalog/call/", 3), ("/table/", 3),
])
async def test_a_rate_limited_org_is_refused_before_its_body_money_or_provider(
    clients, platform_on, gate, monkeypatch, path, limit,
):
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    org_id, store = await _publish(clients, gate, monkeypatch, limit=limit, rate={"rps": 0.5, "burst": 2})
    store.admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=3)
    seen = _upstream(monkeypatch)
    response, row = await refused_before_any_work(clients, path, seen=seen, before=await _balance(clients))
    assert response.headers["Retry-After"] == "3"
    assert response.json()["detail"] == response.json()["detail"] | {
        "error": "org_rate_limit", "retry_after_s": 3, "config_version": 7}
    assert row.org_id == org_id
    assert [(a[0], a[3], a[4], a[5]) for a in store.admissions] == [(org_id, limit, 0.5, 2)]


@pytest.mark.parametrize("result", [None, kv.FlowControlReadFailure.UNAVAILABLE,
                                    kv.FlowControlReadFailure.INVALID, "invalid json"])
async def test_reader_failures_allow_root_calls(clients, platform_on, gate, monkeypatch, result):
    org_id, store = await _publish(clients, gate, monkeypatch)
    store.publications[org_id] = result
    seen = _upstream(monkeypatch)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200, response.text
    assert len(seen) == 1


async def test_cancellation_during_reader_does_not_become_http_failure(
    clients, platform_on, gate, monkeypatch,
):
    _, store = gate
    started = asyncio.Event()

    async def hang(_org):
        store.reads += 1
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "read_org_flow_control", hang)
    seen = _upstream(monkeypatch)
    task = asyncio.create_task(clients.get(f"/call/{EP}?aweme_id=7"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == []
    monkeypatch.setattr(store, "read_org_flow_control", AsyncMock(return_value=None))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    assert len(seen) == 1


async def test_pause_applies_to_every_api_key_for_the_org(clients, platform_on, gate, monkeypatch):
    org_id, _ = await _publish(clients, gate, monkeypatch, pause_until=PAUSED_30_S)
    key = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "second"})).json()["secret"]
    seen = _upstream(monkeypatch)
    for token in (clients.headers["X-Treg-Token"], key):
        response = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Token": token})
        assert response.status_code == 429, response.text
    assert seen == []


@pytest.mark.parametrize("path,tool,args,refusal", [
    ("/mcp/", "call", {"endpoint_id": "tikhub.tiktok.video.comments"}, "rate"),
    ("/mcp/", "call", {"endpoint_id": "tikhub.tiktok.video.comments"}, "pause"),
    ("/mcp/v2/", "catalog_call_read", {"endpoint_id": "tikhub.tiktok.video.comments"}, "rate"),
    ("/mcp/v2/", "catalog_call_write", {"endpoint_id": "dataforseo.web.page.audit"}, "rate"),
    ("/mcp/", "call_media", {
        "endpoint_id": "fishaudio.tts.s2-1-pro", "body": {"text": "test"},
    }, "rate"),
])
async def test_mcp_roots_preserve_admission_retry_and_call_identity(
    clients, gate, monkeypatch, path, tool, args, refusal,
):
    from test_mcp_directory import _call_tool, paired_mcp_session

    org_id, store = await _publish(
        clients, gate, monkeypatch, rate={"rps": 0.5, "burst": 2},
        pause_until=PAUSED_30_S if refusal == "pause" else None)
    store.admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=3)
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
    assert out["call_id"]
    assert [a[0] for a in store.admissions] == ([] if refusal == "pause" else [org_id])


async def test_new_pause_does_not_interrupt_an_accepted_call(clients, platform_on, gate, monkeypatch):
    org_id, store = await _publish(clients, gate, monkeypatch)
    started, proceed = asyncio.Event(), asyncio.Event()
    closed = AsyncMock()

    _upstream(monkeypatch, _gated(started, proceed, closed))
    task = asyncio.create_task(clients.get(f"/call/{EP}?aweme_id=7"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        store.publish(org_id, None, 8, pause_until=PAUSED_30_S)
        refused = await asyncio.wait_for(clients.get(f"/call/{EP}?aweme_id=8"), 5)
        assert refused.status_code == 429
        assert refused.json()["detail"]["config_version"] == 8
        proceed.set()
        accepted = await task
        assert accepted.status_code == 200 and accepted.content == b'{"part":"last"}'
        closed.assert_awaited_once()
    finally:
        proceed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("ending", ["settlement", "cancellation"])
async def test_pause_does_not_block_accepted_financial_cleanup(
    clients, platform_on, gate, monkeypatch, ending,
):
    from test_call_cancellation import (
        _BlockingProviderStream, _BlockingProviderTransport, _funded_org,
        _open_holds, _release_entries, _wait_for_gate,
    )

    org_id, balance = await _funded_org(clients)
    _, store = gate
    monkeypatch.setattr(org_flow_control, "_utcnow", lambda: NOW)
    stream = _BlockingProviderStream()
    async with httpx.AsyncClient(transport=_BlockingProviderTransport(stream)) as http:
        monkeypatch.setattr(app.state, "http", http)
        task = asyncio.create_task(clients.get(
            "/call/tikhub.tiktok.video.comments?aweme_id=test"))
        try:
            await _wait_for_gate(stream.body_started, task, "provider body")
            [hold] = await _open_holds(org_id)
            store.publish(org_id, None, 7, pause_until=PAUSED_30_S)
            refused = await asyncio.wait_for(clients.get("/call/tikhub.tiktok.video.comments"), 5)
            assert refused.status_code == 429
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
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("limit,rate", [
    (None, None), (7, None), (None, {"rps": 0.5, "burst": 2}), (3, {"rps": 2, "burst": 5}),
])
async def test_an_admitted_root_asks_the_store_for_exactly_the_published_limits(
    clients, platform_on, gate, monkeypatch, limit, rate,
):
    org_id, store = await _publish(clients, gate, monkeypatch, limit=limit, rate=rate)
    seen = _upstream(monkeypatch)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    if rate is not None:
        assert [(a[0], a[3], a[4], a[5]) for a in store.admissions] == [(org_id, limit, rate["rps"], rate["burst"])]
        assert store.acquires == 0
    else:
        assert not store.admissions and store.acquires == (1 if limit is not None else 0)
    assert store.count == 0
