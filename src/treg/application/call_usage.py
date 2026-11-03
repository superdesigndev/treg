"""Read the daily call-usage rollup for the dashboard reports (`application/call_usage_rollup.py`).

A report is the folded day rows plus the team's audit rows after the cursor, read in ONE statement
so both halves see the same snapshot: the worker moves the cursor and the counts in one
transaction, so a row is either folded or after the cursor, never both and never neither. The rows
after the cursor are found over `(org_id, id)` and are only the last few minutes of a team's calls
while the cron runs; if it stops, that tail grows until the cron resumes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, cast, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import CallRecord, CallUsageCursor, CallUsageDay
from .call_usage_rollup import CURSOR_ID

UsageRow = tuple[str, str, str, str, str, int, datetime]   # day, email, kind, tool, client, calls, last_at


async def cursor(db: AsyncSession) -> CallUsageCursor | None:
    """None when the worker has never run: the reports then aggregate `callrecord` themselves."""
    return await db.get(CallUsageCursor, CURSOR_ID)


async def rows(db: AsyncSession, org_id: int, since: datetime) -> list[UsageRow]:
    """One team's usage from the UTC day of `since` on: folded day rows, then the unfolded tail
    grouped the same way."""
    after = select(CallUsageCursor.cursor_id).where(CallUsageCursor.id == CURSOR_ID).scalar_subquery()
    day = cast(func.date(CallRecord.created_at), String)
    folded = select(CallUsageDay.day, CallUsageDay.user_email, CallUsageDay.kind, CallUsageDay.tool_name,
                    CallUsageDay.client, CallUsageDay.calls, CallUsageDay.last_at).where(
        CallUsageDay.org_id == org_id, CallUsageDay.day >= since.strftime("%Y-%m-%d"))
    tail = select(day, CallRecord.user_email, CallRecord.kind, CallRecord.tool_name, CallRecord.client,
                  func.count().label("calls"), func.max(CallRecord.created_at)).where(
        CallRecord.org_id == org_id, CallRecord.id > after, CallRecord.created_at >= since,
    ).group_by(day, CallRecord.user_email, CallRecord.kind, CallRecord.tool_name, CallRecord.client)
    result = await db.execute(union_all(folded, tail))
    return [(str(d), email, kind, tool, client or "", int(n), last_at)
            for d, email, kind, tool, client, n, last_at in result.all()]
