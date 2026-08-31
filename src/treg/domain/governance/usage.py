"""Per-member usage policy shared by call and run surfaces."""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import CallRecord, RunRecord
from ...timeutil import utcnow_naive
from ..identity.access import Caller


class UsagePolicyError(Exception):
    """A member exhausted their configured daily usage allowance."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _day_start_utc() -> datetime:
    """Midnight (00:00) of the current UTC day, naive — matches how *Record.created_at is stored."""
    return utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)


async def count_today(db: AsyncSession, org_id: int | None, user_email: str) -> int:
    """How many usage events this user has produced in this org since midnight UTC: proxy calls +
    local-run grants (both `CallRecord`) plus server runs (`RunRecord`). Two indexed COUNTs."""
    since = _day_start_utc()
    calls = (await db.execute(select(func.count()).select_from(CallRecord).where(
        CallRecord.org_id == org_id, CallRecord.user_email == user_email, CallRecord.created_at >= since,
    ))).scalar_one()
    runs = (await db.execute(select(func.count()).select_from(RunRecord).where(
        RunRecord.org_id == org_id, RunRecord.user_email == user_email, RunRecord.created_at >= since,
    ))).scalar_one()
    return calls + runs


async def enforce_daily_cap(caller: Caller, db: AsyncSession, *, sandbox: bool) -> None:
    """Refuse a call/run once the caller has used their per-user daily cap for this org. `-1` (the
    default) = unlimited, so unmetered members pay ZERO extra queries. The sandbox has its own limiter
    and is exempt. Soft by design: the count reads best-effort `CallRecord`s, so under heavy load it
    can lag slightly and fail OPEN (a few extra slip through) — never closed. See docs/USAGE-METERING-PLAN.md."""
    cap = caller.membership.daily_call_cap
    if cap < 0 or sandbox:
        return
    used = await count_today(db, caller.org_id, caller.email)
    if used >= cap:
        raise UsagePolicyError(
            f"daily usage limit reached ({used}/{cap}) — ask an admin to raise your cap")
