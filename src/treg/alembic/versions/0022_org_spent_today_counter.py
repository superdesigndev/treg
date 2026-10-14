"""org.spent_today_micro / spent_today_day — the daily cap reads a counter, not the journal

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-06

`ledger.spent_today` is the fail-closed per-org daily cap and runs inside EVERY metered call's
reserve transaction on an api-pool connection. Until now it was two aggregates over `ledgerentry`
and `hold` since midnight. Revision 0021 gave the ledger half an `(org_id, created_at)` index and
that fixed light orgs and `/billing`, but not the two orgs that write half the platform's day:
their rows sit on nearly every heap page of the day, so the planner (correctly) keeps walking the
whole day's `created_at` range — measured after 0021 on prod 2026-09-06:

    org 5430  Rows Removed by Filter: 166,760   Buffers: shared hit=394,248   439 ms warm
    org 4645  Rows Removed by Filter: 385,118   Buffers: shared hit=395,506   1,797 ms warm
    cold (day's pages evicted by another scan): 56–171 s, holding an api-pool slot throughout

No index can make "sum of this org's rows today" cheaper than "this org's pages today" — for a
heavy org that IS the day. The counter makes it one primary-key read: `domain/money` folds every
reserve, settle and release into `org.spent_today_micro` inside the UPDATE that already moves the
balance, and `spent_today_day` says which UTC day it belongs to (the first movement of a new day
resets it). `spent_today_from_ledger` keeps the journal view for reconciliation.

**Backfill, in its own autocommit step.** The ALTER takes ACCESS EXCLUSIVE on `org`, the hottest
row-updated table (every reserve); adding a column with a constant default is metadata-only on this
Postgres and holds that lock for milliseconds. The backfill — one range aggregate over today's
ledger and holds — would hold it for seconds if it ran in the same transaction, which is exactly
the 2026-08-15 shape. So the ALTER commits first, then the two UPDATEs run autocommit, taking
only row locks on the orgs that moved money today. Calls that reserve between the backfill and the
new code starting are missed by at most that window; the cap is a blast-radius guard, not a bill.

Rollback floor: `spent_today_micro` is NOT NULL with a server default kept (the model carries the
same default), so older code still inserts orgs; downgrading drops both columns. The expand-safety
linter counts the autocommit escape as non-additive, hence the contract marker.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True

# `timezone('UTC', now())` because the app writes NAIVE UTC timestamps and dates (models._now);
# a session in another zone would otherwise draw the day boundary in the wrong place.
_BACKFILL_SETTLED = sa.text("""
    UPDATE org SET spent_today_micro = x.v, spent_today_day = (timezone('UTC', now()))::date
    FROM (SELECT org_id, -sum(amount_micro) AS v FROM ledgerentry
          WHERE kind = 'settle' AND created_at >= date_trunc('day', timezone('UTC', now()))
          GROUP BY org_id) x
    WHERE org.id = x.org_id
""")
_BACKFILL_HELD = sa.text("""
    UPDATE org SET spent_today_micro = spent_today_micro + x.v,
                   spent_today_day = (timezone('UTC', now()))::date
    FROM (SELECT org_id, sum(amount_micro) AS v FROM hold
          WHERE created_at >= date_trunc('day', timezone('UTC', now()))
          GROUP BY org_id) x
    WHERE org.id = x.org_id
""")


def upgrade() -> None:
    op.add_column("org", sa.Column("spent_today_micro", sa.BigInteger(), nullable=False,
                                   server_default="0"))
    op.add_column("org", sa.Column("spent_today_day", sa.Date(), nullable=True))
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite deployments are dev databases with no day of history worth carrying over
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(_BACKFILL_SETTLED)
        bind.execute(_BACKFILL_HELD)


def downgrade() -> None:
    with op.batch_alter_table("org") as batch:
        batch.drop_column("spent_today_day")
        batch.drop_column("spent_today_micro")
