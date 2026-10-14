"""Cancellation compensation for funded metered calls.

These tests use the real HTTP route, marketplace resolution, reserve, relay, and ledger. The only
controlled edges are the provider response stream and the reserve commit boundary where cancellation
must be delivered deterministically.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from treg.domain import money as ledger
from treg.application.call import service as call_service
from treg.application.call.types import GatewayFailed
from treg.routers import call as call_routes
from treg.api import app
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import Hold, IdempotentCall, LedgerEntry

from test_marketplace_call import EP, platform_on  # noqa: F401 - shared tier-4 fixture


class _BlockingProviderStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.body_started = asyncio.Event()
        self.allow_body = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self):
        self.body_started.set()
        await self.allow_body.wait()
        yield b'{"ok":true}'

    async def aclose(self) -> None:
        self.close_calls += 1


class _BlockingProviderTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: _BlockingProviderStream) -> None:
        self.stream = stream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=self.stream,
            request=request,
        )


async def _funded_org(clients: AsyncClient) -> tuple[int, int]:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.grant(
            db,
            org_id,
            amount_micro=100_000,
            kind="cancellation_test",
            once=False,
        )
        await db.commit()
    balance = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
    return org_id, balance


async def _open_holds(org_id: int) -> list[Hold]:
    async with session_maker() as db:
        return list((await db.execute(select(Hold).where(Hold.org_id == org_id))).scalars().all())


async def _release_entries(org_id: int, call_id: str) -> list[LedgerEntry]:
    async with session_maker() as db:
        return list((await db.execute(select(LedgerEntry).where(
            LedgerEntry.org_id == org_id,
            LedgerEntry.call_id == call_id,
            LedgerEntry.kind == "release",
        ))).scalars().all())


async def _idempotency_claim(key: str) -> IdempotentCall | None:
    async with session_maker() as db:
        return (await db.execute(select(IdempotentCall).where(
            IdempotentCall.key == key
        ))).scalar_one_or_none()


async def _wait_for_gate(
    event: asyncio.Event,
    task: asyncio.Task[httpx.Response],
    label: str,
) -> None:
    event_task = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            {event_task, task}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if event_task in done:
            return
        if task in done:
            try:
                response = task.result()
            except BaseException as exc:
                raise AssertionError(
                    f"request task failed before {label}: {type(exc).__name__}: {exc}") from exc
            raise AssertionError(
                f"request finished before {label}: HTTP {response.status_code}: "
                f"{response.text[:500]}")
        raise AssertionError(f"timed out after 30s waiting for {label}; request is still running")
    finally:
        if not event_task.done():
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)


async def test_cancelling_a_funded_metered_call_releases_every_resource(
    clients: AsyncClient, platform_on,
):
    org_id, balance_before = await _funded_org(clients)
    stream = _BlockingProviderStream()
    tracked = AsyncClient(
        transport=_BlockingProviderTransport(stream),
        base_url="https://blocking-provider.test",
    )
    original_http = app.state.http
    app.state.http = tracked
    headers = {"Idempotency-Key": "cancelled-funded-call"}
    task = asyncio.create_task(clients.get(f"/call/{EP}?aweme_id=cancel-me", headers=headers))
    try:
        await _wait_for_gate(stream.body_started, task, "provider body start")
        holds = await _open_holds(org_id)
        assert len(holds) == 1, "the provider blocks only after the funded call has reserved"
        call_id = holds[0].id

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream.close_calls == 1, "cancellation closes the provider response exactly once"
        assert await _open_holds(org_id) == []
        balance_after = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
        assert balance_after == balance_before
        releases = await _release_entries(org_id, call_id)
        assert len(releases) == 1
        assert releases[0].meta["reason"] == "call_cancelled"

        async with session_maker() as db:
            claim = (await db.execute(select(IdempotentCall).where(
                IdempotentCall.key == "cancelled-funded-call"
            ))).scalar_one_or_none()
        assert claim is None, "the cancelled request gives its label back immediately"
    finally:
        app.state.http = original_http
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await tracked.aclose()

    retry = await clients.get(f"/call/{EP}?aweme_id=cancel-me", headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.headers.get("X-Treg-Idempotent-Replay") is None


@pytest.mark.parametrize("reserve_committed", [True, False], ids=["committed", "rolled-back"])
async def test_cancellation_at_the_reserve_commit_boundary_is_idempotent(
    clients: AsyncClient, platform_on, monkeypatch, reserve_committed: bool,
):
    org_id, balance_before = await _funded_org(clients)
    commit_reached = asyncio.Event()
    never_return = asyncio.Event()
    original_commit = AsyncSession.commit
    call_id: str | None = None

    async def _gated_commit(db: AsyncSession) -> None:
        nonlocal call_id
        new_hold = next((row for row in db.new if isinstance(row, Hold)), None)
        if new_hold is None:
            await original_commit(db)
            return
        call_id = new_hold.id
        if reserve_committed:
            await original_commit(db)
        else:
            await db.rollback()
        commit_reached.set()
        await never_return.wait()

    monkeypatch.setattr(AsyncSession, "commit", _gated_commit)
    task = asyncio.create_task(clients.get(
        f"/call/{EP}?aweme_id=commit-edge",
        headers={"Idempotency-Key": f"cancel-at-commit-{reserve_committed}"},
    ))
    try:
        await _wait_for_gate(commit_reached, task, "reserve commit boundary")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert call_id is not None
    assert await _open_holds(org_id) == []
    balance_after = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
    assert balance_after == balance_before
    releases = await _release_entries(org_id, call_id)
    if reserve_committed:
        assert len(releases) == 1 and releases[0].meta["reason"] == "call_cancelled"
    else:
        assert releases == [], "releasing a rolled-back reservation is a no-op"


async def test_repeated_cancellation_cannot_interrupt_compensation(
    clients: AsyncClient, platform_on, monkeypatch,
):
    key = "cancelled-three-times"
    org_id, balance_before = await _funded_org(clients)
    stream = _BlockingProviderStream()
    tracked = AsyncClient(
        transport=_BlockingProviderTransport(stream),
        base_url="https://blocking-provider.test",
    )
    original_http = app.state.http
    original_commit = AsyncSession.commit
    original_execute = AsyncSession.execute
    cleanup_commit_reached = asyncio.Event()
    allow_cleanup_commit = asyncio.Event()
    claim_commit_reached = asyncio.Event()
    allow_claim_commit = asyncio.Event()

    async def _tag_claim_delete(db: AsyncSession, statement, *args, **kwargs):
        # Observe the real hold claim without depending on which money entry composes cleanup.
        if (getattr(statement, "is_delete", False)
                and statement.table.name == Hold.__tablename__):
            db.sync_session.info["cancelled_release"] = True
        # The release is one DELETE fenced on the claim's owner, not a load-then-delete.
        if (getattr(statement, "is_delete", False)
                and statement.table.name == IdempotentCall.__tablename__
                and "call_ref" in str(statement)):  # not the intake's expired-label sweep
            db.sync_session.info["claim_delete"] = True
        return await original_execute(db, statement, *args, **kwargs)

    async def _gate_cleanup_commit(db: AsyncSession) -> None:
        if db.sync_session.info.get("cancelled_release"):
            cleanup_commit_reached.set()
            await allow_cleanup_commit.wait()
        if db.sync_session.info.get("claim_delete"):
            claim_commit_reached.set()
            await allow_claim_commit.wait()
        await original_commit(db)

    monkeypatch.setattr(AsyncSession, "execute", _tag_claim_delete)
    monkeypatch.setattr(AsyncSession, "commit", _gate_cleanup_commit)
    app.state.http = tracked
    headers = {"Idempotency-Key": key}
    task = asyncio.create_task(clients.get(
        f"/call/{EP}?aweme_id=cancel-three-times", headers=headers))
    try:
        await _wait_for_gate(stream.body_started, task, "provider body start")
        holds = await _open_holds(org_id)
        assert len(holds) == 1
        call_id = holds[0].id

        task.cancel()
        await _wait_for_gate(cleanup_commit_reached, task, "cancellation release commit")
        task.cancel()
        await asyncio.sleep(0)
        allow_cleanup_commit.set()
        await _wait_for_gate(claim_commit_reached, task, "idempotency claim delete commit")
        task.cancel()
        await asyncio.sleep(0)
        allow_claim_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream.close_calls == 1
        assert await _open_holds(org_id) == []
        balance_after = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
        assert balance_after == balance_before
        releases = await _release_entries(org_id, call_id)
        assert len(releases) == 1 and releases[0].meta["reason"] == "call_cancelled"
        assert await _idempotency_claim(key) is None
    finally:
        allow_cleanup_commit.set()
        allow_claim_commit.set()
        app.state.http = original_http
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await tracked.aclose()

    retry = await clients.get(
        f"/call/{EP}?aweme_id=cancel-three-times", headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.headers.get("X-Treg-Idempotent-Replay") is None


async def test_cancellation_after_claim_before_reserve_releases_the_label(
    clients: AsyncClient, platform_on, monkeypatch,
):
    key = "cancel-before-reserve"
    resolve_reached = asyncio.Event()
    never_resolve = asyncio.Event()
    original_resolve = call_service._resolve_call

    async def _blocked_resolve(*args, **kwargs):
        resolve_reached.set()
        await never_resolve.wait()
        return await original_resolve(*args, **kwargs)

    monkeypatch.setattr(call_service, "_resolve_call", _blocked_resolve)
    task = asyncio.create_task(clients.get(
        f"/call/{EP}?aweme_id=pre-reserve",
        headers={"Idempotency-Key": key},
    ))
    try:
        await _wait_for_gate(resolve_reached, task, "target resolution gate")
        claim = await _idempotency_claim(key)
        assert claim is not None and claim.status == "pending"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert await _idempotency_claim(key) is None
    monkeypatch.setattr(call_service, "_resolve_call", original_resolve)
    retry = await clients.get(
        f"/call/{EP}?aweme_id=pre-reserve",
        headers={"Idempotency-Key": key},
    )
    assert retry.status_code == 200, retry.text


@pytest.mark.parametrize(
    ("failure", "first_reason"),
    [
        (GatewayFailed(
            "connect_failed", status_code=502, detail="upstream failed"), "call_failed_502"),
        (RuntimeError("call path crashed"), "call_crashed"),
    ],
    ids=["call-failed", "call-crashed"],
)
async def test_cancellation_while_failure_release_is_in_flight_finishes_compensation(
    clients: AsyncClient, platform_on, monkeypatch,
    failure: Exception, first_reason: str,
):
    org_id, balance_before = await _funded_org(clients)
    release_commit_reached = asyncio.Event()
    never_finish_first_release = asyncio.Event()
    original_commit = AsyncSession.commit
    original_release = ledger.release_in_transaction
    gated = False
    call_id: str | None = None

    async def _fail_relay(*args, **kwargs):
        raise failure

    async def _tag_first_release(db: AsyncSession, released_call_id: str, **kwargs):
        if kwargs.get("reason") == first_reason:
            db.sync_session.info["first_release"] = released_call_id
        return await original_release(db, released_call_id, **kwargs)

    async def _gate_first_release(db: AsyncSession) -> None:
        nonlocal gated, call_id
        first_release = db.sync_session.info.get("first_release")
        if first_release is not None and not gated:
            gated = True
            call_id = first_release
            release_commit_reached.set()
            await never_finish_first_release.wait()
        await original_commit(db)

    monkeypatch.setattr(call_service, "relay", _fail_relay)
    monkeypatch.setattr(ledger, "release_in_transaction", _tag_first_release)
    monkeypatch.setattr(AsyncSession, "commit", _gate_first_release)
    task = asyncio.create_task(clients.get(
        f"/call/{EP}?aweme_id=failure-release",
        headers={"Idempotency-Key": f"cancel-{first_reason}"},
    ))
    try:
        await _wait_for_gate(release_commit_reached, task, "failure release commit")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert call_id is not None
    assert await _open_holds(org_id) == []
    balance_after = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
    assert balance_after == balance_before
    releases = await _release_entries(org_id, call_id)
    assert len(releases) == 1 and releases[0].meta["reason"] == "call_cancelled"
    assert await _idempotency_claim(f"cancel-{first_reason}") is None


async def test_cancellation_releases_parent_and_overflow_without_a_hold_org_deadlock(
    clients: AsyncClient, platform_on, monkeypatch,
):
    """The real HTTP cancellation cleanup must claim both holds before refunding either one."""
    from test_money_lock_order import _wait_for_blocker

    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    monkeypatch.setattr(get_settings(), "platform_margin", 0)
    org_id, balance_before = await _funded_org(clients)
    key = "cancel-parent-and-overflow"
    stream = _BlockingProviderStream()
    tracked = AsyncClient(transport=_BlockingProviderTransport(stream),
                          base_url="https://blocking-provider.test")
    original_http = app.state.http
    app.state.http = tracked
    request = asyncio.create_task(clients.get(
        f"/call/{EP}?aweme_id=cancel-parent-and-overflow", headers={"Idempotency-Key": key}))
    ordinary = None
    ordinary_paused = asyncio.Event()
    resume_ordinary = asyncio.Event()
    ordinary_pid = 0
    cleanup_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    call_id = ""

    async def pause(driver_connection):
        nonlocal ordinary_pid
        ordinary_pid = driver_connection.get_server_pid()
        ordinary_paused.set()
        await resume_ordinary.wait()

    def after_statement(conn, cursor, statement, parameters, context, executemany):
        if (asyncio.current_task() is ordinary and not ordinary_paused.is_set()
                and statement.lstrip().upper().startswith("DELETE FROM HOLD ")):
            conn.connection.dbapi_connection.run_async(pause)

    def before_statement(conn, cursor, statement, parameters, context, executemany):
        if (not cleanup_pid.done() and call_id in parameters
                and statement.lstrip().upper().startswith("DELETE FROM HOLD ")):
            cleanup_pid.set_result(conn.connection.driver_connection.get_server_pid())

    event.listen(engine.sync_engine, "after_cursor_execute", after_statement)
    event.listen(engine.sync_engine, "before_cursor_execute", before_statement)
    try:
        async with asyncio.timeout(20):
            await _wait_for_gate(stream.body_started, request, "provider body start")
            holds = await _open_holds(org_id)
            assert len(holds) == 1
            call_id = holds[0].id
            overflow_id = f"{call_id}:overflow"
            # A parent request can have a separately reserved overflow child. Stage that valid
            # funded state through money, then race its completion with the HTTP cancellation.
            async with session_maker() as db:
                await ledger.reserve(db, org_id, "test.overflow", 100, call_id=overflow_id)

            async def finish_overflow():
                async with session_maker() as db:
                    return await ledger.settle(db, overflow_id, 60)

            ordinary = asyncio.create_task(finish_overflow())
            await ordinary_paused.wait()
            request.cancel()
            await _wait_for_blocker(await cleanup_pid, ordinary_pid)
            resume_ordinary.set()
            assert await ordinary == 60
            with pytest.raises(asyncio.CancelledError):
                await request

        assert stream.close_calls == 1
        assert await _open_holds(org_id) == []
        assert await _idempotency_claim(key) is None
        async with session_maker() as db:
            assert await ledger.balance_of(db, org_id) == balance_before - 60
            assert sum(b.remaining_micro for b in await ledger.blocks_of(db, org_id)) == balance_before - 60
            assert await ledger.spent_today(db, org_id) == await ledger.spent_today_from_ledger(db, org_id)
            closed = [e for e in await ledger.entries_of(db, org_id)
                      if e.call_id in {call_id, overflow_id} and e.kind in {"settle", "release"}]
            assert sorted((e.call_id, e.kind, e.amount_micro) for e in closed) == sorted([
                (call_id, "release", holds[0].amount_micro), (overflow_id, "settle", -60),
            ])
    finally:
        resume_ordinary.set()
        for task in (ordinary, request):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (ordinary, request) if task is not None),
                             return_exceptions=True)
        event.remove(engine.sync_engine, "after_cursor_execute", after_statement)
        event.remove(engine.sync_engine, "before_cursor_execute", before_statement)
        app.state.http = original_http
        await tracked.aclose()
