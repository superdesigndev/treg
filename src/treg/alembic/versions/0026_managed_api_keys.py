"""managed API-key controls, audit, Activity attribution, and legacy-token backfill

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-04

This additive revision is a rollback floor because new code can record key disable and revoke state
that old code cannot enforce. Rolling the schema back would remove that security state.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True


def upgrade() -> None:
    op.create_table(
        "apikey",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("membership_id", sa.Integer(), nullable=True),
        sa.Column("identity_label", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("safe_prefix", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("key_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("replacement_key_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["membership_id"], ["membership.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_apikey_org_id", "apikey", ["org_id"])
    op.create_index("ix_apikey_membership_id", "apikey", ["membership_id"])
    op.create_index("ix_apikey_identity_label", "apikey", ["identity_label"])
    op.create_index("ix_apikey_kind", "apikey", ["kind"])
    op.create_index("ix_apikey_state", "apikey", ["state"])
    op.create_index("ix_apikey_key_hash", "apikey", ["key_hash"], unique=True)
    op.create_index(
        "uq_apikey_default_membership", "apikey", ["membership_id"], unique=True,
        postgresql_where=sa.text("kind = 'default_human'"),
        sqlite_where=sa.text("kind = 'default_human'"),
    )
    op.create_index(
        "uq_apikey_current_agent_membership", "apikey", ["membership_id"], unique=True,
        postgresql_where=sa.text("kind = 'agent' AND state IN ('active', 'disabled')"),
        sqlite_where=sa.text("kind = 'agent' AND state IN ('active', 'disabled')"),
    )

    op.create_table(
        "apikeyevent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.Integer(), nullable=False),
        sa.Column("actor_email", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("identity_label", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("action", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["key_id"], ["apikey.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_apikeyevent_org_id", "apikeyevent", ["org_id"])
    op.create_index("ix_apikeyevent_key_id", "apikeyevent", ["key_id"])
    op.create_index("ix_apikeyevent_actor_email", "apikeyevent", ["actor_email"])
    op.create_index("ix_apikeyevent_action", "apikeyevent", ["action"])

    for table in ("callrecord", "runrecord"):
        op.add_column(table, sa.Column("api_key_id", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("api_key_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        op.add_column(table, sa.Column("api_key_prefix", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        op.create_index(f"ix_{table}_api_key_id", table, ["api_key_id"])

    # One signed default-key control per real human membership. Machine identities cannot sign in.
    bind = op.get_bind()
    bind.execute(sa.text("""
        INSERT INTO apikey
            (org_id, membership_id, identity_label, kind, name, safe_prefix, key_hash, state,
             created_by, created_at)
        SELECT m.org_id, m.id, u.email, 'default_human', 'Default key', NULL, NULL, 'active',
               u.email, m.created_at
          FROM membership AS m
          JOIN "user" AS u ON u.id = m.user_id
         WHERE u.demo = false
           AND u.email NOT LIKE '%@agents.treg.local'
           AND u.email NOT LIKE '%@public-demo.treg.local'
           AND NOT EXISTS (
               SELECT 1 FROM apikey AS k
                WHERE k.membership_id = m.id AND k.kind = 'default_human'
           )
    """))
    # Copy stored hashes. Existing secrets keep working and no plaintext is needed.
    bind.execute(sa.text("""
        INSERT INTO apikey
            (org_id, membership_id, identity_label, kind, name, safe_prefix, key_hash, state,
             created_by, created_at)
        SELECT m.org_id, m.id, u.email,
               CASE WHEN u.email LIKE '%@agents.treg.local' THEN 'agent' ELSE 'legacy_human' END,
               CASE WHEN u.email LIKE '%@agents.treg.local' THEN 'Agent key' ELSE 'Legacy key' END,
               NULL, m.token_hash, 'active',
               CASE WHEN m.created_by <> '' THEN m.created_by ELSE u.email END,
               m.created_at
          FROM membership AS m
          JOIN "user" AS u ON u.id = m.user_id
         WHERE m.token_hash IS NOT NULL AND m.token_hash <> ''
           AND NOT EXISTS (SELECT 1 FROM apikey AS k WHERE k.key_hash = m.token_hash)
    """))


def downgrade() -> None:
    for table in ("runrecord", "callrecord"):
        op.drop_index(f"ix_{table}_api_key_id", table_name=table)
        op.drop_column(table, "api_key_prefix")
        op.drop_column(table, "api_key_name")
        op.drop_column(table, "api_key_id")
    op.drop_index("ix_apikeyevent_action", table_name="apikeyevent")
    op.drop_index("ix_apikeyevent_actor_email", table_name="apikeyevent")
    op.drop_index("ix_apikeyevent_key_id", table_name="apikeyevent")
    op.drop_index("ix_apikeyevent_org_id", table_name="apikeyevent")
    op.drop_table("apikeyevent")
    op.drop_index("uq_apikey_current_agent_membership", table_name="apikey")
    op.drop_index("uq_apikey_default_membership", table_name="apikey")
    op.drop_index("ix_apikey_key_hash", table_name="apikey")
    op.drop_index("ix_apikey_state", table_name="apikey")
    op.drop_index("ix_apikey_kind", table_name="apikey")
    op.drop_index("ix_apikey_identity_label", table_name="apikey")
    op.drop_index("ix_apikey_membership_id", table_name="apikey")
    op.drop_index("ix_apikey_org_id", table_name="apikey")
    op.drop_table("apikey")
