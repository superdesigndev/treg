"""Queue mutable endpoint/day evidence for bounded catalog recounts.

Revision ID: 0074
Revises: 0073

Additive structure only. Legacy day buckets remain unreconciled until the catalog worker
recounts them; the migration never reads the audit table or changes task/money state.
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "endpointstatdirty",
        sa.Column("endpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("day", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("retry_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("endpoint_id", "day"),
    )
    op.create_index("ix_endpointstatdirty_retry_at_endpoint_id_day", "endpointstatdirty",
                    ["retry_at", "endpoint_id", "day"])
    op.add_column("endpointdaystat", sa.Column(
        "terminal_reconciled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("endpointdaystat") as batch:
        batch.drop_column("terminal_reconciled")
    op.drop_table("endpointstatdirty")
