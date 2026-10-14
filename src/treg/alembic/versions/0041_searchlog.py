"""The discovery experiment's per-search record: both rankers' pages and the one served."""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "searchlog",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("query", sa.String(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("user_email", sa.String(), nullable=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("arm", sa.String(), nullable=False),
        sa.Column("baseline_ids", sa.JSON(), nullable=True),
        sa.Column("judged", sa.JSON(), nullable=True),
        sa.Column("shown", sa.JSON(), nullable=True),
        sa.Column("baseline_total", sa.Integer(), nullable=False),
        sa.Column("differs", sa.Boolean(), nullable=False),
        sa.Column("judge_ms", sa.Integer(), nullable=True),
        sa.Column("judge_tokens_in", sa.Integer(), nullable=True),
        sa.Column("judge_tokens_out", sa.Integer(), nullable=True),
        sa.Column("judge_error", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_searchlog_created_at", "searchlog", ["created_at"])
    op.create_index("ix_searchlog_source", "searchlog", ["source"])
    op.create_index("ix_searchlog_org_id", "searchlog", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_searchlog_org_id", table_name="searchlog")
    op.drop_index("ix_searchlog_source", table_name="searchlog")
    op.drop_index("ix_searchlog_created_at", table_name="searchlog")
    op.drop_table("searchlog")
