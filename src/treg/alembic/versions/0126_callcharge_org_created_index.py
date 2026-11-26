"""callcharge (org_id, created_at) in place of (org_id) - a team's oldest queued charge is one
index probe

Revision ID: 0126
Revises: 0125
Create Date: 2026-11-25

The allowance's fold pass lists the teams of its share by the age of their oldest queued charge.
With only `ix_callcharge_org_id` that age is a read of every row the team has queued, and a long
queue is exactly when the pass runs most often. Leading with `org_id`, the new index still serves
every lookup the old one did (a team's rows, the next team number), so the old one goes, as
revision 0091 did for other prefix-covered indexes.

The queue is short-lived and small, so the build runs here, CONCURRENTLY in an autocommit block
with a bounded lock wait; INVALID debris from a killed build is dropped first.

Rollback floor: 0126. A process of an earlier build would run against the new index (every
query it makes leads with `org_id`), but an earlier build's deploy cannot start: its
`python -m treg upgrade` refuses a schema newer than itself. To go back to one process folding
every team, set `TREG_MONEY_ALLOWANCE_FOLD_SHARDS=1` on this build. To withdraw the code, ship a
forward release that reverts it and keeps this revision. `downgrade` restores the old index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0126"
down_revision: str | Sequence[str] | None = "0125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # drops the prefix-covered index - see the rollback floor note

_TABLE = "callcharge"
NAME = "ix_callcharge_org_id_created_at"
OLD = "ix_callcharge_org_id"
BUILD_LOCK_TIMEOUT = "60s"

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = :name")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(NAME, _TABLE, ["org_id", "created_at"])
        op.drop_index(OLD, table_name=_TABLE)
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        settings = {name: bind.execute(sa.text(f"SHOW {name}")).scalar_one()
                    for name in ("lock_timeout", "statement_timeout")}
        try:
            bind.execute(sa.text(f"SET lock_timeout = '{BUILD_LOCK_TIMEOUT}'"))
            bind.execute(sa.text("SET statement_timeout = 0"))   # this build only; restored below
            valid = bind.execute(_VALIDITY, {"name": NAME}).scalar()
            if valid is False:  # debris from a killed build - unusable, and never repaired
                op.drop_index(NAME, table_name=_TABLE, postgresql_concurrently=True)
            if valid is not True:
                op.create_index(NAME, _TABLE, ["org_id", "created_at"],
                                postgresql_concurrently=True)
            op.drop_index(OLD, table_name=_TABLE, postgresql_concurrently=True,
                          if_exists=True)
        finally:
            for name, value in settings.items():
                bind.execute(sa.text("SELECT set_config(:name, :value, false)"),
                             {"name": name, "value": value})


def downgrade() -> None:
    op.create_index(OLD, _TABLE, ["org_id"])
    op.drop_index(NAME, table_name=_TABLE)
