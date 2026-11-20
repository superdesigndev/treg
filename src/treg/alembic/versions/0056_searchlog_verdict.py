"""searchlog: the verdict a judged search ended on

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-01

A v2 answer's verdict (strong | closest | name | none | keyword) was only implied by the row: a
`none` by an empty `shown`, strong and closest both by owner `judged`. The report stratifies
conversion by it, so the verdict is its own column, with the reason behind a none or a keyword
fallback after a colon (`none:gap`, `keyword:not_task`). Nullable, no default: v1 rows and rows
written before read as unknown.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | Sequence[str] | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("searchlog", sa.Column("verdict", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("searchlog") as batch:
        batch.drop_column("verdict")
