"""archiveendpointstat — the report's running totals, backfilled from the live archive

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-03

The panel's report walked 434k keys + 505k snapshots on every cold load (measured 9.9s + 8.0s +
14.8s per query on prod). From this revision the recorder maintains one totals row per endpoint
in the same transaction as each snapshot, and the report reads ~50 tiny rows. The backfill runs
the old aggregates ONCE, here, where a preDeploy step may take its time — those two execute()
statements are the rollback floor: a build older than this revision does not know the totals
table and cannot re-derive rows recorded after it, so do not downgrade past here on a live
database (the table would simply be dropped and rebuilt stale by a re-upgrade). A partial index over
refresh snapshots keeps the report's "refreshes today" count off the big table too.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # backfill executes — see the rollback floor note above


def upgrade() -> None:
    op.create_table(
        "archiveendpointstat",
        sa.Column("endpoint_id", sa.String(), primary_key=True),
        sa.Column("provider", sa.String(), nullable=False, server_default=""),
        sa.Column("policy", sa.String(), nullable=False, server_default="forbidden"),
        sa.Column("keys", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bodies_kept", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kept_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("newest_fetch", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_archivesnapshot_refresh_fetched", "archivesnapshot", ["fetched_at"],
                    postgresql_where=sa.text("origin = 'refresh'"),
                    sqlite_where=sa.text("origin = 'refresh'"))
    # Backfill: the exact aggregates the report used to compute per load, run once. Key-side
    # counters first, then the snapshot-side counters merged in.
    op.execute("""
        INSERT INTO archiveendpointstat
            (endpoint_id, provider, policy, keys, stable, changed,
             snapshots, bodies_kept, kept_bytes, newest_fetch)
        SELECT k.endpoint_id, max(k.provider), max(k.policy), count(k.id),
               coalesce(sum(k.stable_seen), 0), coalesce(sum(k.change_seen), 0),
               0, 0, 0, max(k.fetched_at)
        FROM archivekey k GROUP BY k.endpoint_id
    """)
    op.execute("""
        UPDATE archiveendpointstat SET
            snapshots   = agg.snaps,
            bodies_kept = agg.kept,
            kept_bytes  = agg.bytes
        FROM (
            SELECT k.endpoint_id AS endpoint_id, count(s.id) AS snaps,
                   count(s.body) AS kept,
                   coalesce(sum(CASE WHEN s.body IS NOT NULL THEN s.size_bytes END), 0) AS bytes
            FROM archivekey k JOIN archivesnapshot s ON s.key_id = k.id
            GROUP BY k.endpoint_id
        ) AS agg
        WHERE archiveendpointstat.endpoint_id = agg.endpoint_id
    """)


def downgrade() -> None:
    op.drop_index("ix_archivesnapshot_refresh_fetched", table_name="archivesnapshot")
    op.drop_table("archiveendpointstat")
