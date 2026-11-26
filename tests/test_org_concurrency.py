"""Published root admission, transport lifetime and fail-open behavior; no live services."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from treg import audit
from treg.api import app
from treg.application.call import service
from treg.application.call.types import UpstreamResponse
from treg.config import Settings, get_settings
from treg.infra import kv, org_concurrency as guard, org_flow_control as flow
from treg.infra.db import session_maker
from treg.models import CallRecord
from treg.routers import call as routes
from test_marketplace_call import EP, EP_DFS, _balance, platform_on  # noqa: F401
from test_org_endpoint_quota_call import _assert_no_money
from test_routing import enrichment_on  # noqa: F401


class Slots:
    def __init__(self):
        self.owners = {}
        self.acquires = 0
        self.admissions = []
        self.admit_result = None
        self.charges = 0
        self.releases = 0
        self.renewed = asyncio.Event()
        self.acquire_result = None
        self.renew_result = None
        self.release_result = None
        self.publications = {}
        self.clock = 100.0
        self.reads = 0

    def publish(self, org_id, limit, version=1, *, rate=None, pause_until=None):
        self.publications[org_id] = json.dumps({"org_id": org_id, "version": version, "config": {
            "schema_version": 1, "pause_until": pause_until, "rate_limit": rate,
            "concurrency_limit": {"max_in_flight": limit} if limit is not None else None}})
        self.clock += 3

    async def read_org_flow_control(self, org_id):
        self.reads += 1
        return self.publications.get(org_id)

    async def acquire_slot(self, key, token, ttl_ms, limit):
        self.acquires += 1
        if self.acquire_result:
            return self.acquire_result
        owners = self.owners.setdefault(key, {})
        if len(owners) >= limit:
            return "busy"
        owners[token] = True
        return "acquired"

    async def admit_org(self, org_id, token, ttl_ms, limit, *, rps, burst):
        self.admissions.append((org_id, token, ttl_ms, limit, rps, burst))
        if self.admit_result is not None:
            return self.admit_result
        owners = self.owners.setdefault(f"treg:org-concurrency:v1:{org_id}", {})
        if limit is not None and len(owners) >= limit:
            return kv.OrgAdmissionResult("busy")
        self.charges += 1
        if limit is not None:
            owners[token] = True
        return kv.OrgAdmissionResult("acquired")

    async def renew_slot(self, key, token, ttl_ms):
        self.renewed.set()
        return self.renew_result or ("renewed" if token in self.owners.get(key, {}) else "lost")

    async def release_slot(self, key, token):
        self.releases += 1
        if self.release_result:
            return self.release_result
        self.owners.get(key, {}).pop(token, None)
        return "released"

    @property
    def count(self):
        return sum(map(len, self.owners.values()))


def _upstream(monkeypatch, answer=None):
    """The provider's side of every call: `answer()` builds each response, or raises."""
    seen = []

    async def relay(request, upstream_url, tool, secrets, client, *, before_outbound=None, **kwargs):
        if before_outbound is not None:
            await before_outbound()
        seen.append(upstream_url)
        if answer is not None:
            return answer()

        async def body():
            yield b'{"ok":true}'
        return UpstreamResponse(200, ((b"content-type", b"application/json"),), body(), AsyncMock())

    monkeypatch.setattr(service, "relay", relay)
    return seen


def _closes_once(close):
    """The real relay's close is idempotent; callers may call it more than once."""
    closed = False

    async def once():
        nonlocal closed
        if not closed:
            closed = True
            await close()
    return once


def _gated(body_started, proceed, closed, *, fail=False, headers=()):
    """An answer whose body waits for `proceed`, so the call stays in flight until the test says."""
    def answer():
        async def body():
            body_started.put_nowait(True) if isinstance(body_started, asyncio.Queue) else body_started.set()
            yield b'{"part":'
            await proceed.wait()
            if fail:
                raise RuntimeError("stream fault")
            yield b'"last"}'
        return UpstreamResponse(200, ((b"content-type", b"application/json"), *headers), body(),
                                _closes_once(closed))
    return answer


async def own_key(clients):
    """A team's own key is never metered, so its answer streams instead of being buffered to settle."""
    assert (await clients.post("/secrets", json={"name": "tikhub", "value": "OWN-KEY"})).status_code < 300


async def _never_ending_body():
    yield b"["
    await asyncio.Event().wait()


async def refused_before_any_work(clients, path, *, seen, before):
    """An accepted POST reads its body first, so a body that never ends would hang it."""
    response = await asyncio.wait_for(clients.post(
        f"{path}{EP_DFS}", content=_never_ending_body(), headers={
            "content-type": "application/json", "X-Treg-Call-Id": "caller-invented-id",
            "X-Treg-Org-Id": "999999"}), 5)
    assert response.status_code == 429, response.text
    assert response.headers["X-Treg-Error"] == "1"
    call_ref = response.headers["X-Treg-Call-Id"]
    assert call_ref and call_ref != "caller-invented-id"
    assert seen == []
    assert await _balance(clients) == before
    await _assert_no_money()
    await audit.drain()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.status_code == 429))).scalars().all()
    assert [(row.call_ref, row.status_code) for row in rows] == [(call_ref, 429)]
    return response, rows[0]


@pytest.fixture
def gate(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "org_flow_control_enabled", True)
    monkeypatch.setattr(settings, "org_flow_control_refresh_s", 2)
    monkeypatch.setattr(guard, "_LEASE_S", 0.12)
    monkeypatch.setattr(guard, "_next_unavailable_log", 0.0)
    monkeypatch.setattr(guard, "_suppressed_unavailable_logs", 0)
    monkeypatch.setattr(guard, "_next_rate_log", 0.0)
    monkeypatch.setattr(guard, "_suppressed_rate_logs", 0)
    monkeypatch.setattr(flow, "_reader", flow.Reader())
    store = Slots()
    monkeypatch.setattr(flow, "_clock", lambda: store.clock)
    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: store.clock))
    monkeypatch.setattr(kv, "store", lambda: store)
    return settings, store


def test_default_disabled_and_explicit_enable_validation():
    assert not Settings(_env_file=None).org_flow_control_enabled
    assert not any(name.startswith("org_concurrency_") for name in Settings.model_fields)
    with pytest.raises(ValidationError, match="requires a valid Redis kv_url"):
        Settings(_env_file=None, org_flow_control_enabled=True, kv_url="")
    with pytest.raises(ValidationError, match="valid Redis kv_url"):
        Settings(_env_file=None, org_flow_control_enabled=True, kv_url="http://localhost")


async def test_no_published_limit_never_contacts_slot_store(gate, monkeypatch):
    monkeypatch.setattr(kv, "store", lambda: pytest.fail("bypassed slot admission touched Redis"))
    assert await guard.acquire(1, limit=None) is None


@pytest.mark.parametrize("limit", [None, 2])
async def test_rate_uses_one_combined_admission_and_only_concurrency_owns_a_slot(
    gate, monkeypatch, limit,
):
    _, store = gate
    monkeypatch.setattr(store, "acquire_slot", AsyncMock(side_effect=AssertionError("split admission")))
    slot = await guard.acquire(12, limit=limit, version=7, rate_limit=flow.RateLimit(2.5, 3))
    [(org, token, ttl, effective_limit, rps, burst)] = store.admissions
    assert (org, effective_limit, rps, burst) == (12, limit, 2.5, 3)
    assert token and ttl > 0
    assert store.charges == 1
    if limit is None:
        assert slot is None and store.count == store.releases == 0
        assert not store.renewed.is_set()
    else:
        assert slot is not None and slot.token == token and store.count == 1
        try:
            await asyncio.wait_for(store.renewed.wait(), 1)
        finally:
            await slot.finish()
        assert store.count == 0 and store.releases == 1


@pytest.mark.parametrize("limit", [None, 2])
async def test_rate_rejection_keeps_store_retry_guidance_and_published_version(gate, limit):
    _, store = gate
    store.admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=4)
    with pytest.raises(guard.AdmissionRejected) as caught:
        await guard.acquire(12, limit=limit, version=7, rate_limit=flow.RateLimit(0.25, 1))
    assert caught.value.status_code == 429 and caught.value.retry_after_s == 4
    assert caught.value.detail == {
        "error": "org_rate_limit", "message": "organization request rate limit reached",
        "retry_after_s": 4, "config_version": 7,
    }
    assert store.count == store.charges == 0


async def test_mixed_busy_keeps_concurrency_rejection_and_does_not_use_split_guard(gate, monkeypatch):
    _, store = gate
    store.admit_result = kv.OrgAdmissionResult("busy")
    monkeypatch.setattr(store, "acquire_slot", AsyncMock(side_effect=AssertionError("split admission")))
    with pytest.raises(guard.AdmissionRejected) as caught:
        await guard.acquire(12, limit=2, version=7, rate_limit=flow.RateLimit(1, 2))
    assert caught.value.detail["error"] == "org_concurrency_limit"
    assert caught.value.retry_after_s == 1 and caught.value.detail["config_version"] == 7
    assert store.count == store.charges == 0


@pytest.mark.parametrize("limit", [None, 2])
@pytest.mark.parametrize("reply", ["unavailable", "cancelled"])
async def test_ambiguous_combined_admission_cleans_unique_owner_without_refunding_rate(
    gate, monkeypatch, limit, reply,
):
    _, store = gate
    existing = await guard.acquire(12, limit=2)
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = store.admit_org

    async def ambiguous(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await proceed.wait()
        return kv.OrgAdmissionResult("unavailable")

    monkeypatch.setattr(store, "admit_org", ambiguous)
    task = asyncio.create_task(guard.acquire(
        12, limit=limit, version=7, rate_limit=flow.RateLimit(1, 2)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if reply == "cancelled":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        proceed.set()
        if reply == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
        assert store.count == 1  # Existing requests retain their different owner token.
        assert existing.token in store.owners[existing.key]
        assert store.charges == 1  # An accepted/ambiguous rate charge is never refunded.
    finally:
        proceed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await existing.finish()
    assert store.count == 0


async def test_limit_and_org_isolation(gate):
    _, store = gate
    slots = [await guard.acquire(1, limit=2, version=7),
             await guard.acquire(1, limit=2, version=7),
             await guard.acquire(2, limit=1, version=3)]
    try:
        assert store.count == 3
        assert len({slot.token for slot in slots}) == 3
        with pytest.raises(guard.AdmissionRejected) as caught:
            await guard.acquire(1, limit=2, version=7)
        assert caught.value.status_code == 429
        assert caught.value.detail["config_version"] == 7
    finally:
        await asyncio.gather(*(slot.finish() for slot in slots))
    assert store.count == 0


async def test_redis_outage_allows_instead_of_reporting_over_limit(gate, caplog):
    _, store = gate
    store.acquire_result = "unavailable"
    assert await guard.acquire(1, limit=1, version=4) is None
    assert "acquire_unavailable" in caplog.text
    assert store.count == 0


async def test_outage_warnings_are_bounded_and_report_suppressed_admissions(
    gate, monkeypatch, caplog,
):
    _, store = gate
    store.acquire_result = "unavailable"
    clock = [100.0]
    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    for _ in range(101):
        assert await guard.acquire(1, limit=1, version=4) is None
    messages = [record.getMessage() for record in caplog.records
                if record.name == guard.log.name and "acquire_unavailable" in record.getMessage()]
    assert len(messages) == 1
    assert "action=allow suppressed_since_last_log=0" in messages[0]
    clock[0] += 5
    assert await guard.acquire(2, limit=1, version=6) is None
    messages = [record.getMessage() for record in caplog.records
                if record.name == guard.log.name and "acquire_unavailable" in record.getMessage()]
    assert len(messages) == 2
    assert "org_id=2 config_version=6 action=allow suppressed_since_last_log=100" in messages[-1]
    assert store.count == 0


async def test_redis_client_creation_failure_allows_without_logging_url(gate, monkeypatch, caplog):
    def broken_client():
        raise ValueError("invalid Redis URL option: must-not-log-url")

    monkeypatch.setattr(kv, "store", broken_client)
    assert await guard.acquire(1, limit=1, version=4) is None
    assert "acquire_unavailable" in caplog.text
    assert "must-not-log-url" not in caplog.text


@pytest.mark.parametrize("outcome", ["lost", "unavailable"])
async def test_renewal_loss_keeps_accepted_cleanup_running(gate, caplog, outcome):
    _, store = gate
    slot = await guard.acquire(1, limit=1, version=1)
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
    task = asyncio.create_task(guard.acquire(1, limit=1, version=1))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # cleanup must survive a second cancellation too
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.count == 0


@pytest.mark.parametrize("reply", ["unavailable", "late"])
async def test_unconfirmed_acquisition_allows_after_cleaning_ambiguous_token(gate, monkeypatch, reply):
    _, store = gate
    original = store.acquire_slot

    async def ambiguous(*args):
        await original(*args)
        if reply == "late":
            store.clock += 0.15
            return "acquired"
        return "unavailable"

    monkeypatch.setattr(store, "acquire_slot", ambiguous)
    assert await guard.acquire(1, limit=1, version=1) is None
    assert store.count == 0


async def test_release_failure_is_reported_without_changing_accepted_result(gate, caplog):
    _, store = gate
    slot = await guard.acquire(1, limit=1, version=1)
    store.release_result = "unavailable"
    await slot.finish()
    assert "release_unavailable" in caplog.text
    assert slot.renewal.done()


async def test_close_failure_cannot_mask_original_error_or_leak_slot(gate, caplog):
    _, store = gate
    slot = await guard.acquire(1, limit=1, version=1)
    with pytest.raises(ValueError, match="original"):
        try:
            raise ValueError("original")
        finally:
            await slot.finish(AsyncMock(side_effect=RuntimeError("close failed")))
    assert store.count == 0
    assert "upstream_close_failed" in caplog.text


async def test_long_cleanup_renews_and_survives_anyio_cancellation(gate):
    _, store = gate
    slot = await guard.acquire(1, limit=1, version=1)

    async def close():
        await asyncio.sleep(0.3)
        assert store.count == 1

    with anyio.CancelScope() as scope:
        scope.cancel()
        await slot.finish(close)
    assert store.renewed.is_set()
    assert store.count == 0


async def _configure_org(clients, gate, *, limit=1, rate=None):
    _, store = gate
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    store.publish(org_id, limit, rate=rate)
    return org_id, store


async def test_published_updates_null_and_missing_preserve_in_flight_owners(
    clients, platform_on, gate, monkeypatch,
):
    org, store = await _configure_org(clients, gate)
    entered, proceed = asyncio.Queue(), asyncio.Event()
    closed = []

    def answer():
        close = AsyncMock()
        closed.append(close)
        return _gated(entered, proceed, close)()

    _upstream(monkeypatch, answer)
    tasks = []

    async def start_request():
        task = asyncio.create_task(clients.get(f"/call/{EP}?aweme_id={len(tasks)}"))
        tasks.append(task)
        await asyncio.wait_for(entered.get(), 2)

    try:
        await start_request()
        assert store.count == 1
        rejected = await clients.get(f"/call/{EP}?aweme_id=refused")
        assert rejected.status_code == 429
        assert rejected.json()["detail"]["config_version"] == 1

        store.publish(org, 2, version=2)
        await start_request()
        assert store.count == 2
        store.publish(org, 1, version=3)
        rejected = await clients.get(f"/call/{EP}?aweme_id=refused")
        assert rejected.status_code == 429
        assert rejected.json()["detail"]["config_version"] == 3
        assert store.count == 2  # Lowering a limit does not evict accepted owners.

        acquires = store.acquires
        store.publish(org, None, version=4)
        await start_request()
        assert store.count == 2 and store.acquires == acquires
        del store.publications[org]
        store.clock += 3
        await start_request()
        assert store.count == 2 and store.acquires == acquires

        proceed.set()
        results = await asyncio.gather(*tasks)
        assert all(r.status_code == 200 and r.content == b'{"part":"last"}' for r in results)
        assert store.count == 0
        for close in closed:
            close.assert_awaited_once()
    finally:
        proceed.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("change", ["bad_json", "invalid_limit", "stale", "same_version"])
async def test_bad_or_conflicting_publication_retains_previous_concurrency(
    clients, platform_on, gate, monkeypatch, change,
):
    org, store = await _configure_org(clients, gate)
    store.publish(org, 1, version=2)
    held = await guard.acquire(org, limit=1, version=2)
    seen = _upstream(monkeypatch)
    try:
        assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 429
        if change == "bad_json":
            store.publications[org] = "{"
        elif change == "invalid_limit":
            store.publish(org, True, version=3)
        else:
            store.publish(org, 10, version=1 if change == "stale" else 2)
        store.clock += 3
        response = await clients.get(f"/call/{EP}?aweme_id=7")
        assert response.status_code == 429
        assert response.json()["detail"]["config_version"] == 2
        assert store.count == 1
        assert seen == []
    finally:
        await held.finish()
    assert store.count == 0


@pytest.mark.parametrize("configuration", ["disabled", "missing", "null", "unavailable"])
async def test_root_without_effective_published_limit_performs_no_slot_io(
    clients, platform_on, gate, monkeypatch, configuration,
):
    org, store = await _configure_org(clients, gate)
    if configuration == "disabled":
        monkeypatch.setattr(gate[0], "org_flow_control_enabled", False)
        monkeypatch.setattr(kv, "store", lambda: pytest.fail("disabled controls touched Redis"))
    elif configuration == "missing":
        del store.publications[org]
    elif configuration == "null":
        store.publish(org, None)
    else:
        store.publications[org] = kv.FlowControlReadFailure.UNAVAILABLE
    seen = _upstream(monkeypatch)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200 and response.content == b'{"ok":true}'
    assert len(seen) == 1
    assert store.acquires == store.count == 0
    assert store.reads == (0 if configuration == "disabled" else 1)


@pytest.mark.parametrize("path,policy", [
    ("/call/", "concurrency"), ("/call/", "mixed"), ("/call/", "rate"),
    ("/catalog/call/", "concurrency"), ("/table/", "concurrency"),
])
async def test_rejection_before_money_provider_or_body_with_existing_audit(
    clients, platform_on, gate, monkeypatch, path, policy,
):
    rate = {"rps": 0.25, "burst": 1} if policy != "concurrency" else None
    org, store = await _configure_org(clients, gate, limit=None if policy == "rate" else 1, rate=rate)
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    held = await guard.acquire(org, limit=1, version=1) if policy == "concurrency" else None
    if policy != "concurrency":
        store.admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=4)
    seen = _upstream(monkeypatch)
    try:
        response, row = await refused_before_any_work(clients, path, seen=seen, before=await _balance(clients))
        assert response.headers["Retry-After"] == ("1" if policy == "concurrency" else "4")
        assert response.json()["detail"]["error"] == (
            "org_concurrency_limit" if policy == "concurrency" else "org_rate_limit")
        assert response.json()["detail"]["config_version"] == 1
        assert row.org_id == org
    finally:
        if held is not None:
            await held.finish()
    assert store.count == 0


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
async def test_unauthenticated_root_does_not_read_or_charge_published_rate(
    clients, gate, monkeypatch, path,
):
    _, store = await _configure_org(clients, gate, limit=None, rate={"rps": 1, "burst": 2})
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    monkeypatch.setattr(get_settings(), "claude_connector_enabled", True)
    response = await clients.post(path + "example.operation", headers={"X-Treg-Token": "invalid"})
    assert response.status_code == 401
    assert store.reads == store.charges == 0 and not store.admissions


async def test_all_api_keys_share_the_org_slot(clients, platform_on, gate, monkeypatch):
    org, _ = await _configure_org(clients, gate)
    key = (await clients.post(f"/orgs/{org}/api-keys", json={"name": "second"})).json()["secret"]
    seen = _upstream(monkeypatch)
    held = await guard.acquire(org, limit=1, version=1)
    try:
        for token in (clients.headers["X-Treg-Token"], key):
            response = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Token": token})
            assert response.status_code == 429
        assert seen == []
    finally:
        await held.finish()


@pytest.mark.parametrize("failure", ["unknown_endpoint", "provider_fault"])
@pytest.mark.parametrize("policy", ["concurrency", "mixed", "rate"])
async def test_execution_exception_releases(clients, platform_on, gate, monkeypatch, failure, policy):
    _, store = await _configure_org(
        clients, gate, limit=None if policy == "rate" else 1,
        rate={"rps": 1, "burst": 2} if policy != "concurrency" else None)

    def fault():
        raise RuntimeError("test fault")

    _upstream(monkeypatch, fault)
    if failure == "unknown_endpoint":
        response = await clients.get("/call/tikhub.no-such.endpoint")
        assert response.status_code == 404
    else:
        with pytest.raises(RuntimeError, match="test fault"):
            await clients.get(f"/call/{EP}?aweme_id=7")
    assert store.count == 0
    assert store.charges == (0 if policy == "concurrency" else 1)


async def test_http_redis_outage_allows_an_accepted_submission(clients, platform_on, gate, monkeypatch):
    _, store = await _configure_org(clients, gate)
    store.acquire_result = "unavailable"

    def accepted():
        async def body():
            yield b'{"job_id":"test-job"}'
        return UpstreamResponse(202, ((b"content-type", b"application/json"),), body(), AsyncMock())

    seen = _upstream(monkeypatch, accepted)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 202
    assert len(seen) == 1
    assert store.count == 0


async def test_an_accepted_submission_releases_after_its_response(clients, platform_on, gate, monkeypatch):
    _, store = await _configure_org(clients, gate)

    def accepted():
        async def body():
            yield b'{"job_id":"still-running-remotely"}'
        return UpstreamResponse(202, ((b"content-type", b"application/json"),), body(), AsyncMock())

    _upstream(monkeypatch, accepted)
    for n in range(2):
        assert (await clients.get(f"/call/{EP}?aweme_id={n}")).status_code == 202
        assert store.count == 0


@pytest.mark.parametrize("ending", ["success", "stream_error", "cancel"])
@pytest.mark.parametrize("policy", ["concurrency", "mixed", "rate"])
async def test_stream_owns_slot_through_completion(clients, platform_on, gate, monkeypatch, ending, policy):
    _, store = await _configure_org(
        clients, gate, limit=None if policy == "rate" else 1,
        rate={"rps": 1, "burst": 2} if policy != "concurrency" else None)
    await own_key(clients)
    started, proceed = asyncio.Event(), asyncio.Event()
    closed = AsyncMock()
    _upstream(monkeypatch, _gated(started, proceed, closed, fail=ending == "stream_error"))
    task = asyncio.create_task(clients.get(f"/call/{EP}?aweme_id=7"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.3)  # Outlive multiple leases before the stream ends.
        assert store.renewed.is_set() == (policy != "rate")
        assert store.count == (0 if policy == "rate" else 1)
        if policy == "rate":
            store.admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=1)
        response = await clients.get(f"/call/{EP}?aweme_id=8")
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
                assert (await task).content == b'{"part":"last"}'
        closed.assert_awaited_once()
        assert store.count == 0
        assert store.charges == (0 if policy == "concurrency" else 1)
        if policy == "rate":
            assert store.releases == 0  # Rate-only completion has no lease lifecycle.
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_send_disconnect_before_first_body_releases(gate):
    from starlette.requests import ClientDisconnect
    from starlette.responses import StreamingResponse

    _, store = gate
    slot = await guard.acquire(1, limit=1, version=1)
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


async def test_table_finish_failure_closes_once_and_releases(clients, platform_on, gate, monkeypatch):
    from treg.routers import table

    _, store = await _configure_org(clients, gate)
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    closed = AsyncMock()

    def answer():
        async def body():
            yield b"{}"
        return UpstreamResponse(200, ((b"content-type", b"application/json"),), body(), _closes_once(closed))

    _upstream(monkeypatch, answer)
    monkeypatch.setattr(table, "_table_answer", AsyncMock(side_effect=RuntimeError("table fault")))
    with pytest.raises(RuntimeError, match="table fault"):
        await clients.get(f"/table/{EP}?aweme_id=7")
    closed.assert_awaited_once()
    assert store.count == 0


@pytest.mark.parametrize("policy", ["concurrency", "mixed", "rate"])
async def test_routing_children_take_no_extra_root_slots(
    clients, enrichment_on, gate, monkeypatch, policy,
):
    from test_routing import _relay_by_provider, ROUTED

    _, store = await _configure_org(
        clients, gate, limit=None if policy == "rate" else 1,
        rate={"rps": 1, "burst": 2} if policy != "concurrency" else None)
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "person@example.com", "score": 99,
                                  "verification": {"status": "valid"}}})]}, seen))
    response = await clients.post(f"/call/{ROUTED}", json={"full_name": "Test Person", "domain": "example.com"})
    assert response.status_code == 200, response.text
    assert seen and store.acquires == (1 if policy == "concurrency" else 0)
    assert len(store.admissions) == store.charges == (0 if policy == "concurrency" else 1)
    assert store.count == 0


@pytest.mark.parametrize("policy", ["concurrency", "mixed", "rate"])
async def test_funded_cancellation_keeps_cleanup_inside_slot(
    clients, platform_on, gate, monkeypatch, policy,
):
    import httpx
    from treg.infra import org_traffic
    from test_call_cancellation import (
        _BlockingProviderStream, _BlockingProviderTransport, _funded_org,
        _open_holds, _release_entries, _wait_for_gate,
    )

    counter = org_traffic.Counters()
    monkeypatch.setattr(org_traffic, "counters", counter)
    org, balance = await _funded_org(clients)
    _, store = await _configure_org(
        clients, gate, limit=None if policy == "rate" else 1,
        rate={"rps": 1, "burst": 2} if policy != "concurrency" else None)
    stream = _BlockingProviderStream()
    entered, proceed = asyncio.Event(), asyncio.Event()
    original_cleanup = service._finish_cancelled_call

    async def cleanup(*args, **kwargs):
        entered.set()
        await proceed.wait()
        assert store.count == (0 if policy == "rate" else 1)
        await original_cleanup(*args, **kwargs)

    monkeypatch.setattr(service, "_finish_cancelled_call", cleanup)
    original_execute = routes.execute_call

    async def gc_pause_then_execute(*args, **kwargs):
        time.sleep(0.2)
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(routes, "execute_call", gc_pause_then_execute)
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
            assert store.count == (0 if policy == "rate" else 1)
            assert store.renewed.is_set() == (policy != "rate")
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await _open_holds(org) == []
            assert len(await _release_entries(org, hold.id)) == 1
            assert (await clients.get(f"/orgs/{org}/balance")).json()["balance_micro"] == balance
            assert store.count == 0
            assert store.charges == (0 if policy == "concurrency" else 1)
            [traffic_row] = counter.snapshot()["top"]
            assert traffic_row["requests"] == 0 and traffic_row["completed"] == 1
            assert traffic_row["in_flight_max"] == 1 and not counter.orgs
        finally:
            proceed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("path,tool,args,policy", [
    ("/mcp/", "call", {"endpoint_id": "tikhub.tiktok.video.comments"}, "concurrency"),
    ("/mcp/", "call", {"endpoint_id": "tikhub.tiktok.video.comments"}, "rate"),
    ("/mcp/v2/", "catalog_call_read", {"endpoint_id": "tikhub.tiktok.video.comments"}, "concurrency"),
    ("/mcp/v2/", "catalog_call_write", {"endpoint_id": "dataforseo.web.page.audit"}, "concurrency"),
    ("/mcp/", "call_media", {"endpoint_id": "fishaudio.tts.s2-1-pro", "body": {"text": "test"}}, "concurrency"),
])
async def test_mcp_surfaces_preserve_retry_information(
    clients, platform_on, gate, monkeypatch, path, tool, args, policy,
):
    from test_mcp_directory import paired_mcp_session, _call_tool

    org, store = await _configure_org(
        clients, gate, limit=None if policy == "rate" else 1,
        rate={"rps": 0.25, "burst": 1} if policy != "concurrency" else None)
    held = await guard.acquire(org, limit=1, version=1) if policy == "concurrency" else None
    if policy != "concurrency":
        store.admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=4)
    seen = _upstream(monkeypatch)
    try:
        async with paired_mcp_session() as client:
            out = await _call_tool(client, tool, args, clients.headers["X-Treg-Token"], path=path)
        assert out["status"] == 429, out
        retry = 1 if policy == "concurrency" else 4
        assert out["retry_after"] == str(retry)
        assert out["body"]["detail"]["retry_after_s"] == retry
        assert out["body"]["detail"]["config_version"] == 1
        assert out["body"]["detail"]["error"] == (
            "org_concurrency_limit" if policy == "concurrency" else "org_rate_limit")
        assert seen == []
        await _assert_no_money()
    finally:
        if held is not None:
            await held.finish()


@pytest.mark.parametrize("path", ["/mcp/", "/mcp/v2/"])
async def test_unauthenticated_mcp_does_not_read_or_charge_published_rate(clients, gate, path):
    _, store = await _configure_org(clients, gate, limit=None, rate={"rps": 1, "burst": 2})
    response = await clients.post(path, headers={"X-Treg-Token": "invalid"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "call", "arguments": {"endpoint_id": "example.operation"}},
    })
    assert response.status_code == 401
    assert store.reads == store.charges == 0 and not store.admissions
