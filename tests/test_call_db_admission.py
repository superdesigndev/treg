"""New call transactions share bounded capacity without delaying existing money cleanup."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import event, inspect, select, text

from treg import analytics
from treg.api import app
from treg.application.call import reserve as call_reserve
from treg.application.call import service as call_service
from treg.application.call import settle as call_settle
from treg.application.call.types import ReservationFailed
from treg.domain import money as ledger
from treg.domain.identity import access
from treg.infra import call_db_admission as admission, db as database, money_admission
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry, Org

from conftest import drain_background_writes
from test_marketplace_call import EP, EP_MICRO, platform_on  # noqa: F401 - local ASGI provider fixture
from test_money_admission_application import _call, _fund, _payer, _reserve


def _request(path="/call/test"):
    return Request({"type": "http", "method": "GET", "path": path,
                    "headers": [], "query_string": b""})


def _install_gate(monkeypatch, *, limit=1, wait_s=0.01):
    gate = admission._Gate(limit, wait_s=wait_s)
    monkeypatch.setattr(admission, "_gate", lambda: gate)
    return gate


@pytest.mark.parametrize("size,overflow,expected", [(5, 10, 7), (2, 0, 1), (1, 0, 0), (8, 0, 4)])
def test_budget_uses_combined_configured_pool_capacity(monkeypatch, size, overflow, expected):
    monkeypatch.setitem(database.POOL_SPECS, "api", {"pool_size": size, "max_overflow": overflow})
    assert admission._capacity() == expected


async def test_identity_and_reserve_share_a_bounded_wait_queue(monkeypatch):
    gate = _install_gate(monkeypatch, wait_s=10)
    requested = asyncio.Event()

    async def queued():
        requested.set()
        async with admission.admit("reserve"):
            return "admitted"

    async with admission.admit("identity"):
        waiter = asyncio.create_task(queued())
        try:
            await requested.wait()
            assert gate.waiting == 1
            with pytest.raises(admission.CallDBAdmissionRejected):
                async with asyncio.timeout(0.5), admission.admit("identity"):
                    pytest.fail("an unbounded waiter entered")
            assert not waiter.done()
        finally:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert gate.waiting == 0
    async with admission.admit("reserve"):
        assert gate.active == 1
    assert gate.active == 0


async def test_timeout_releases_waiter_and_body_exception_releases_permit(monkeypatch):
    gate = _install_gate(monkeypatch)
    async with admission.admit("reserve"):
        with pytest.raises(admission.CallDBAdmissionRejected):
            async with admission.admit("identity"):
                pytest.fail("a second transaction entered")
        assert gate.waiting == 0
    with pytest.raises(RuntimeError, match="transaction failed"):
        async with admission.admit("identity"):
            raise RuntimeError("transaction failed")
    async with admission.admit("reserve"):
        assert gate.active == 1
    assert gate.active == 0


@pytest.mark.parametrize("path", ["/call/unknown", "/catalog/call/unknown"])
async def test_identity_overload_is_typed_503_before_checkout(clients, monkeypatch, path):
    await drain_background_writes()
    _install_gate(monkeypatch)
    engine = session_maker.kw["bind"].sync_engine
    checkouts = []
    captured = []
    monkeypatch.setattr(analytics, "capture", lambda _who, name, props, **_kw: captured.append((name, props)))

    def checked_out(*_args):
        checkouts.append(1)

    event.listen(engine, "checkout", checked_out)
    try:
        async with admission.admit("reserve"):
            response = await clients.get(path)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "2"
        assert response.json()["treg_saturated"] is True
        assert checkouts == []
        failures = [props for name, props in captured if name == "call_intake_failed"]
        assert len(failures) == 1
        assert failures[0]["failure_kind"] == "db_admission"
        assert failures[0]["phase"] == "caller_identity"
    finally:
        event.remove(engine, "checkout", checked_out)


async def test_identity_success_keeps_shared_session_and_releases_before_handler(clients, monkeypatch):
    gate = _install_gate(monkeypatch)
    async with session_maker() as db:
        caller = await access.require_member(
            _request(), clients.headers["X-Treg-Token"], "", "", db)
        assert gate.active == 0
        assert not db.in_transaction()
        assert inspect(caller.org).session is db.sync_session
        assert inspect(caller.membership).session is db.sync_session
        assert (await db.execute(select(type(caller.org)).where(
            type(caller.org).id == caller.org_id))).scalar_one() is caller.org
        await db.rollback()
        async with admission.admit("reserve"):
            assert gate.active == 1


@pytest.mark.parametrize("token,expected", [("", "not authenticated"), ("unknown-key", "invalid token")])
async def test_authentication_failure_rolls_back_before_releasing(clients, monkeypatch, token, expected):
    gate = _install_gate(monkeypatch)
    async with session_maker() as db:
        with pytest.raises(HTTPException) as error:
            await access.require_member(_request(), token, "", "", db)
        assert error.value.status_code == 401
        assert error.value.detail == expected
        assert not db.in_transaction()
        assert gate.active == 0


@pytest.mark.parametrize("path", ["/orgs", "/table/test"])
async def test_other_member_dependencies_do_not_take_call_capacity(clients, monkeypatch, path):
    _install_gate(monkeypatch, limit=0)
    async with session_maker() as db:
        caller = await access.require_member(
            _request(path), clients.headers["X-Treg-Token"], "", "", db)
        assert caller.org_id > 0


async def test_identity_holds_permit_through_slow_commit_and_repeated_cancel_rollback(clients, monkeypatch):
    gate = _install_gate(monkeypatch)
    committing, rolling_back, allow_rollback = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with session_maker() as db:
        rollback = db.rollback

        async def slow_commit():
            committing.set()
            await asyncio.Event().wait()

        async def slow_rollback():
            rolling_back.set()
            await allow_rollback.wait()
            await rollback()

        monkeypatch.setattr(db, "commit", slow_commit)
        monkeypatch.setattr(db, "rollback", slow_rollback)
        task = asyncio.create_task(access.require_member(
            _request(), clients.headers["X-Treg-Token"], "", "", db))
        try:
            await asyncio.wait_for(committing.wait(), 5)
            assert gate.active == 1 and db.in_transaction()
            task.cancel()
            await asyncio.wait_for(rolling_back.wait(), 5)
            task.cancel()
            with pytest.raises(admission.CallDBAdmissionRejected):
                async with admission.admit("reserve"):
                    pytest.fail("permit escaped before rollback")
            assert not task.done()
            assert db.in_transaction()
            allow_rollback.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert not db.in_transaction() and gate.active == 0
        finally:
            allow_rollback.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_failed_identity_commit_preserves_error_and_releases_after_rollback(clients, monkeypatch):
    gate = _install_gate(monkeypatch)
    async with session_maker() as db:
        async def failed_commit():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(db, "commit", failed_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            await access.require_member(_request(), clients.headers["X-Treg-Token"], "", "", db)
        assert not db.in_transaction() and gate.active == 0


async def test_failed_identity_rollback_joins_close_before_release(clients, monkeypatch):
    gate = _install_gate(monkeypatch)
    closing, allow_close = asyncio.Event(), asyncio.Event()
    async with session_maker() as db:
        close = db.close

        async def failed_commit():
            raise RuntimeError("commit failed")

        async def failed_rollback():
            raise RuntimeError("rollback failed")

        async def slow_close():
            closing.set()
            await allow_close.wait()
            await close()

        monkeypatch.setattr(db, "commit", failed_commit)
        monkeypatch.setattr(db, "rollback", failed_rollback)
        monkeypatch.setattr(db, "close", slow_close)
        task = asyncio.create_task(access.require_member(
            _request(), clients.headers["X-Treg-Token"], "", "", db))
        try:
            await asyncio.wait_for(closing.wait(), 5)
            with pytest.raises(admission.CallDBAdmissionRejected):
                async with admission.admit("reserve"):
                    pytest.fail("permit escaped before close")
            assert db.in_transaction() and gate.active == 1
            allow_close.set()
            with pytest.raises(RuntimeError, match="commit failed"):
                await asyncio.wait_for(task, 5)
            assert not db.in_transaction() and gate.active == 0
        finally:
            allow_close.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_reserve_waits_before_session_checkout(clients, monkeypatch):
    await drain_background_writes()
    _install_gate(monkeypatch)
    engine = session_maker.kw["bind"].sync_engine
    checkouts = []

    def checked_out(*_args):
        checkouts.append(1)

    event.listen(engine, "checkout", checked_out)
    try:
        async with admission.admit("identity"):
            with pytest.raises(ReservationFailed) as rejected:
                async with call_reserve.reserve_admission(1), session_maker() as db:
                    await db.execute(text("SELECT 1"))
        assert rejected.value.status_code == 503
        assert rejected.value.kind == "db_admission"
        assert rejected.value.blame == "treg"
        assert checkouts == []
    finally:
        event.remove(engine, "checkout", checked_out)


async def test_org_admission_wait_does_not_occupy_shared_budget(monkeypatch):
    gate = _install_gate(monkeypatch)
    waiting, allow = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def org_admit(_orgs, *, operation):
        assert operation == "reserve"
        waiting.set()
        await allow.wait()
        yield

    monkeypatch.setattr(money_admission, "admit", org_admit)

    async def reserve():
        async with call_reserve.reserve_admission(1):
            assert gate.active == 1

    task = asyncio.create_task(reserve())
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        async with admission.admit("identity"):
            assert gate.active == 1
        allow.set()
        await asyncio.wait_for(task, 5)
    finally:
        allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_reserve_keeps_shared_permit_until_cancelled_session_close_finishes(clients, monkeypatch):
    gate = _install_gate(monkeypatch)
    org_id, caller = await _payer("cancelled-reserve")
    db = session_maker()
    close = db.close
    committing, closing, allow_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_commit():
        committing.set()
        await asyncio.Event().wait()

    async def slow_close():
        closing.set()
        await allow_close.wait()
        await close()

    monkeypatch.setattr(db, "commit", slow_commit)
    monkeypatch.setattr(db, "close", slow_close)
    monkeypatch.setattr(call_reserve, "session_maker", lambda: db)
    task = asyncio.create_task(call_reserve._platform_reserve(
        _call(org_id, "cancelled-reserve"), caller, call_ref="cancelled-reserve"))
    try:
        await asyncio.wait_for(committing.wait(), 5)
        assert gate.active == 1 and db.in_transaction()
        task.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        task.cancel()
        with pytest.raises(admission.CallDBAdmissionRejected):
            async with admission.admit("identity"):
                pytest.fail("reserve released its permit before session cleanup")
        assert not task.done()
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not db.in_transaction() and gate.active == 0
    finally:
        allow_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await close()
    async with session_maker() as check:
        assert await check.get(Hold, "cancelled-reserve") is None
        assert await ledger.balance_of(check, org_id) == 1_000


async def test_provider_and_response_stream_do_not_retain_call_db_permit(clients, platform_on, monkeypatch):
    _install_gate(monkeypatch)
    original = call_service.relay
    phases = []

    async def relay(*args, **kwargs):
        async with admission.admit("identity"):
            phases.append("provider")
        response = await original(*args, **kwargs)
        original_stream = response.body_stream

        async def stream():
            async with admission.admit("reserve"):
                phases.append("stream")
            async for chunk in original_stream:
                yield chunk

        response.body_stream = stream()
        return response

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200
    assert phases == ["provider", "stream"]


async def test_reserve_overload_never_calls_provider_and_same_idempotency_key_can_retry(
    clients, platform_on, monkeypatch,
):
    _install_gate(monkeypatch)
    original = call_service._platform_reserve
    original_relay = call_service.relay
    provider_calls = []
    reject = True

    async def reserve(*args, **kwargs):
        if reject:
            async with admission.admit("identity"):
                return await original(*args, **kwargs)
        return await original(*args, **kwargs)

    async def relay(*args, **kwargs):
        provider_calls.append(1)
        return await original_relay(*args, **kwargs)

    monkeypatch.setattr(call_service, "_platform_reserve", reserve)
    monkeypatch.setattr(call_service, "relay", relay)
    url = f"/call/{EP}?aweme_id=7"
    headers = {"Idempotency-Key": "db-admission-retry"}
    rejected = await clients.get(url, headers=headers)
    assert rejected.status_code == 503
    assert rejected.headers["Retry-After"] == "2"
    assert rejected.json()["treg_saturated"] is True
    assert rejected.json()["detail"]["error"] == "db_admission"
    assert provider_calls == []
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        assert (await db.execute(select(LedgerEntry).where(
            LedgerEntry.kind == "reserve"))).scalars().all() == []
    reject = False
    retried = await clients.get(url, headers=headers)
    assert retried.status_code == 200
    assert retried.headers.get("X-Treg-Idempotent-Replay") != "1"
    assert provider_calls == [1]


@pytest.mark.parametrize("status,charged", [(200, 70), (503, 0)])
async def test_many_orgs_can_finish_money_while_new_work_budget_is_full(clients, monkeypatch, status, charged):
    _install_gate(monkeypatch)
    first, second = await _fund("first"), await _fund("second")
    await _reserve(first, "first-call")
    await _reserve(second, "second-call")
    async with admission.admit("identity"):
        results = await asyncio.wait_for(asyncio.gather(*[
            call_settle._platform_settle(_call(org_id, call_id), status, observed_override=70)
            for org_id, call_id in ((first, "first-call"), (second, "second-call"))
        ]), 5)
    assert [result[0] for result in results] == [charged, charged]
    async with session_maker() as db:
        for org_id, call_id in ((first, "first-call"), (second, "second-call")):
            assert await db.get(Hold, call_id) is None
            assert await ledger.balance_of(db, org_id) == 1_000 - charged
            kinds = (await db.execute(select(LedgerEntry.kind).where(
                LedgerEntry.call_id == call_id))).scalars().all()
            assert kinds.count("settle" if charged else "release") == 1


async def test_twenty_simultaneous_http_calls_bound_checkouts_and_only_charge_admitted_work(
    clients, platform_on, monkeypatch,
):
    await drain_background_writes()
    async with session_maker() as db:
        org = (await db.execute(select(Org))).scalars().one()
        org_id, before = org.id, org.balance_micro
        existing_entries = (await db.execute(select(LedgerEntry.kind))).scalars().all()
    limit = admission._capacity()
    assert limit == 7
    gate = _install_gate(monkeypatch, limit=limit, wait_s=0.05)
    committing, allow_commit, start = asyncio.Event(), asyncio.Event(), asyncio.Event()
    completed = asyncio.Queue()
    commit_count = 0
    checkout_count = 0
    connections = set()
    identity_tasks = set()
    provider_calls = []
    engine = session_maker.kw["bind"].sync_engine
    original_relay = call_service.relay

    def checkout(connection, *_args):
        nonlocal checkout_count
        if not allow_commit.is_set() and asyncio.current_task() in identity_tasks:
            checkout_count += 1
            connections.add(id(connection))

    async def slow_identity_session():
        nonlocal commit_count
        identity_tasks.add(asyncio.current_task())
        async with session_maker() as db:
            original_commit = db.commit

            async def commit():
                nonlocal commit_count
                commit_count += 1
                if commit_count == limit:
                    committing.set()
                await allow_commit.wait()
                await original_commit()

            db.commit = commit
            yield db

    async def relay(*args, **kwargs):
        provider_calls.append(1)
        return await original_relay(*args, **kwargs)

    async def call(index):
        await start.wait()
        response = await clients.get(f"/call/{EP}?aweme_id=simultaneous-{index}")
        completed.put_nowait(response)
        return response

    monkeypatch.setitem(app.dependency_overrides, database.get_session, slow_identity_session)
    monkeypatch.setattr(call_service, "relay", relay)
    event.listen(engine, "checkout", checkout)
    tasks = [asyncio.create_task(call(index)) for index in range(20)]
    try:
        start.set()
        async with asyncio.timeout(2):
            await committing.wait()
            refused = [await completed.get() for _ in range(20 - limit)]
        assert [response.status_code for response in refused] == [503] * (20 - limit)
        assert all(response.headers["Retry-After"] == "2" for response in refused)
        assert all(response.json()["treg_saturated"] is True for response in refused)
        assert checkout_count == len(connections) == limit
        assert provider_calls == []
        assert gate.active == limit and gate.waiting == 0
        allow_commit.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert sorted(response.status_code for response in responses) == [200] * limit + [503] * (20 - limit)
        assert len(provider_calls) == limit
        assert gate.active == gate.waiting == 0
    finally:
        allow_commit.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(engine, "checkout", checkout)

    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == before - limit * EP_MICRO
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry.kind))).scalars().all()
        assert entries.count("reserve") - existing_entries.count("reserve") == limit
        assert entries.count("settle") - existing_entries.count("settle") == limit
        assert entries.count("release") == existing_entries.count("release")
    recovered = await clients.get(f"/call/{EP}?aweme_id=after-burst")
    assert recovered.status_code == 200
    assert len(provider_calls) == limit + 1
    assert gate.active == gate.waiting == 0
