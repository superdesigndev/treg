"""hubrun.output — the run's answer, for the caller's run page (docs/HUB-DECISIONS.md round 5 q8)

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-10

One nullable JSON column: a metadata-only ALTER on Postgres.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | Sequence[str] | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hubrun", sa.Column("output", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("hubrun") as batch:
        batch.drop_column("output")
