"""Per-member usage policy shared by call and run surfaces."""

import logging
from datetime import datetime

from sqlalchemy import case, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import CallRecord, Membership, RunRecord
from ...timeutil import utcnow_naive
from ..identity.access import Caller

log = logging.getLogger("treg.usage")


class UsagePolicyError(Exception):
    """A member exhausted their configured daily usage allowance."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _day_start_utc() -> datetime:
    """Midnight (00:00) of the current UTC day, naive — matches how *Record.created_at is stored."""
    return utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)


async def count_today(db: AsyncSession, org_id: int | None, user_email: str) -> int:
    """How many usage events this user has produced in this org since midnight UTC, from the JOURNAL:
    proxy calls + local-run grants (both `CallRecord`) plus server runs (`RunRecord`).

    NOT the cap's number and NOT on the call path. This is what the roster, `/usage/me` and the
    usage report show, and what `seed_counter` copies when a cap is first set; the gate itself
    reads `Membership.calls_today` (`take_daily_slot`). Two range COUNTs over
    `(org_id, user_email, created_at)` - O(this member's rows today), which for a member making
    100k calls a day was 2.8 s per call when the gate still ran it (2026-09-06).
    """
    since = _day_start_utc()
    calls = (await db.execute(select(func.count()).select_from(CallRecord).where(
        CallRecord.org_id == org_id, CallRecord.user_email == user_email, CallRecord.created_at >= since,
    ))).scalar_one()
    runs = (await db.execute(select(func.count()).select_from(RunRecord).where(
        RunRecord.org_id == org_id, RunRecord.user_email == user_email, RunRecord.created_at >= since,
    ))).scalar_one()
    return calls + runs


async def take_daily_slot(db: AsyncSession, membership_id: int, cap: int) -> bool:
    """Admit one usage event against the member's cap, or say no. ONE conditional UPDATE.

    The WHERE is the check and the SET is the count, so N concurrent calls cannot each read a
    compliant figure and together overshoot - the same idiom as `money.reserve`. The first event
    of a new UTC day resets the counter to 1 instead of adding to yesterday. A refused event is not
    counted: the counter is "admitted today", so a capped member hammering the gate reads exactly
    `cap`, not a runaway number. Does not commit; the caller's transaction owns it.
    """
    today = utcnow_naive().date()
    # "Used today" is the counter only if it belongs to today; a stale or never-set day is 0. The
    # same expression gates and counts, so a cap of 0 refuses even the day's first event.
    used_today = case((Membership.calls_today_day == today, Membership.calls_today), else_=0)
    result = await db.execute(
        update(Membership)
        .where(Membership.id == membership_id, used_today < cap)
        .values(calls_today=used_today + 1, calls_today_day=today)
    )
    return result.rowcount == 1


async def seed_counter(db: AsyncSession, membership: Membership, user_email: str) -> None:
    """Start the counter from today's journal - called when a cap is set on a member who was
    unlimited until now. Only capped members are counted on the call path, so without this a
    member who already made 50k calls today would get a fresh allowance the moment they were capped.
    One journal count, at cap-setting time, never per call."""
    membership.calls_today = await count_today(db, membership.org_id, user_email)
    membership.calls_today_day = utcnow_naive().date()


async def enforce_daily_cap(caller: Caller, db: AsyncSession, *, sandbox: bool) -> None:
    """Refuse a call/run once the caller has used their per-user daily cap for this org. `-1` (the
    default) = unlimited, so unmetered members pay ZERO extra queries. The sandbox has its own limiter
    and is exempt.

    One conditional UPDATE of the member's row (`take_daily_slot`), exact under concurrency. Fails
    OPEN if that statement cannot run: a cap is a courtesy limit an admin set on a colleague, and
    the database being unavailable is not the colleague's fault - the money gates behind this one
    are the ones that fail closed. See docs/USAGE-METERING-PLAN.md.
    """
    cap = caller.membership.daily_call_cap
    if cap < 0 or sandbox:
        return
    try:
        admitted = await take_daily_slot(db, caller.membership.id, cap)
    except Exception as exc:  # noqa: BLE001 - fail open, see docstring
        log.warning("daily-cap check failed for membership %s: %s", caller.membership.id, exc)
        return
    if not admitted:
        used = (await db.execute(
            select(Membership.calls_today).where(Membership.id == caller.membership.id))).scalar() or 0
        raise UsagePolicyError(
            f"daily usage limit reached ({used}/{cap}) — ask an admin to raise your cap")
