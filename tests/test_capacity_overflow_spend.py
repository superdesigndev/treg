"""Atomic daily accounting for overflow aggregator spend."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from treg.infra.db import session_maker
from treg.domain.capacity import overflow_spend as spend_ledger
from treg.domain.capacity.overflow_spend import add_in_transaction
from treg.models import OverflowSpend


async def test_add_in_transaction_does_not_commit_for_its_caller(clients):
    async with session_maker() as db:
        row = await add_in_transaction(db, "orthogonal", 2_000, 100, day="2026-08-28")
        row = await add_in_transaction(db, "orthogonal", 3_000, -50, day="2026-08-28")
        assert (row.calls, row.cost_micro, row.delta_micro) == (2, 5_000, 50)
        await db.rollback()

    async with session_maker() as db:
        assert await db.get(OverflowSpend, ("orthogonal", "2026-08-28")) is None


async def test_concurrent_first_adds_are_atomic(clients, monkeypatch):
    original_get = AsyncSession.get
    both_read_missing = asyncio.Event()
    reads = 0
    gate_reads = True

    async def get_after_both_read(self, entity, ident, *args, **kwargs):
        nonlocal reads
        row = await original_get(self, entity, ident, *args, **kwargs)
        if (
            gate_reads and entity is OverflowSpend
            and ident == ("orthogonal", "2026-08-28")
        ):
            reads += 1
            if reads == 2:
                both_read_missing.set()
            await asyncio.wait_for(both_read_missing.wait(), timeout=2)
        return row

    monkeypatch.setattr(AsyncSession, "get", get_after_both_read)

    async def add(cost_micro: int, delta_micro: int) -> None:
        async with session_maker() as db:
            await add_in_transaction(
                db, "orthogonal", cost_micro, delta_micro, day="2026-08-28",
            )
            await db.commit()

    results = await asyncio.gather(add(2_000, 100), add(3_000, -50), return_exceptions=True)
    gate_reads = False

    assert results == [None, None]
    async with session_maker() as db:
        row = await db.get(OverflowSpend, ("orthogonal", "2026-08-28"))
    assert row is not None
    assert (row.calls, row.cost_micro, row.delta_micro) == (2, 5_000, 50)


async def test_concurrent_budget_reservations_allow_at_most_one_call_at_the_cap(clients):
    async def reserve() -> bool:
        async with session_maker() as db:
            row = await spend_ledger.reserve_in_transaction(
                db, "orthogonal", 3_000, 4_000, day="2026-08-28",
            )
            await db.commit()
            return row is not None

    admitted = await asyncio.gather(reserve(), reserve())

    assert sum(admitted) == 1
    async with session_maker() as db:
        row = await db.get(OverflowSpend, ("orthogonal", "2026-08-28"))
    assert row is not None
    assert (row.calls, row.cost_micro) == (0, 3_000)


async def test_first_budget_reservation_larger_than_the_cap_is_rejected(clients):
    async with session_maker() as db:
        row = await spend_ledger.reserve_in_transaction(
            db, "orthogonal", 5_000, 4_000, day="2026-08-28",
        )
        await db.commit()

    assert row is None
    async with session_maker() as db:
        assert await db.get(OverflowSpend, ("orthogonal", "2026-08-28")) is None


async def test_budget_reservation_does_not_commit_for_its_caller(clients):
    async with session_maker() as db:
        row = await spend_ledger.reserve_in_transaction(
            db, "orthogonal", 3_000, 4_000, day="2026-08-28",
        )
        assert row is not None
        await db.rollback()

    async with session_maker() as db:
        assert await db.get(OverflowSpend, ("orthogonal", "2026-08-28")) is None
