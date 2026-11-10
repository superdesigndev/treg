"""showcase leaderboard (applications, history, weekly activity, votes) and team/user pictures

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-15

Additive: six new tables and two nullable columns. `showcaseapp` holds teams' applications to the
/showcase leaderboard and their review state; `showcaseappevent` its append-only history;
`showcaseappkey` the API keys an app's activity comes from; `showcaseweek` one row per source (a team or
one key) per UTC week and `showcasecursor` the last call counted into them,
both written by `treg-worker showcase activity`, which reads only new `callrecord` rows on the existing
(org_id, id) and (org_id, api_key_id, id) indexes; `showcasevote` one upvote per person per app.
`org.avatar_key` and `user.avatar_key` name a picture in the media bucket, NULL for initials.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | Sequence[str] | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("org", sa.Column("avatar_key", sa.String(), nullable=True))
    op.add_column("user", sa.Column("avatar_key", sa.String(), nullable=True))
    op.create_table(
        "showcaseapp",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("tagline", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("maker_name", sa.String(), nullable=False),
        sa.Column("x_handle", sa.String(), nullable=False),
        sa.Column("launch_post_url", sa.String(), nullable=False),
        sa.Column("icon_key", sa.String(), nullable=True),
        sa.Column("shot_keys", sa.JSON(), nullable=False),
        sa.Column("show_calls", sa.Boolean(), nullable=False),
        sa.Column("show_streak", sa.Boolean(), nullable=False),
        sa.Column("show_providers", sa.Boolean(), nullable=False),
        sa.Column("show_categories", sa.Boolean(), nullable=False),
        sa.Column("pending", sa.JSON(), nullable=True),
        sa.Column("reject_reason", sa.String(), nullable=False),
        sa.Column("submitted_by", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("usage_source", sa.String(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'hidden')", name="ck_showcaseapp_status"),
        sa.CheckConstraint("version >= 0", name="ck_showcaseapp_version"),
        sa.CheckConstraint("usage_source IN ('team', 'keys')", name="ck_showcaseapp_usage_source"),
    )
    op.create_index("ix_showcaseapp_org_id", "showcaseapp", ["org_id"])
    op.create_index("ix_showcaseapp_status_id", "showcaseapp", ["status", "id"])
    op.create_table(
        "showcaseappevent",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("app_id", sa.Integer(), sa.ForeignKey("showcaseapp.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("note", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("app_id", "version", name="uq_showcaseappevent_version"),
        sa.CheckConstraint("version > 0", name="ck_showcaseappevent_version"),
        sa.CheckConstraint("source IN ('web', 'api', 'admin')", name="ck_showcaseappevent_source"),
    )
    op.create_table(
        "showcaseappkey",
        sa.Column("api_key_id", sa.Integer(), sa.ForeignKey("apikey.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("app_id", sa.Integer(), sa.ForeignKey("showcaseapp.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_showcaseappkey_app_id", "showcaseappkey", ["app_id"])
    op.create_table(
        "showcaseweek",
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("api_key_id", sa.Integer(), primary_key=True),
        sa.Column("week_start", sa.String(), primary_key=True),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("active_days", sa.Integer(), nullable=False),
        sa.Column("providers", sa.JSON(), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("calls >= 0", name="ck_showcaseweek_calls"),
        sa.CheckConstraint("active_days BETWEEN 0 AND 127", name="ck_showcaseweek_active_days"),
    )
    op.create_table(
        "showcasecursor",
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("api_key_id", sa.Integer(), primary_key=True),
        sa.Column("through_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "showcasevote",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("app_id", sa.Integer(), sa.ForeignKey("showcaseapp.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("app_id", "user_id", name="uq_showcasevote_app_user"),
    )
    op.create_index("ix_showcasevote_app_id", "showcasevote", ["app_id"])
    op.create_index("ix_showcasevote_user_id", "showcasevote", ["user_id"])


def downgrade() -> None:
    op.drop_table("showcasevote")
    op.drop_table("showcasecursor")
    op.drop_table("showcaseappkey")
    op.drop_table("showcaseweek")
    op.drop_table("showcaseappevent")
    op.drop_table("showcaseapp")
    op.drop_column("user", "avatar_key")
    op.drop_column("org", "avatar_key")
