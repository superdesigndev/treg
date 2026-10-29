"""agent payments: the payer team of a wallet address and the payment proofs it presented

Revision ID: 0091
Revises: 0090
Create Date: 2026-10-30

Additive: two new small tables, nothing else touched. Both are written only by
`application/agent_payments.py`, behind `TREG_AGENT_PAYMENTS_ENABLED`.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0091"
down_revision: str | Sequence[str] | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agentpayer",
        sa.Column("address", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("address"),
        sa.UniqueConstraint("org_id"),
    )
    op.create_table(
        "agentpayment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proof_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("protocol", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("network", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("payer_address", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("reference", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agentpayment_proof_id", "agentpayment", ["proof_id"], unique=True)
    op.create_index("ix_agentpayment_payer_address", "agentpayment", ["payer_address"])
    op.create_index("ix_agentpayment_org_id", "agentpayment", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_agentpayment_org_id", table_name="agentpayment")
    op.drop_index("ix_agentpayment_payer_address", table_name="agentpayment")
    op.drop_index("ix_agentpayment_proof_id", table_name="agentpayment")
    op.drop_table("agentpayment")
    op.drop_table("agentpayer")
