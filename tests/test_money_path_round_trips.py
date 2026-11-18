"""Database work on a metered call's money path: pool checkouts per call, statements under the
team's money session, no connection held while the caller uploads, auto top-up after reserve, and
the stale-hold sweep that can replace the lazy reap."""

from __future__ import annotations

import asyncio
import traceback
from datetime import timedelta

import greenlet
import pytest
from httpx import AsyncClient
from sqlalchemy import event, func, select, update

from treg import audit
from treg.application import billing
from treg.application import hold_sweep
from treg.application.call import reserve as call_reserve
from treg.application.call import service as call_service
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.money import allowance
from treg.domain.identity import api_keys as managed_keys
from treg.domain.identity.access import Caller
from treg.infra.db import reset_db, session_maker
from treg.models import CreditBlock, Hold, LedgerEntry, Membership, Org, Tool, User
from treg.timeutil import utcnow_naive

from test_marketplace_call import EP, EP_DFS, _fake_relay, platform_on  # noqa: F401 - fixture reuse


async def test_metered_call_takes_four_pool_checkouts(clients: AsyncClient, platform_on, monkeypatch):
    """Sign-in, then one session from resolution through authorization, then reserve and settle."""
    await audit.drain()
    monkeypatch.setattr(managed_keys, "touch", lambda *args, **kwargs: None)
    assert (await clients.get(f"/call/{EP}?aweme_id=warm")).status_code == 200
    await audit.drain()
    checkouts: list[int] = []

    def on_checkout(*_args):
        # The audit writer flushes its batch on its own task, whenever it runs; it is not the call.
        parent = greenlet.getcurrent().parent
        frames = traceback.extract_stack(parent.gr_frame if parent else None)
        if not any(frame.filename.endswith("/treg/audit.py") for frame in frames):
            checkouts.append(1)

    engine = session_maker.kw["bind"].sync_engine
    event.listen(engine, "checkout", on_checkout)
    try:
        response = await clients.get(f"/call/{EP}?aweme_id=count")
    finally:
        event.remove(engine, "checkout", on_checkout)
    assert response.status_code == 200
    assert response.headers["X-Treg-Cost-Micro"] != "0"
    assert len(checkouts) == 4


@pytest.fixture
async def money_db(monkeypatch):
    await reset_db()
    monkeypatch.setattr(get_settings(), "platform_margin", 0)


async def _payer(slug: str, **org_fields) -> tuple[int, Caller]:
    async with session_maker() as db:
        org = Org(name=slug, slug=slug, **org_fields)
        db.add(org)
        await db.flush()
        await ledger.grant(db, org.id, amount_micro=10_000)
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


def _call(org_id: int, estimate: int = 100) -> MarketplaceCall:
    return MarketplaceCall(
        tool=Tool(org_id=org_id, name="test", owner="test@example.invalid",
                  base_url="https://example.invalid", host="example.invalid"),
        upstream="https://example.invalid/call", consumed=set(), endpoint_id="test.money_path",
        provider="test", tier="platform", cost_type="per_call", estimate_micro=estimate,
        settlement_basis={"amount": {"kind": "observed"}, "fallback_micro": estimate},
    )


class _Statements:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self._engine = session_maker.kw["bind"].sync_engine

    def _seen(self, _conn, _cursor, statement, *_args):
        self.sql.append(" ".join(statement.split()).lower())

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._seen)
        return self

    def __exit__(self, *_exc):
        event.remove(self._engine, "before_cursor_execute", self._seen)


async def _drift(org_id: int) -> int:
    async with session_maker() as db:
        balance = await ledger.balance_of(db, org_id)
        blocks = (await db.execute(select(func.coalesce(func.sum(CreditBlock.remaining_micro), 0))
                                   .where(CreditBlock.org_id == org_id))).scalar_one()
        held = (await db.execute(select(func.coalesce(func.sum(Hold.amount_micro), 0))
                                 .where(Hold.org_id == org_id))).scalar_one()
    return int(balance) - (int(blocks) - int(held))


async def test_reserve_and_settle_read_no_row_twice(money_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "reserve_reaps_stale_holds", False)
    org_id, caller = await _payer("money-path-statements")
    mk = _call(org_id)
    with _Statements() as reserve_sql:
        await call_reserve._platform_reserve(mk, caller, call_ref="statements")
    with _Statements() as settle_sql:
        assert (await call_settle._platform_settle(mk, 200, observed_override=70))[0] == 70

    gate = next(i for i, sql in enumerate(reserve_sql.sql) if sql.startswith("update org"))
    assert "returning" in reserve_sql.sql[gate]
    assert not [sql for sql in reserve_sql.sql[gate:] if sql.startswith("select") and "from org" in sql]
    assert not [sql for sql in reserve_sql.sql if "from hold" in sql]   # no reap scan
    assert not [sql for sql in settle_sql.sql if sql.startswith("select") and "from hold" in sql]
    [claim] = [sql for sql in settle_sql.sql if sql.startswith("delete from hold")]
    assert "returning" in claim
    assert await _drift(org_id) == 0


async def test_auto_topup_sees_the_balance_the_reserve_left(money_db, monkeypatch):
    org_id, caller = await _payer(
        "money-path-autotopup", autotopup_enabled=True, autotopup_consented_at=utcnow_naive())
    seen: list[tuple[int, int | None]] = []
    monkeypatch.setattr(billing, "maybe_schedule_autotopup",
                        lambda org, balance_micro=None: seen.append((org.id, balance_micro)))
    await call_reserve._platform_reserve(_call(org_id, 300), caller, call_ref="autotopup")
    async with session_maker() as db:
        assert seen == [(org_id, await ledger.balance_of(db, org_id))] == [(org_id, 9_700)]


async def _stale_hold(org_id: int, call_id: str) -> None:
    async with session_maker() as db:
        await ledger.reserve(db, org_id, "test.money_path", 100, call_id=call_id)
        await db.execute(update(Hold).where(Hold.id == call_id).values(
            created_at=utcnow_naive() - timedelta(seconds=ledger.hold_ttl_s() + 5)))
        await db.commit()


async def _entries(call_id: str) -> list[str]:
    async with session_maker() as db:
        return sorted((await db.execute(select(LedgerEntry.kind).where(
            LedgerEntry.call_id == call_id))).scalars().all())


async def test_reserve_leaves_stale_holds_to_the_sweep_when_lazy_reap_is_off(money_db, monkeypatch):
    org_id, caller = await _payer("money-path-sweep")
    await _stale_hold(org_id, "stale")
    monkeypatch.setattr(get_settings(), "reserve_reaps_stale_holds", False)
    mk = _call(org_id)
    await call_reserve._platform_reserve(mk, caller, call_ref="fresh")
    assert await _entries("stale") == ["reserve"]

    result = await hold_sweep.reap_stale_holds()
    assert (result.seen, result.released_micro, result.complete) == (1, 100, True)
    assert (await hold_sweep.reap_stale_holds()).seen == 0
    assert await _entries("stale") == ["release", "reserve"]
    assert await _entries("fresh") == ["reserve"]   # a live hold is not the sweep's
    await call_settle._platform_settle(mk, 200, observed_override=70)
    assert await _drift(org_id) == 0
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 10_000 - 70


async def test_reserve_still_reaps_lazily_by_default(money_db):
    assert get_settings().reserve_reaps_stale_holds is True
    org_id, caller = await _payer("money-path-lazy")
    await _stale_hold(org_id, "stale")
    await call_reserve._platform_reserve(_call(org_id), caller, call_ref="fresh")
    assert await _entries("stale") == ["release", "reserve"]
    assert (await hold_sweep.reap_stale_holds()).seen == 0


async def test_sweep_continues_in_batches(money_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "reserve_reaps_stale_holds", False)
    org_id, _caller = await _payer("money-path-batches")
    for n in range(5):
        await _stale_hold(org_id, f"stale-{n}")
    partial = await hold_sweep.reap_stale_holds(batch_size=2, max_batches=2)
    assert (partial.seen, partial.complete) == (4, False)
    rest = await hold_sweep.reap_stale_holds(batch_size=2, max_batches=2)
    assert (rest.seen, rest.complete) == (1, True)
    assert await _drift(org_id) == 0


async def _aged_allowance_hold(org_id: int, age_s: int) -> None:
    async with session_maker() as db:
        db.add(Hold(id=allowance.allowance_id(org_id), org_id=org_id, amount_micro=1_000,
                    allowance=True, created_at=utcnow_naive() - timedelta(seconds=age_s)))
        await db.execute(update(Org).where(Org.id == org_id).values(
            balance_micro=Org.balance_micro - 1_000))
        await db.commit()


async def test_aged_allowance_holds_never_stall_the_sweep(money_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "reserve_reaps_stale_holds", False)
    allowance_orgs = [(await _payer(f"money-path-allowance-{n}"))[0] for n in range(5)]
    for allowance_org in allowance_orgs:
        await _aged_allowance_hold(allowance_org, ledger.hold_ttl_s() + 3_600)
    org_id, _caller = await _payer("money-path-behind-allowance")
    await _stale_hold(org_id, "stale-behind")

    result = await hold_sweep.reap_stale_holds(batch_size=2, max_batches=5)
    assert (result.seen, result.released_micro, result.complete) == (1, 100, True)
    assert await _entries("stale-behind") == ["release", "reserve"]
    async with session_maker() as db:
        for allowance_org in allowance_orgs:
            assert await db.get(Hold, allowance.allowance_id(allowance_org)) is not None
    for every_org in (*allowance_orgs, org_id):
        assert await _drift(every_org) == 0


async def test_sweep_moves_past_holds_it_could_not_release(money_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "reserve_reaps_stale_holds", False)
    org_id, _caller = await _payer("money-path-cursor")
    for n in range(5):
        await _stale_hold(org_id, f"stale-{n}")
    await _stale_hold(org_id, "stale-last")
    stuck = {f"stale-{n}" for n in range(5)}
    release = ledger.release

    async def release_or_skip(db, call_id, **kwargs):
        return 0 if call_id in stuck else await release(db, call_id, **kwargs)

    monkeypatch.setattr(ledger, "release", release_or_skip)
    result = await hold_sweep.reap_stale_holds(batch_size=2, max_batches=5)
    assert (result.seen, result.released_micro, result.complete) == (6, 100, True)
    assert await _entries("stale-last") == ["release", "reserve"]
    assert await _drift(org_id) == 0


async def _autotopup_with_card(clients: AsyncClient, threshold_micro: int) -> int:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.autotopup_enabled = True
        org.autotopup_consented_at = utcnow_naive()
        org.stripe_customer_id = "cus_test"
        org.stripe_default_pm = "pm_test"
        org.autotopup_threshold_micro = threshold_micro
        await db.commit()
    return org_id


@pytest.mark.parametrize("threshold_micro", [1, 10**12], ids=["balance-healthy", "refill-due"])
async def test_metered_call_with_auto_top_up_and_a_saved_card(
        clients: AsyncClient, platform_on, monkeypatch, threshold_micro):
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_not_a_key")
    started: list[int] = []

    async def no_stripe(org_id: int) -> None:
        started.append(org_id)
        billing._scheduled.discard(org_id)

    monkeypatch.setattr(billing, "_run_autotopup", no_stripe)
    org_id = await _autotopup_with_card(clients, threshold_micro)
    response = await clients.get(f"/call/{EP}?aweme_id=autotopup-{threshold_micro}")
    await asyncio.sleep(0)
    assert response.status_code == 200, response.text
    assert started == ([] if threshold_micro == 1 else [org_id])
    async with session_maker() as db:
        assert (await db.execute(select(Hold).where(Hold.org_id == org_id))).first() is None
    assert await _drift(org_id) == 0


async def test_a_failing_auto_top_up_check_never_fails_the_committed_call(
        clients: AsyncClient, platform_on, monkeypatch):
    org_id = await _autotopup_with_card(clients, 10**12)

    def broken(*_args, **_kwargs):
        raise RuntimeError("auto top-up check failed")

    monkeypatch.setattr(billing, "maybe_schedule_autotopup", broken)
    response = await clients.get(f"/call/{EP}?aweme_id=autotopup-broken")
    assert response.status_code == 200, response.text
    async with session_maker() as db:
        assert (await db.execute(select(Hold).where(Hold.org_id == org_id))).first() is None
    assert await _drift(org_id) == 0


class _HeldConnections:
    """Pooled connections checked out right now, by the treg frames that took them."""

    def __init__(self) -> None:
        self.held: dict[int, list[str]] = {}
        self._engine = session_maker.kw["bind"].sync_engine

    def _out(self, _dbapi, record, _proxy) -> None:
        parent = greenlet.getcurrent().parent
        frames = traceback.extract_stack(parent.gr_frame if parent else None)
        self.held[id(record)] = [f.name for f in frames if "/treg/" in f.filename][-3:]

    def _in(self, _dbapi, record) -> None:
        self.held.pop(id(record), None)

    def __enter__(self):
        event.listen(self._engine, "checkout", self._out)
        event.listen(self._engine, "checkin", self._in)
        return self

    def __exit__(self, *_exc) -> None:
        event.remove(self._engine, "checkout", self._out)
        event.remove(self._engine, "checkin", self._in)


VENDOR_PATH = "dataforseo/serp/google/organic/live/advanced"


def _quiet_side_writers(monkeypatch) -> None:
    """The fake upstream, and no key last-used writer: it takes its own connection on its own task."""
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'[{"ok":1}]'))
    monkeypatch.setattr(managed_keys, "touch", lambda *args, **kwargs: None)


@pytest.mark.parametrize("rest", [EP_DFS, VENDOR_PATH], ids=["catalog-id", "vendor-path"])
async def test_no_connection_is_held_while_the_body_uploads(
        clients: AsyncClient, platform_on, monkeypatch, rest):
    _quiet_side_writers(monkeypatch)
    await audit.drain()
    stalled, resume = asyncio.Event(), asyncio.Event()

    async def slow_body():
        yield b'[{"keyword": "coffee", "location_code": 2840'
        stalled.set()
        await resume.wait()
        yield b', "language_code": "en"}]'

    with _HeldConnections() as pool:
        task = asyncio.create_task(clients.post(
            f"/call/{rest}", content=slow_body(), headers={"content-type": "application/json"}))
        try:
            await asyncio.wait_for(stalled.wait(), 5)
            await asyncio.sleep(0.3)
            held_while_uploading = list(pool.held.values())
        finally:
            resume.set()
            response = await asyncio.wait_for(task, 10)
    assert held_while_uploading == []
    assert response.status_code == 200, response.text


async def test_a_vendor_path_call_refreshes_the_views_before_any_connection(
        clients: AsyncClient, platform_on, monkeypatch):
    _quiet_side_writers(monkeypatch)
    await audit.drain()
    loaded = call_service.load_marketplace_views
    held_at_refresh: list[int] = []
    with _HeldConnections() as pool:
        async def counted() -> None:
            held_at_refresh.append(len(pool.held))
            await loaded()

        monkeypatch.setattr(call_service, "load_marketplace_views", counted)
        response = await clients.post(f"/call/{VENDOR_PATH}", json=[
            {"keyword": "coffee", "location_code": 2840, "language_code": "en"}])
    assert response.status_code == 200, response.text
    assert held_at_refresh == [0]
