"""Admission waits outside application-owned money sessions; accounting remains in PostgreSQL."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from treg.application import asynctasks as task_app
from treg.application.call import reserve as call_reserve
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.application.hub import runner as hub_runner
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.identity.access import Caller
from treg.infra import kv, money_admission
from treg.infra.money_session import money_session
from treg.infra.db import reset_db, session_maker
from treg.models import AsyncTaskRecord, Hold, HubTool, LedgerEntry, Membership, Org, Tool, User
from treg.timeutil import utcnow_naive

from test_marketplace_call import EP, platform_on  # noqa: F401 - real call fixture
from test_money_admission import LeaseStore


@pytest.fixture
async def money_db(monkeypatch):
    await reset_db()
    monkeypatch.setattr(get_settings(), "platform_margin", 0)

    async def no_archive(*_args, **_kwargs):
        pass

    monkeypatch.setattr(task_app.archive, "store_terminal_response", no_archive)


async def _fund(slug: str) -> int:
    async with session_maker() as db:
        org = Org(name=slug, slug=slug)
        db.add(org)
        await db.flush()
        org_id = org.id
        assert org_id is not None
        await ledger.grant(db, org_id, amount_micro=1_000)
        await db.commit()
        return org_id


async def _reserve(org_id: int, call_id: str, amount: int = 100) -> None:
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "test.admission", amount, call_id=call_id)


async def _payer(slug: str) -> tuple[int, Caller]:
    """A detached caller whose org can fund a platform reserve."""
    async with session_maker() as db:
        org = Org(name=slug, slug=slug)
        db.add(org)
        await db.flush()
        assert org.id is not None
        await ledger.grant(db, org.id, amount_micro=1_000)
        user = User(email=f"{slug}@example.invalid")
        db.add(user)
        await db.flush()
        membership = Membership(user_id=user.id, org_id=org.id, role="owner", token_hash=slug)
        db.add(membership)
        await db.commit()
        for row in (org, user, membership):
            await db.refresh(row)
            db.expunge(row)
        return org.id, Caller(membership, user, org, None)


def _checkouts():
    seen = []
    engine = session_maker.kw["bind"].sync_engine

    def checkout(*_args):
        seen.append(1)

    event.listen(engine, "checkout", checkout)
    return engine, checkout, seen


def _call(org_id: int, call_id: str, reserved: int = 100) -> MarketplaceCall:
    return MarketplaceCall(
        tool=Tool(org_id=org_id, name="test", owner="test@example.invalid",
                  base_url="https://example.invalid", host="example.invalid"),
        upstream="https://example.invalid/call", consumed=set(), endpoint_id="test.admission",
        provider="test", tier="platform", cost_type="per_call", estimate_micro=reserved,
        call_id=call_id, payer_org_id=org_id, reserved_micro=reserved,
        settlement_basis={"amount": {"kind": "observed"}, "fallback_micro": reserved},
    )


class AdmissionProbe:
    """Pause the external admission boundary and observe real pool checkout/checkin events."""

    def __init__(self, monkeypatch):
        self.requested = asyncio.Event()
        self.allow = asyncio.Event()
        self.calls = []
        self.connections = set()
        self.checkouts = 0
        self.connections_on_exit = []
        self.engine = session_maker.kw["bind"].sync_engine
        event.listen(self.engine, "checkout", self.checkout)
        event.listen(self.engine, "checkin", self.checkin)
        monkeypatch.setattr(money_admission, "admit", self.admit)

    def checkout(self, connection, *_args):
        self.checkouts += 1
        self.connections.add(id(connection))

    def checkin(self, connection, *_args):
        self.connections.discard(id(connection))

    @asynccontextmanager
    async def admit(self, org_ids, *, operation):
        ids = list(org_ids)
        self.calls.append((operation, ids))
        if ids:
            self.requested.set()
            await self.allow.wait()
        try:
            yield
        finally:
            self.connections_on_exit.append(len(self.connections))

    def close(self):
        event.remove(self.engine, "checkout", self.checkout)
        event.remove(self.engine, "checkin", self.checkin)


async def _paid_case(kind: str, org_id: int):
    call_id = "admitted:price" if kind == "hub" else "admitted"
    await _reserve(org_id, call_id)
    if kind == "close":
        return call_settle._platform_settle(_call(org_id, call_id), 200, observed_override=70), (70, 70), call_id
    if kind == "deferred":
        pending = [call_settle.DeferredSettle(
            call_id, True, 70, None, "", {}, payer_org_id=org_id, reserved_micro=100)]
        return call_settle.close_deferred(pending, charge=True), 70, call_id
    if kind == "hub":
        payee = await _fund("payee")
        tool = HubTool(org_id=payee, tool_id="payee.tool", name="tool", kind="steps", summary="test")
        return hub_runner._close_price(
            tool, "admitted", 100, success=True, actual=70, payer_org_id=org_id), 70, call_id
    now = utcnow_naive()
    row = AsyncTaskRecord(
        call_id=call_id, org_id=org_id, provider="test", endpoint_id="test.admission",
        reserved_micro=100, created_at=now, next_check_at=now,
        settlement_basis={"amount": {"kind": "observed"}, "fallback_micro": 70},
    )
    async with session_maker() as db:
        db.add(row)
        await db.commit()
    return task_app._finish_terminal(row.model_copy(), "success", {}, 200, b"{}", now), "settled", call_id


@pytest.mark.parametrize("kind", ["close", "deferred", "async", "hub"])
async def test_paid_entry_waits_before_checkout_and_releases_after_session_cleanup(money_db, monkeypatch, kind):
    org_id = await _fund("payer")
    operation, expected, call_id = await _paid_case(kind, org_id)
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [(kind, [org_id])]
        assert probe.checkouts == 0, "queued settlement must not consume a database connection"
        probe.allow.set()
        assert await asyncio.wait_for(task, 5) == expected
        assert probe.connections_on_exit == [0], "return the connection before releasing admission"
    finally:
        probe.allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        assert await db.get(Hold, call_id) is None
        assert await ledger.balance_of(db, org_id) == 930
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "settle"))).scalars().all()
        assert [entry.amount_micro for entry in entries] == [-70]


@pytest.mark.parametrize("status,actual", [(503, None), (200, 0), (200, -1)])
async def test_release_and_zero_settlement_wait_before_checkout(money_db, monkeypatch, status, actual):
    org_id = await _fund("release")
    await _reserve(org_id, "release")
    mk = _call(org_id, "release")
    if actual == -1:
        # The provider's observed-cost parser rejects negatives; exercise the raw settlement seam.
        monkeypatch.setattr(call_settle.settlement_basis, "settle", lambda *_args: -1)
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(call_settle._platform_settle(mk, status, observed_override=actual))
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [("release", [org_id])]
        assert probe.checkouts == 0, "a refund must not checkout before the release lease"
        probe.allow.set()
        charged, _ = await asyncio.wait_for(task, 5)
        assert charged == 0
        assert probe.connections_on_exit == [0]
        assert len(probe.calls) == 1, "the refund inside this session is not a second admit"
    finally:
        probe.allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 1_000
        assert await db.get(Hold, "release") is None
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == "release", LedgerEntry.kind.in_(["settle", "release"])))).scalars().all()
        assert kinds == ["release"] if status == 503 else kinds == ["settle"]


async def test_deferred_only_admits_orgs_that_consume_blocks(money_db, monkeypatch):
    first, second = await _fund("first"), await _fund("second")
    await _reserve(first, "charged")
    await _reserve(second, "refunded")
    pending = [
        call_settle.DeferredSettle("refunded", False, None, None, "failed", {}, second, 100),
        call_settle.DeferredSettle("charged", True, 70, None, "", {}, first, 100),
    ]
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    try:
        assert await call_settle.close_deferred(pending, charge=True) == 70
        assert probe.calls == [("deferred", sorted([first, second]))]
    finally:
        probe.close()
    async with session_maker() as db:
        assert await ledger.balance_of(db, first) == 930
        assert await ledger.balance_of(db, second) == 1_000
        assert await db.get(Hold, "charged") is None
        assert await db.get(Hold, "refunded") is None


async def test_cancellation_during_gate_wait_keeps_hold_for_existing_compensation(money_db, monkeypatch):
    org_id = await _fund("cancelled")
    await _reserve(org_id, "cancelled")
    mk = _call(org_id, "cancelled")
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(call_settle._platform_settle(mk, 200, observed_override=70))
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert probe.checkouts == 0
        probe.allow.set()
        await call_settle._finish_cancelled_call(None, mk, "cancelled")
        assert probe.calls == [("close", [org_id]), ("release", [org_id])]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 1_000
        assert await db.get(Hold, "cancelled") is None
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == "cancelled"))).scalars().all()
        assert kinds.count("release") == 1 and "settle" not in kinds


async def test_admission_survives_commit_until_session_cleanup(money_db, monkeypatch):
    org_id = await _fund("committed")
    await _reserve(org_id, "committed")
    mk = _call(org_id, "committed")
    committed, never = asyncio.Event(), asyncio.Event()
    original = AsyncSession.commit

    async def after_commit(db):
        await original(db)
        committed.set()
        await never.wait()

    monkeypatch.setattr(AsyncSession, "commit", after_commit)
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    task = asyncio.create_task(call_settle._platform_settle(mk, 200, observed_override=70))
    try:
        await asyncio.wait_for(committed.wait(), 5)
        assert probe.connections_on_exit == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert probe.connections_on_exit == [0]
        monkeypatch.setattr(AsyncSession, "commit", original)
        await call_settle._finish_cancelled_call(None, mk, "committed")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 930
        assert await db.get(Hold, "committed") is None
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == "committed", LedgerEntry.kind.in_(["settle", "release"])))).scalars().all()
        assert [(entry.kind, entry.amount_micro) for entry in entries] == [("settle", -70)]


async def test_real_call_carries_reserved_payer_to_admission(clients, platform_on, monkeypatch):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    try:
        response = await clients.get(f"/call/{EP}?aweme_id=admission")
        assert response.status_code == 200, response.text
        assert probe.calls == [("reserve", [org_id]), ("close", [org_id])]
    finally:
        probe.close()


async def test_deferred_child_keeps_payer_until_parent_closes(money_db, monkeypatch):
    org_id = await _fund("deferred-child")
    await _reserve(org_id, "child")
    mk = _call(org_id, "child")
    mk.settlement_basis["fallback_micro"] = 70
    mk.deferred = []
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    try:
        assert (await call_settle._platform_settle(mk, 200))[0] == 70
        assert probe.calls == [], "children must not own admission while the parent still runs upstream"
        assert len(mk.deferred) == 1
        assert mk.deferred[0].payer_org_id == org_id
        assert mk.deferred[0].reserved_micro == 100
        assert await call_settle.close_deferred(mk.deferred, charge=True) == 70
        assert probe.calls == [("deferred", [org_id])]
    finally:
        probe.close()


async def test_async_usage_wait_skips_admission_until_the_balance_row(money_db, monkeypatch):
    org_id = await _fund("async-awaiting")
    call_id = "async-awaiting"
    await _reserve(org_id, call_id)
    now = utcnow_naive()
    row = AsyncTaskRecord(
        call_id=call_id, org_id=org_id, provider="test", endpoint_id="test.admission",
        reserved_micro=100, created_at=now, next_check_at=now,
        settlement_basis={"amount": {"kind": "usage", "path": "usage.cost", "unit": "usd"},
                          "reserve_micro": 100},
    )
    async with session_maker() as db:
        db.add(row)
        await db.commit()
    probe = AdmissionProbe(monkeypatch)
    try:
        result = await asyncio.wait_for(task_app._finish_terminal(
            row.model_copy(), "success", {}, 200, b"{}", now, require_usage=True), 5)
        assert result == "awaiting_usage"
        assert probe.calls == []
        assert probe.checkouts == 1
    finally:
        probe.close()
    async with session_maker() as db:
        assert await db.get(Hold, call_id) is not None
        assert await ledger.balance_of(db, org_id) == 900


def _usage_task(org_id: int, call_id: str, created) -> AsyncTaskRecord:
    return AsyncTaskRecord(
        call_id=call_id, org_id=org_id, provider="test", endpoint_id="test.admission",
        reserved_micro=100, created_at=created, next_check_at=created,
        settlement_basis={"amount": {"kind": "usage", "path": "usage.cost", "unit": "usd"},
                          "reserve_micro": 100},
    )


async def test_usage_failure_admits_release_before_checkout(money_db, monkeypatch):
    """require_usage does not exempt a failure that will return the hold."""
    org_id = await _fund("async-usage-failure")
    call_id = "async-usage-failure"
    await _reserve(org_id, call_id)
    now = utcnow_naive()
    row = _usage_task(org_id, call_id, now)
    async with session_maker() as db:
        db.add(row)
        await db.commit()
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(task_app._finish_terminal(
        row.model_copy(), "failure", {}, 200, b"{}", now, require_usage=True))
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [("release", [org_id])]
        assert probe.checkouts == 0
        probe.allow.set()
        assert await asyncio.wait_for(task, 5) == "released"
        assert probe.connections_on_exit == [0]
        assert len(probe.calls) == 1
    finally:
        probe.allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        stored = await db.get(AsyncTaskRecord, call_id)
        assert stored is not None and stored.status == "released"
        assert await db.get(Hold, call_id) is None
        assert await ledger.balance_of(db, org_id) == 1_000
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == call_id))).scalars().all()
        assert kinds.count("release") == 1


@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_expired_terminal_without_usage_admits_release(money_db, monkeypatch, outcome):
    """An expired terminal state is a refund, not an awaiting-usage task-row update."""
    org_id = await _fund(f"async-expired-{outcome}")
    call_id = f"async-expired-{outcome}"
    await _reserve(org_id, call_id)
    now = utcnow_naive()
    created = now - timedelta(hours=24)
    row = _usage_task(org_id, call_id, created)
    async with session_maker() as db:
        db.add(row)
        await db.commit()
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(task_app._finish_terminal(
        row.model_copy(), outcome, {}, 200, b"{}", now, require_usage=True))
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [("release", [org_id])]
        assert probe.checkouts == 0
        probe.allow.set()
        assert await asyncio.wait_for(task, 5) == "timed_out"
        assert probe.connections_on_exit == [0]
        assert len(probe.calls) == 1
    finally:
        probe.allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        stored = await db.get(AsyncTaskRecord, call_id)
        assert stored is not None and stored.status == "timed_out"
        assert await db.get(Hold, call_id) is None
        assert await ledger.balance_of(db, org_id) == 1_000
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == call_id))).scalars().all()
        assert kinds.count("release") == 1


async def test_async_failure_release_waits_before_checkout(money_db, monkeypatch):
    org_id = await _fund("async-failure")
    call_id = "async-failure"
    await _reserve(org_id, call_id)
    now = utcnow_naive()
    row = AsyncTaskRecord(
        call_id=call_id, org_id=org_id, provider="test", endpoint_id="test.admission",
        reserved_micro=100, created_at=now, next_check_at=now,
        settlement_basis={"amount": {"kind": "observed"}, "fallback_micro": 100},
    )
    async with session_maker() as db:
        db.add(row)
        await db.commit()
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(task_app._finish_terminal(
        row.model_copy(), "failure", {}, 200, b"{}", now))
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [("release", [org_id])]
        assert probe.checkouts == 0
        probe.allow.set()
        assert await asyncio.wait_for(task, 5) == "released"
        assert probe.connections_on_exit == [0]
        assert len(probe.calls) == 1
    finally:
        probe.allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        assert await db.get(Hold, call_id) is None
        assert await ledger.balance_of(db, org_id) == 1_000
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == call_id))).scalars().all()
        assert kinds.count("release") == 1


async def test_known_rollback_releases_admission_before_retrying_whole_settlement(money_db, monkeypatch):
    from sqlalchemy.exc import DBAPIError

    org_id = await _fund("retry")
    await _reserve(org_id, "retry")
    original = ledger.settle_in_transaction
    attempts = 0

    class Deadlock(Exception):
        sqlstate = "40P01"

    async def deadlock_after_writes(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        consumed = await original(*args, **kwargs)
        if attempts == 1:
            raise DBAPIError("test deadlock", {}, Deadlock())
        return consumed

    monkeypatch.setattr(ledger, "settle_in_transaction", deadlock_after_writes)
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    try:
        result = await call_settle._platform_settle(_call(org_id, "retry"), 200, observed_override=70)
        assert result == (70, 70)
        assert attempts == 2
        assert probe.calls == [("close", [org_id]), ("close", [org_id])]
        assert probe.connections_on_exit == [0, 0]
    finally:
        probe.close()
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 930
        assert await db.get(Hold, "retry") is None
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == "retry", LedgerEntry.kind == "settle"))).scalars().all()
        assert [entry.amount_micro for entry in entries] == [-70]


@pytest.mark.parametrize("kind", ["close", "deferred", "async", "hub"])
async def test_repeated_cancel_joins_session_rollback_before_releasing_admission(money_db, monkeypatch, kind):
    org_id = await _fund("repeated-cancel")
    operation, _, call_id = await _paid_case(kind, org_id)
    ledger_done, closing, allow_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_close = AsyncSession.close
    ledger_method = ("settle_to_in_transaction" if kind == "hub" else
                     "close_holds_in_transaction" if kind == "deferred" else "settle_in_transaction")
    original_ledger = getattr(ledger, ledger_method)

    async def before_commit(*args, **kwargs):
        result = await original_ledger(*args, **kwargs)
        ledger_done.set()
        await asyncio.Event().wait()
        return result

    async def delayed_close(db):
        closing.set()
        await allow_close.wait()
        await original_close(db)

    monkeypatch.setattr(ledger, ledger_method, before_commit)
    monkeypatch.setattr(AsyncSession, "close", delayed_close)
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(ledger_done.wait(), 5)
        assert probe.connections
        task.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "repeated cancellation must still join session cleanup"
        assert probe.connections and not probe.connections_on_exit
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not probe.connections
        assert probe.connections_on_exit == [0]
    finally:
        allow_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
        monkeypatch.setattr(AsyncSession, "close", original_close)
    async with session_maker() as db:
        # Cancellation happened before commit: all staged writes rolled back together.
        assert await db.get(Hold, call_id) is not None
        assert await ledger.balance_of(db, org_id) == 900
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "settle"))).scalars().all()
        assert entries == []
        await ledger.release(db, call_id, reason="test_cancelled")
        assert await ledger.balance_of(db, org_id) == 1_000


async def test_cancellation_first_arriving_during_session_exit_still_propagates(money_db, monkeypatch):
    org_id = await _fund("cancel-on-exit")
    closing, allow_close = asyncio.Event(), asyncio.Event()
    original_close = AsyncSession.close

    async def delayed_close(db):
        closing.set()
        await allow_close.wait()
        await original_close(db)

    monkeypatch.setattr(AsyncSession, "close", delayed_close)
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()

    async def work():
        async with money_admission.admit([org_id], operation="close"), money_session(session_maker()) as db:
            await db.get(Org, org_id)

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(closing.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert probe.connections and not probe.connections_on_exit
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert probe.connections_on_exit == [0]
    finally:
        allow_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
        monkeypatch.setattr(AsyncSession, "close", original_close)


def _enable_real_admission(monkeypatch, *, wait_s: float = 2.0, unavailable: bool = False) -> LeaseStore:
    """The real gate, not AdmissionProbe. Probe tests cannot show a Redis lease or a fallback."""
    settings = get_settings()
    monkeypatch.setattr(settings, "money_admission_enabled", True)
    monkeypatch.setattr(settings, "money_admission_org_ids", [])
    monkeypatch.setattr(settings, "money_admission_wait_s", wait_s)
    store = LeaseStore()
    store.unavailable = unavailable
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "store", lambda: store)
    monkeypatch.setattr(money_admission, "_local_loop", None)
    monkeypatch.setattr(money_admission, "_windows", {})
    monkeypatch.setattr(money_admission, "_current", {})
    return store


def _stop_listening(engine, checkout) -> None:
    event.remove(engine, "checkout", checkout)


async def test_reserve_waits_before_checkout_and_reaps_without_a_second_admit(money_db, monkeypatch):
    org_id, caller = await _payer("reserve-reap")
    await _reserve(org_id, "stale-hold", 50)
    async with session_maker() as db:
        hold = await db.get(Hold, "stale-hold")
        assert hold is not None
        hold.created_at = datetime(2000, 1, 1)
        await db.commit()
    mk = _call(org_id, "fresh-hold")
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(call_reserve._platform_reserve(mk, caller, call_ref="fresh-hold"))
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [("reserve", [org_id])]
        assert probe.checkouts == 0, "a reserve must not checkout while it is waiting for admission"
        probe.allow.set()
        await asyncio.wait_for(task, 5)
        assert probe.calls == [("reserve", [org_id])], "the reaper's release stays inside this admit"
        assert probe.connections_on_exit == [0]
    finally:
        probe.allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        assert await db.get(Hold, "stale-hold") is None
        fresh = await db.get(Hold, "fresh-hold")
        assert fresh is not None and fresh.amount_micro == 100
        assert await ledger.balance_of(db, org_id) == 900
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == "stale-hold"))).scalars().all()
        assert kinds.count("release") == 1


async def test_pure_release_batches_admit_once_as_release(money_db, monkeypatch):
    first, second = await _fund("batch-a"), await _fund("batch-b")
    await _reserve(first, "batch-a")
    await _reserve(second, "batch-b")
    pending = [
        call_settle.DeferredSettle("batch-b", True, 40, None, "", {}, second, 100),
        call_settle.DeferredSettle("batch-a", True, 40, None, "", {}, first, 100),
    ]
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    try:
        assert await call_settle.close_deferred(pending, charge=False) == 0
        assert probe.calls == [("release", sorted([first, second]))]
    finally:
        probe.close()
    async with session_maker() as db:
        assert await ledger.balance_of(db, first) == 1_000
        assert await ledger.balance_of(db, second) == 1_000
        assert await db.get(Hold, "batch-a") is None
        assert await db.get(Hold, "batch-b") is None
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id.in_(["batch-a", "batch-b"]),
            LedgerEntry.kind.in_(["settle", "release"])))).scalars().all()
        assert sorted(kinds) == ["release", "release"]


async def test_all_zero_deferred_batch_admits_as_release(money_db, monkeypatch):
    org_id = await _fund("zero-batch")
    await _reserve(org_id, "zero-batch")
    pending = [call_settle.DeferredSettle("zero-batch", True, 0, None, "", {}, org_id, 100)]
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    try:
        assert await call_settle.close_deferred(pending, charge=True) == 0
        assert probe.calls == [("release", [org_id])]
    finally:
        probe.close()
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 1_000
        assert await db.get(Hold, "zero-batch") is None
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == "zero-batch",
            LedgerEntry.kind.in_(["settle", "release"])))).scalars().all()
        assert kinds == ["settle"]


async def test_missing_identity_refuses_balance_sessions_without_checkout(money_db, monkeypatch):
    org_id = await _fund("identity")
    await _reserve(org_id, "identity-settle")
    await _reserve(org_id, "identity-cancel")
    await _reserve(org_id, "identity-batch")
    engine, checkout, seen = _checkouts()
    try:
        mk = _call(org_id, "identity-settle")
        mk.payer_org_id = None
        charged, _observed = await call_settle._platform_settle(mk, 503)
        assert charged == 0
        cancel = _call(org_id, "identity-cancel")
        cancel.payer_org_id = None
        await call_settle._finish_cancelled_call(None, cancel, "identity-cancel")
        pending = [call_settle.DeferredSettle(
            "identity-batch", False, None, None, "failed", {}, None, 100)]
        assert await call_settle.close_deferred(pending, charge=False) == 0
        org = Org(name="missing", slug="missing", autotopup_enabled=False)
        user = User(email="missing@example.invalid")
        membership = Membership(user_id=1, org_id=0, role="owner", token_hash="missing")
        caller = Caller(membership, user, org, None)
        with pytest.raises(money_admission.AdmissionRefused, match="missing_identity") as caught:
            await call_reserve._platform_reserve(_call(0, "identity-reserve"), caller, call_ref="identity-reserve")
        assert not hasattr(caught.value, "status_code")
        assert seen == [], "a refused identity must not open a balance session"
    finally:
        _stop_listening(engine, checkout)
    async with session_maker() as db:
        for call_id in ("identity-settle", "identity-cancel", "identity-batch"):
            assert await db.get(Hold, call_id) is not None
            kinds = (await db.execute(select(LedgerEntry.kind).where(
                LedgerEntry.call_id == call_id, LedgerEntry.kind.in_(["settle", "release"])))).scalars().all()
            assert kinds == []


async def test_same_org_reserve_does_not_checkout_while_a_lease_is_held(money_db, monkeypatch):
    store = _enable_real_admission(monkeypatch, wait_s=5)
    org_a, caller_a = await _payer("lease-a")
    org_b, caller_b = await _payer("lease-b")
    release_lease = asyncio.Event()
    entered = asyncio.Event()

    async def holding():
        async with money_admission.admit([org_a], operation="release"):
            entered.set()
            await release_lease.wait()
    holder = asyncio.create_task(holding())
    engine, checkout, seen = _checkouts()
    task_a = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert list(store.values) == [f"money-admission:org:{org_a}"]
        task_a = asyncio.create_task(call_reserve._platform_reserve(
            _call(org_a, "lease-a"), caller_a, call_ref="lease-a"))
        deadline = asyncio.get_running_loop().time() + 2
        while money_admission._current.get(("reserve", "redis"), {}).get("waiting", 0) < 1:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("same-org reserve did not wait outside the database")
            await asyncio.sleep(0.01)
        assert seen == [], "a leased same-org reserve must not hold a pool connection"
        await asyncio.wait_for(call_reserve._platform_reserve(
            _call(org_b, "lease-b"), caller_b, call_ref="lease-b"), 5)
        assert seen, "another org still checks out and reserves"
        assert not task_a.done()
        assert money_admission._current[("reserve", "redis")]["waiting"] == 1
        assert money_admission._current[("reserve", "redis")]["active"] == 0
        assert f"money-admission:org:{org_a}" in store.values
        release_lease.set()
        await asyncio.wait_for(task_a, 5)
    finally:
        release_lease.set()
        holder.cancel()
        if task_a is not None and not task_a.done():
            task_a.cancel()
        await asyncio.gather(holder, *([task_a] if task_a is not None else []), return_exceptions=True)
        _stop_listening(engine, checkout)
    async with session_maker() as db:
        assert await db.get(Hold, "lease-a") is not None
        assert await db.get(Hold, "lease-b") is not None


async def test_wait_and_kv_fallback_still_complete_the_ledger(money_db, monkeypatch):
    org_id, caller = await _payer("fallback-reserve")
    store = _enable_real_admission(monkeypatch, wait_s=0.2)
    store.values[f"money-admission:org:{org_id}"] = ("another-owner", time.monotonic() + 30)
    await call_reserve._platform_reserve(_call(org_id, "fallback-reserve"), caller, call_ref="fallback-reserve")
    rows = [row for row in money_admission.snapshot() if row["completed"] and row["operation"] == "reserve"]
    assert len(rows) == 1
    assert rows[0]["mode"] == "fallback" and rows[0]["fallback_wait_timeout"] == 1
    assert rows[0]["failed"] == 0

    release_org = await _fund("fallback-release")
    await _reserve(release_org, "fallback-release")
    monkeypatch.setattr(money_admission, "_windows", {})
    monkeypatch.setattr(money_admission, "_current", {})
    store.unavailable = True
    charged, _observed = await call_settle._platform_settle(_call(release_org, "fallback-release"), 503)
    assert charged == 0
    rows = [row for row in money_admission.snapshot() if row["completed"] and row["operation"] == "release"]
    assert len(rows) == 1
    assert rows[0]["mode"] == "fallback" and rows[0]["fallback_kv_unavailable"] == 1
    assert rows[0]["failed"] == 0
    async with session_maker() as db:
        assert await db.get(Hold, "fallback-reserve") is not None
        assert await db.get(Hold, "fallback-release") is None
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == "fallback-release",
            LedgerEntry.kind.in_(["settle", "release"])))).scalars().all()
        assert kinds == ["release"]


def _hub_parent(caller: Caller):
    return SimpleNamespace(input=SimpleNamespace(caller=caller), meta=SimpleNamespace(tags={}))


async def test_hub_price_reserve_and_release_admit_once(money_db, monkeypatch):
    org_id, caller = await _payer("hub-buyer")
    own = HubTool(org_id=org_id, tool_id="buyer.tool", name="tool", kind="steps", summary="test", version=1)
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    try:
        assert await hub_runner._reserve_price(_hub_parent(caller), own, "own-run", 80) == 0
        assert await hub_runner._close_price(own, "own-run", 0, success=False, payer_org_id=org_id) == 0
        assert probe.calls == []
    finally:
        probe.close()

    payee = await _fund("hub-maker")
    tool = HubTool(org_id=payee, tool_id="maker.tool", name="tool", kind="steps", summary="test", version=1)
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(hub_runner._reserve_price(_hub_parent(caller), tool, "hub-run", 80))
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [("reserve", [org_id])]
        assert probe.checkouts == 0
        probe.allow.set()
        assert await asyncio.wait_for(task, 5) == 80
        assert probe.connections_on_exit == [0]
        probe.requested.clear()
        probe.allow.clear()
        checkouts = probe.checkouts
        task = asyncio.create_task(hub_runner._close_price(
            tool, "hub-run", 80, success=False, payer_org_id=org_id, reason="hub_run_failed"))
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [("reserve", [org_id]), ("release", [org_id])]
        assert probe.checkouts == checkouts, "the price refund must not checkout before its release lease"
        probe.allow.set()
        assert await asyncio.wait_for(task, 5) == 0
        assert probe.calls == [("reserve", [org_id]), ("release", [org_id])]
    finally:
        probe.allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        assert await db.get(Hold, "hub-run:price") is None
        assert await ledger.balance_of(db, org_id) == 1_000
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == "hub-run:price",
            LedgerEntry.kind.in_(["settle", "release"])))).scalars().all()
        assert kinds == ["release"]

    await _reserve(org_id, "hub-zero:price", 80)
    probe = AdmissionProbe(monkeypatch)
    probe.allow.set()
    try:
        assert await hub_runner._close_price(
            tool, "hub-zero", 80, success=True, actual=0, payer_org_id=org_id) == 0
        assert probe.calls == [("release", [org_id])]
    finally:
        probe.close()
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 1_000
        assert await ledger.balance_of(db, payee) == 1_000
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == "hub-zero:price",
            LedgerEntry.kind.in_(["settle", "release"])))).scalars().all()
        assert kinds == ["settle"]


async def test_async_timeout_release_waits_before_the_balance_session(money_db, monkeypatch):
    org_id = await _fund("async-timeout")
    call_id = "async-timeout"
    await _reserve(org_id, call_id)
    now = utcnow_naive()
    row = AsyncTaskRecord(
        call_id=call_id, org_id=org_id, provider="test", endpoint_id="test.admission",
        reserved_micro=100, created_at=now - timedelta(hours=25), next_check_at=now,
        settlement_basis={"amount": {"kind": "observed"}, "fallback_micro": 100},
    )
    async with session_maker() as db:
        db.add(row)
        await db.commit()
    probe = AdmissionProbe(monkeypatch)
    task = asyncio.create_task(task_app._process(call_id, None, 0))
    try:
        await asyncio.wait_for(probe.requested.wait(), 5)
        assert probe.calls == [("release", [org_id])]
        assert not probe.connections, "the task read has checked in before the balance lease"
        probe.allow.set()
        assert await asyncio.wait_for(task, 5) == "timed_out"
        assert probe.connections_on_exit == [0]
        assert len(probe.calls) == 1
    finally:
        probe.allow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        probe.close()
    async with session_maker() as db:
        assert await db.get(Hold, call_id) is None
        assert await ledger.balance_of(db, org_id) == 1_000
        stored = await db.get(AsyncTaskRecord, call_id)
        assert stored is not None and stored.status == "timed_out"
        kinds = (await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == call_id))).scalars().all()
        assert kinds.count("release") == 1


async def test_session_cleanup_failure_does_not_replace_original_money_error(caplog):
    failure = ValueError("original money failure")

    class BrokenSession:
        async def close(self):
            raise RuntimeError("close failed")

    with pytest.raises(ValueError) as caught:
        async with money_session(BrokenSession()):
            raise failure
    assert caught.value is failure
    assert "money session cleanup failed" in caplog.text
    assert "close failed" in caplog.text
