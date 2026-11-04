"""archive snapshots drop the `body_of` foreign key, so deleting a snapshot stops scanning the table

Revision ID: 0094
Revises: 0093
Create Date: 2026-11-04

`archivesnapshot.body_of` referenced `archivesnapshot.id` with no index on `body_of`. Postgres
checks every deleted snapshot for rows still pointing at it, and without that index each check is a
sequential scan of the whole table. On a large archive one erasure batch of a few hundred snapshots
cannot finish inside the erasure's statement cap.

The constraint guards nothing the application relies on: `body_of` only points within one key,
erasure deletes all of a key's snapshots together, the pruner clears bodies but never a surviving
version's carrier, and a read whose carrier is gone finds no body (unavailable, a cache miss).
Dropping it is a catalog change with no table scan; it takes a brief ACCESS EXCLUSIVE lock, and
`env.py`'s session `lock_timeout` makes it fail fast rather than queue behind live calls. A retry
after a lock timeout finds the constraint still present and drops it then.

Rollback floor: older code runs without the constraint, so a code rollback does not need the
downgrade. The downgrade adds it back NOT VALID, which checks only new rows and scans nothing;
validating the existing rows is left to an operator.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0094"
down_revision: str | Sequence[str] | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # drops a constraint; see the rollback floor note

_POSTGRES_FK_NAME = "archivesnapshot_body_of_fkey"
_SQLITE_FK_NAME = "fk_archivesnapshot_body_of_archivesnapshot"
_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot drop a foreign key in place. Alembic's batch mode reflects and rebuilds the
        # table; the naming convention gives its unnamed FK a stable drop target.
        with op.batch_alter_table("archivesnapshot", naming_convention=_NAMING_CONVENTION) as batch:
            batch.drop_constraint(_SQLITE_FK_NAME, type_="foreignkey")
        return
    op.execute(f"ALTER TABLE archivesnapshot DROP CONSTRAINT IF EXISTS {_POSTGRES_FK_NAME}")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("archivesnapshot", naming_convention=_NAMING_CONVENTION) as batch:
            batch.create_foreign_key(_SQLITE_FK_NAME, "archivesnapshot", ["body_of"], ["id"])
        return
    op.create_foreign_key(_POSTGRES_FK_NAME, "archivesnapshot", "archivesnapshot",
                          ["body_of"], ["id"], postgresql_not_valid=True)
