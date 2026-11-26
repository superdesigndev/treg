"""compareattemptday.spent_micro: the day's second-vendor spend, kept apart from any team's rows

Revision ID: 0134
Revises: 0133
Create Date: 2026-11-26

The comparison run caps a UTC day at `compare_daily_usd` of B's catalog prices. Summing the day's
`callcomparison` rows would forget what a deleted team's comparisons cost (team deletion removes
them), so the spend is counted where the call count already is: on the day's row, which is no
team's, in the same conditional update that takes the attempt.

One BIGINT NOT NULL column with a constant default on a table of one row per day: a catalog-only
ALTER on Postgres, no rewrite. Rows written before this revision read 0.

Rollback: an earlier build's `python -m treg upgrade` refuses a schema newer than itself, so the
code is withdrawn by a forward release that keeps this revision; `downgrade` drops the column.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0134"
down_revision: str | Sequence[str] | None = "0133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _present() -> set[str] | None:
    """The columns already there, or None when generating SQL offline (`--sql`)."""
    if op.get_context().as_sql:
        return None
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("compareattemptday")}


def upgrade() -> None:
    present = _present()
    if present is None or "spent_micro" not in present:
        op.add_column("compareattemptday", sa.Column("spent_micro", sa.BigInteger(), nullable=False,
                                                     server_default="0"))


def downgrade() -> None:
    present = _present()
    if present is None or "spent_micro" in present:
        op.drop_column("compareattemptday", "spent_micro")
