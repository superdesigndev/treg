"""A team's allowance: money set aside once for many calls, and the charges drawn against it.

The allowance is ONE `Hold` row per team, marked `allowance`. It is opened and topped up by
`reserve` entries and closed by a `release` entry, so the balance invariant counts it like any
other hold. A call that spends from it records a `CallCharge`; folding applies recorded charges
to the credit blocks and writes each call's `settle` entry. Nothing here commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...models import CallCharge
from . import with_margin


class Charge(NamedTuple):
    """What one finished call cost. `actual_micro` is the raw provider cost; margin is applied here."""

    call_id: str
    org_id: int
    endpoint_id: str
    actual_micro: int
    meta: dict | None = None


async def record_charges(db: AsyncSession, charges: Sequence[Charge]) -> int:
    """Queue finished calls' costs in the caller-owned transaction; return how many were new.

    One statement for the whole batch. A `call_id` already queued is skipped, so recording a call
    twice charges it once.
    """
    if not charges:
        return 0
    margin = get_settings().platform_margin
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    rows = {charge.call_id: {
        "call_id": charge.call_id, "org_id": charge.org_id, "endpoint_id": charge.endpoint_id or "",
        "amount_micro": max(0, with_margin(int(charge.actual_micro))),
        "meta": {**(charge.meta or {}), "observed_micro": int(charge.actual_micro), "margin": margin},
    } for charge in charges}
    result = await db.execute(
        insert(CallCharge).values(list(rows.values()))
        .on_conflict_do_nothing(index_elements=[CallCharge.call_id]))
    return int(result.rowcount)
