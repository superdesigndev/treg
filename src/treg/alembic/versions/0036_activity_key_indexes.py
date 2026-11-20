"""Build partial Activity key indexes without blocking PostgreSQL audit writes.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-14

The Activity query filters by team and key, then takes newest ids. The partial index omits
historical unassigned rows; the id suffix permits a backward scan without sorting a key's
whole history. PostgreSQL must still scan the table, so runtime depends on its size and load.
This separate concurrent revision commits the preceding column additions before the scan.
Interrupted builds are retried by removing only invalid indexes. The rollback floor marker
is pro forma for the autocommit escape; these indexes are additive.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma — see the rollback floor note; the operation is one additive index

_TABLES = ("callrecord", "runrecord")

_LOCK_TIMEOUT = "180s"
_STATEMENT_TIMEOUT = "600s"
_ENV_LOCK_TIMEOUT = "5s"
_ENV_STATEMENT_TIMEOUT = "120s"

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        for table in _TABLES:
            op.create_index(f"ix_{table}_org_key_id", table, ["org_id", "api_key_id", "id"],
                            sqlite_where=sa.text("api_key_id IS NOT NULL"))
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(sa.text(f"SET lock_timeout = '{_LOCK_TIMEOUT}'"))
        bind.execute(sa.text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        try:
            for table in _TABLES:
                name = f"ix_{table}_org_key_id"
                valid = bind.execute(_VALIDITY, {"name": name}).scalar()
                if valid is True:
                    continue
                if valid is False:  # debris from a killed build — unusable, and never repaired
                    op.drop_index(name, table_name=table, postgresql_concurrently=True)
                op.create_index(name, table, ["org_id", "api_key_id", "id"],
                                postgresql_where=sa.text("api_key_id IS NOT NULL"),
                                postgresql_concurrently=True)
        finally:
            bind.execute(sa.text(f"SET lock_timeout = '{_ENV_LOCK_TIMEOUT}'"))
            bind.execute(sa.text(f"SET statement_timeout = '{_ENV_STATEMENT_TIMEOUT}'"))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_org_key_id", table_name=table)
