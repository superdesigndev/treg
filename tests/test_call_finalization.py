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
