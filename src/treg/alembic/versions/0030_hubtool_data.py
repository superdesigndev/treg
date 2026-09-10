"""hubtool.data — the maker's uploaded CSV (docs/HUB-DECISIONS.md round 1 q6)

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-10

One nullable text column: a metadata-only ALTER on Postgres.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("hubtool", sa.Column("data", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("hubtool") as batch:
        batch.drop_column("data")
