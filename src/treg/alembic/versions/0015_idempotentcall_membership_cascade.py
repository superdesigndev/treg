"""idempotent calls leave with the membership that owns their replay key

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-03

Revoking a member or agent used to fail on Postgres after that caller made a metered request with an
Idempotency-Key: idempotentcall still referenced the membership being deleted. The cached response
has no valid reader after token revocation, so make it a true membership-owned row.

This replaces an existing foreign key and is therefore a rollback floor. Older code remains
compatible with the cascading constraint, but rolling the schema itself back changes deletion
semantics and must not overlap membership revocations.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True

_SQLITE_FK_NAME = "fk_idempotentcall_membership_id_membership"
_POSTGRES_FK_NAME = "idempotentcall_membership_id_fkey"
_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _replace_membership_fk(*, ondelete: str | None) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        # SQLite cannot alter a foreign key in place. Alembic's batch mode reflects and rebuilds the
        # table; the naming convention gives its previously unnamed FK a stable drop target.
        with op.batch_alter_table(
            "idempotentcall", naming_convention=_NAMING_CONVENTION
        ) as batch:
            batch.drop_constraint(_SQLITE_FK_NAME, type_="foreignkey")
            batch.create_foreign_key(
                _SQLITE_FK_NAME,
                "membership",
                ["membership_id"],
                ["id"],
                ondelete=ondelete,
            )
        return

    op.drop_constraint(_POSTGRES_FK_NAME, "idempotentcall", type_="foreignkey")
    op.create_foreign_key(
        _POSTGRES_FK_NAME,
        "idempotentcall",
        "membership",
        ["membership_id"],
        ["id"],
        ondelete=ondelete,
    )


def upgrade() -> None:
    _replace_membership_fk(ondelete="CASCADE")


def downgrade() -> None:
    _replace_membership_fk(ondelete=None)
