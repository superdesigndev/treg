"""Application-owned settlement and exactly-once finalization."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient
from sqlalchemy import select

from treg.domain import money as ledger
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry, TagSpend


async def _funded_call(clients: AsyncClient, call_id: str) -> tuple[int, int, MarketplaceCall]:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        await ledger.grant(db, org_id, amount_micro=100_000, kind="finalizer_test", once=False)
        await db.commit()
        before = await ledger.balance_of(db, org_id)
        await ledger.reserve(
            db, org_id, "provider.operation", 1_000,
            call_id=call_id, tags={"customer": "one"},
        )
    mk = MarketplaceCall(
        tool=None,
        upstream="https://provider.test/resource",
        consumed=set(),
        endpoint_id="provider.operation",
        provider="provider",
        tier="platform",
        cost_type="per_call",
        estimate_micro=1_000,
        call_id=call_id,
    )
    return org_id, before, mk


async def test_success_and_cancellation_race_has_one_terminal_money_effect(
    clients: AsyncClient,
) -> None:
    call_id = "double-finalizer-race"
    org_id, before, mk = await _funded_call(clients, call_id)
    await asyncio.gather(
        call_settle._platform_settle(mk, 200),
        call_settle._finish_cancelled_call(None, mk, call_id),
    )

    async with session_maker() as db:
        holds = (await db.execute(select(Hold).where(Hold.id == call_id))).scalars().all()
        terminal = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id,
            LedgerEntry.kind.in_(("settle", "release")),
        ))).scalars().all()
        tags = (await db.execute(select(TagSpend).where(
            TagSpend.hold_id == call_id))).scalars().all()
        after = await ledger.balance_of(db, org_id)

    assert holds == []
    assert len(terminal) == 1
    if terminal[0].kind == "settle":
        assert after == before + terminal[0].amount_micro
        assert len(tags) == 1 and tags[0].settled is True
    else:
        assert after == before
        assert tags == []


async def test_persistent_settlement_failure_keeps_response_path_non_raising(
    clients: AsyncClient, monkeypatch,
) -> None:
    call_id = "settlement-outage"
    org_id, before, mk = await _funded_call(clients, call_id)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ledger, "settle_in_transaction", unavailable)
    charged, observed = await call_settle._platform_settle(mk, 200)

    assert (charged, observed) == (0, None)
    async with session_maker() as db:
        assert await db.get(Hold, call_id) is not None
        assert await ledger.balance_of(db, org_id) < before


_TIMED_OUT = (b'{"error":{"type":"run-failed","message":"Actor run did not succeed '
              b'(run ID: K7Ap5kqvhC8N8bQLN, status: TIMED-OUT)."}}')


async def test_timed_out_apify_platform_run_settles_at_the_hold(clients: AsyncClient) -> None:
    """The run billed up to the caller's cap and its rows stay readable by run id."""
    call_id = "apify-timed-out"
    org_id, before, mk = await _funded_call(clients, call_id)
    mk.provider, mk.cost_type = "apify", "per_result"
    mk.settlement_basis = {"when": "response", "amount": {"kind": "observed"}, "fallback_micro": 1_000}
    charged, observed = await call_settle._platform_settle(mk, 400, _TIMED_OUT)
    assert observed == 1_000 and charged >= 1_000
    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == before - charged


def test_only_a_timed_out_platform_apify_run_is_billed_on_400() -> None:
    mk = MarketplaceCall(tool=None, upstream="", consumed=set(), provider="apify",
                         endpoint_id="apify.weibo.search.posts",
                         tier="platform", cost_type="per_result", estimate_micro=1_000)
    assert call_settle._apify_run_timed_out(mk, 400, _TIMED_OUT)
    failed = _TIMED_OUT.replace(b"TIMED-OUT", b"FAILED")
    assert not call_settle._apify_run_timed_out(mk, 400, failed)
    assert not call_settle._apify_run_timed_out(mk, 408, _TIMED_OUT)
    assert not call_settle._apify_run_timed_out(mk, 400, b"not json")
    assert not call_settle._apify_run_timed_out(mk, 400, b"[]")
    mk.tier = "credential"
    assert not call_settle._apify_run_timed_out(mk, 400, _TIMED_OUT)
