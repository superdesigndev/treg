"""Read the daily call-usage rollup for the dashboard reports (`application/call_usage_rollup.py`).

A report is the folded day rows plus the team's audit rows after the cursor, read in ONE statement
so both halves see the same snapshot: the worker moves the cursor and the counts in one
transaction, so a row is either folded or after the cursor, never both and never neither. The rows
after the cursor are found over `(org_id, id)` and are only the last few minutes of a team's calls
while the cron runs; if it stops, that tail grows until the cron resumes.

Postgres plans the cursor subquery's value as unknown and estimates the team's whole history, so
on a busy team it spends far longer compiling the statement (JIT) than running it. JIT is off for
the read's transaction. A constant bound taken from an earlier read cannot replace the subquery:
rebuilding the rollup moves the cursor back, and such a bound would then drop the rows between.

The tail is bounded by its UTC day, the expression it groups by, never by `created_at` itself.
Postgres estimates today's rows from statistics that end before today, so a `created_at` bound
looks like one row and wins the `(org_id, created_at)` index; on a busy team that reads every row
of its day to keep the few after the cursor. Without it the scan runs over `(org_id, id)`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, cast, func, select, text, union_all
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
    first_day = since.strftime("%Y-%m-%d")
    folded = select(CallUsageDay.day, CallUsageDay.user_email, CallUsageDay.kind, CallUsageDay.tool_name,
                    CallUsageDay.client, CallUsageDay.calls, CallUsageDay.last_at).where(
        CallUsageDay.org_id == org_id, CallUsageDay.day >= first_day)
    tail = select(day, CallRecord.user_email, CallRecord.kind, CallRecord.tool_name, CallRecord.client,
                  func.count().label("calls"), func.max(CallRecord.created_at)).where(
        CallRecord.org_id == org_id, CallRecord.id > after, day >= first_day,
    ).group_by(day, CallRecord.user_email, CallRecord.kind, CallRecord.tool_name, CallRecord.client)
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SET LOCAL jit = off"))
    result = await db.execute(union_all(folded, tail))
    return [(str(d), email, kind, tool, client or "", int(n), last_at)
            for d, email, kind, tool, client, n, last_at in result.all()]
