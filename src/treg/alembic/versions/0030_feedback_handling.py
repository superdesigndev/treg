"""Internal feedback handling state and append-only history.

Revision ID: 0030
Revises: 0029
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedbackhandling",
        sa.Column("feedback_id", sa.Integer(), sa.ForeignKey("feedback.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("assignee", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'investigating', 'resolved', 'rejected')", name="ck_feedbackhandling_status"),
        sa.CheckConstraint("version >= 0", name="ck_feedbackhandling_version"),
    )
    op.create_index("ix_feedbackhandling_status_feedback_id", "feedbackhandling", ["status", "feedback_id"])
    op.create_table(
        "feedbackhandlingevent",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("feedback_id", sa.Integer(), sa.ForeignKey("feedback.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(), nullable=False),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column("from_assignee", sa.String(), nullable=True),
        sa.Column("to_assignee", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=False),
        sa.Column("links", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("feedback_id", "version", name="uq_feedbackhandlingevent_version"),
        sa.CheckConstraint("version > 0", name="ck_feedbackhandlingevent_version"),
        sa.CheckConstraint("from_status IN ('open', 'investigating', 'resolved', 'rejected') AND to_status IN ('open', 'investigating', 'resolved', 'rejected')", name="ck_feedbackhandlingevent_status"),
        sa.CheckConstraint("source IN ('web', 'api')", name="ck_feedbackhandlingevent_source"),
        sa.CheckConstraint("to_status NOT IN ('resolved', 'rejected') OR to_status = from_status OR length(trim(note)) > 0", name="ck_feedbackhandlingevent_closure_note"),
    )


def downgrade() -> None:
    op.drop_table("feedbackhandlingevent")
    op.drop_table("feedbackhandling")
