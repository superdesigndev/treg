"""hubtool.check_result — the check run's verdict on a version (docs/HUB-DECISIONS.md round 2 q10)

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-09

One nullable JSON column: a metadata-only ALTER on Postgres.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hubtool", sa.Column("check_result", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("hubtool") as batch:
        batch.drop_column("check_result")
