"""call usage: a daily rollup of the audit table's rows per member, kind, tool and client, and
the cursor that fills it

Revision ID: 0088
Revises: 0087
Create Date: 2026-10-27

Additive: two new small tables, nothing else touched. `callrecord` is only read by the worker that
fills them (`treg-worker usage rollup`), by primary key.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0088"
down_revision: str | Sequence[str] | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "callusageday",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("day", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_email", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("tool_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("client", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("last_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("org_id", "day", "user_email", "kind", "tool_name", "client"),
    )
    op.create_table(
        "callusagecursor",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cursor_id", sa.Integer(), nullable=False),
        sa.Column("watermark", sa.DateTime(), nullable=True),
        sa.Column("history_from", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("caught_up_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("callusagecursor")
    op.drop_table("callusageday")
