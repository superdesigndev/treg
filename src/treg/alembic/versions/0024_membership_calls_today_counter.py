"""membership.calls_today / calls_today_day — the per-user daily cap takes a slot, not a count

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-06

`governance/usage.enforce_daily_cap` runs on every call and run a capped member makes. Until now it
counted the member's `callrecord` and `runrecord` rows since midnight. Revision 0023's
`(org_id, user_email, created_at)` index turned that into an index-only scan, but today's pages are
not yet all-visible until autovacuum reaches them, so every row still fetched the heap — measured
on prod 2026-09-06 for the busiest member (110k rows today):

    Index Only Scan ... Heap Fetches: 110,166   Buffers: shared hit=81,738 read=14,436   2,777 ms

O(this member's rows today), per call, on an api-pool connection. Same disease as `spent_today`
(revision 0022), same cure: a counter on the row the gate already has. `take_daily_slot` is one
conditional UPDATE — the WHERE is the check and the SET is the count, so the cap is exact under
concurrency and a refused call is not counted. Only capped members are counted; the roster and
`/usage/me` keep reading the journal (`count_today`), and `seed_counter` copies today's journal
onto the row when a cap is first set, so a member capped mid-day does not start from zero.

**Backfill, in its own autocommit step, capped members only.** 123 of 9,239 memberships carry a
cap; each backfill row is one journal count over 0023's index — the heaviest ~3 s, the rest
milliseconds. It runs after the ALTER commits, so the ACCESS EXCLUSIVE lock on `membership`
(read on every request) lasts milliseconds, not the length of those counts.

Rollback floor: `calls_today` is NOT NULL with a server default kept (the model carries the same
default), so older code still inserts memberships; downgrading drops both columns. The
expand-safety linter counts the autocommit escape as non-additive, hence the contract marker.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True

# `timezone('UTC', now())` because the app writes NAIVE UTC timestamps and dates; a session in
# another zone would draw the day boundary in the wrong place.
_BACKFILL = sa.text("""
    UPDATE membership SET calls_today = x.n, calls_today_day = (timezone('UTC', now()))::date
    FROM (SELECT m.id,
                 (SELECT count(*) FROM callrecord r
                   WHERE r.org_id = m.org_id AND r.user_email = u.email
                     AND r.created_at >= date_trunc('day', timezone('UTC', now())))
               + (SELECT count(*) FROM runrecord r
                   WHERE r.org_id = m.org_id AND r.user_email = u.email
                     AND r.created_at >= date_trunc('day', timezone('UTC', now()))) AS n
            FROM membership m JOIN "user" u ON u.id = m.user_id
           WHERE m.daily_call_cap >= 0) x
    WHERE membership.id = x.id
""")


def upgrade() -> None:
    op.add_column("membership", sa.Column("calls_today", sa.Integer(), nullable=False,
                                          server_default="0"))
    op.add_column("membership", sa.Column("calls_today_day", sa.Date(), nullable=True))
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite deployments are dev databases with no day of history worth carrying over
    with op.get_context().autocommit_block():
        op.get_bind().execute(_BACKFILL)


def downgrade() -> None:
    with op.batch_alter_table("membership") as batch:
        batch.drop_column("calls_today_day")
        batch.drop_column("calls_today")
