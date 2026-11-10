"""org.archive_opt_out_at: a team's archive opt-out

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-09

One nullable timestamp on `org`: the moment an admin opted the team out of the archive
(docs/context/architecture/archive.md, "Opting out"). NULL is every team today: in the archive.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: str | Sequence[str] | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org", sa.Column("archive_opt_out_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("org") as batch:
        batch.drop_column("archive_opt_out_at")
