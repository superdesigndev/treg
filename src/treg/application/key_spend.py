"""Billed spend per API key: the ledger's settle entries, attributed to a key through the audit row.

The money comes only from `LedgerEntry` (kind `settle`), never from `CallRecord.cost_*`, because the
ledger is the authority and audit rows may be shed. `CallRecord` supplies only the key: its
`call_ref` is the ledger `call_id` of a metered call. A routed child (`<ref>:r1`) or an overflow
hold (`<ref>:overflow`) settles under a suffixed id whose own audit row may not exist; those fall
back to the parent ref before the part after the first colon. A charge whose call has no keyed
audit row (shed, written before key tracking, or never keyed) is reported as not attributed, so
the per-key lines and that one line always add up to the ledger total.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..models import CallRecord, LedgerEntry

# A metered async task settles up to its 24-hour worker deadline after the call's audit row was
# written, so the key lookup reaches this far further back than the money window.
_AUDIT_SLACK = timedelta(days=2)


def window_start(now: datetime, days: int) -> datetime:
    """Today (UTC) plus the `days - 1` before it: the same window as `/orgs/{id}/usage`."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)


async def _attributed(db: AsyncSession, org_id: int, since: datetime, key_id: int | None = None):
    """Yield `(key_id, day, call, micro)` per settled call since `since`; `key_id` is None when no
    keyed audit row names the call. Two indexed range reads: settle entries over `(org_id,
    created_at)`, and the org's keyed audit rows over the same pair. The join happens here so
    neither query depends on a dialect's string functions for the parent-ref fallback.

    Given `key_id`, only that key's audit rows are read, so every other charge comes back as None.
    """
    day = func.date(LedgerEntry.created_at)  # 'YYYY-MM-DD' on sqlite, a date on Postgres; str() both
    per_call = (await db.execute(
        select(LedgerEntry.call_id, day, func.coalesce(func.sum(-LedgerEntry.amount_micro), 0)).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle", LedgerEntry.created_at >= since,
        ).group_by(LedgerEntry.call_id, day)
    )).all()
    if not per_call:
        return
    records = select(CallRecord.call_ref, CallRecord.api_key_id).where(
        CallRecord.org_id == org_id, CallRecord.created_at >= since - _AUDIT_SLACK,
        CallRecord.api_key_id.is_not(None), CallRecord.call_ref != "",
    )
    if key_id is not None:
        records = records.where(CallRecord.api_key_id == key_id)
    key_of: dict[str, int] = {}
    for call_ref, owner in (await db.execute(records.order_by(CallRecord.id))).all():
        key_of.setdefault(call_ref, int(owner))  # several rows can share a ref; the first one keys it
    for call_id, settled_day, micro in per_call:
        ref = call_id or ""
        parent = ref.split(":", 1)[0]
        owner = key_of.get(ref)
        if owner is None:
            owner = key_of.get(parent)
        yield owner, str(settled_day), parent, int(micro)


async def spend_by_api_key(db: AsyncSession, org_id: int, since: datetime) -> dict:
    """`{"keys": {key_id: {"spend_micro", "calls"}}, "unattributed": {"spend_micro", "calls"}}`.

    `calls` counts billed calls: distinct parent refs with a settle in the window.
    """
    spend: dict[int | None, int] = {}
    calls: dict[int | None, set[str]] = {}
    async for key_id, _day, parent, micro in _attributed(db, org_id, since):
        spend[key_id] = spend.get(key_id, 0) + micro
        calls.setdefault(key_id, set()).add(parent)  # an overflow hold is part of its parent's call
    return {
        "keys": {key_id: _line(micro, len(calls[key_id]))
                 for key_id, micro in spend.items() if key_id is not None},
        "unattributed": _line(spend.get(None, 0), len(calls.get(None, ()))),
    }


async def daily_spend_of_key(db: AsyncSession, org_id: int, key_id: int, since: datetime) -> list[dict]:
    """One key's billed spend per UTC day since `since`, oldest first; days without a charge are
    absent (the caller draws the empty days)."""
    spend: dict[str, int] = {}
    calls: dict[str, set[str]] = {}
    async for owner, day, parent, micro in _attributed(db, org_id, since, key_id):
        if owner != key_id:
            continue
        spend[day] = spend.get(day, 0) + micro
        calls.setdefault(day, set()).add(parent)
    return [{"day": day, **_line(spend[day], len(calls[day]))} for day in sorted(spend)]


def _line(spend_micro: int, calls: int) -> dict:
    return {"spend_micro": spend_micro, "calls": calls}
