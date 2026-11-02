"""Internal derived reading of each feedback report.

Revision ID: 0093
Revises: 0092
"""
from alembic import op
import sqlalchemy as sa

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedbackdiagnosis",
        sa.Column("feedback_id", sa.Integer(), sa.ForeignKey("feedback.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("endpoint_id", sa.String(), nullable=True),
        sa.Column("endpoint_source", sa.String(), nullable=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("probabilities", sa.JSON(), nullable=False),
        sa.Column("classifier", sa.String(), nullable=False),
        sa.Column("diagnosed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("endpoint_source IN ('call', 'message')", name="ck_feedbackdiagnosis_endpoint_source"),
        sa.CheckConstraint("(endpoint_id IS NULL) = (endpoint_source IS NULL)", name="ck_feedbackdiagnosis_endpoint_pair"),
    )


def downgrade() -> None:
    op.drop_table("feedbackdiagnosis")
