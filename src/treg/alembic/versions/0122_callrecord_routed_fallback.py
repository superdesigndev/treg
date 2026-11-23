"""callrecord.routed_fallback and endpointdaystat fallback counters

Revision ID: 0122
Revises: 0121
Create Date: 2026-11-22

Router fallback attempts receive requests earlier candidates already missed or failed on, so
counting them in `ok_rate` makes providers later in a plan look broken. Adding nullable
`callrecord.routed_fallback` and additive `endpointdaystat.fallback_ok` / `fallback_bad`
counters lets stats and rankings measure `ok_rate` on first-choice calls without backfilling
the audit table; older rows infer fallback from `:r<n>` (`n >= 1`).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0122"
down_revision: str | Sequence[str] | None = "0121"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("callrecord", sa.Column("routed_fallback", sa.Boolean(), nullable=True))
    op.add_column("endpointdaystat", sa.Column(
        "fallback_ok", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("endpointdaystat", sa.Column(
        "fallback_bad", sa.Integer(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    with op.batch_alter_table("endpointdaystat") as batch:
        batch.drop_column("fallback_bad")
        batch.drop_column("fallback_ok")
    with op.batch_alter_table("callrecord") as batch:
        batch.drop_column("routed_fallback")
