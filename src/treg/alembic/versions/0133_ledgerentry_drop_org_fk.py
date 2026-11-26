"""ledgerentry drops its `org_id` foreign key, so its inserts stop share-locking the team's org row

Revision ID: 0133
Revises: 0132
Create Date: 2026-11-26

Every insert into a table with a foreign key to `org` runs PostgreSQL's referential check, which
takes `FOR KEY SHARE` on the team's `org` row. Balance writes hold `FOR NO KEY UPDATE` on the same
row. The two do not block each other, but each new locker of a row that is already locked makes
PostgreSQL write a new MultiXact naming every current locker, so a busy team's concurrent inserts
and balance updates write MultiXacts that grow with the number of lockers. On PostgreSQL 16 the
MultiXact SLRUs are a few fixed pages behind one lock each, shared by the whole server: once they
thrash, unrelated inserts wait on `MultiXactOffsetSLRU` with no row-lock blocker.

`ledgerentry`: two or more rows per call and one of the two largest tables; the drop is still a
catalog change. Every writer updates the team row, claims a hold or queued charge the deletion
also deletes, or inserts a `creditblock`, whose foreign key to `org` stays.
The constraint is NO ACTION. Team deletion removes these rows itself, in `ORG_SCOPED_MODELS` and
again in `ORG_ROWS_WITHOUT_FOREIGN_KEY` after the team row, which takes a row committed while the
deletion waited. The `org_id` indexes stay.

Dropping it scans nothing, but it takes ACCESS EXCLUSIVE on `ledgerentry` and on `org`, and while it
waits for them every later query on either table queues behind it. `SET LOCAL lock_timeout`
bounds that wait for this revision alone; a timeout fails the upgrade with the earlier revisions
applied, and `python -m treg upgrade` resumes here.

Rollback floor: older code runs unchanged without the constraint, so the code can go back without
the downgrade (a forward commit that keeps this file). A deploy of a build that does not ship this
revision must downgrade first, from a build that does: `python -m treg upgrade` refuses a stamp
its build does not know (docs/context/ops/deploy.md, "Rolling back past a revision"). The
downgrade adds the constraint back NOT VALID, which checks only new rows and scans nothing.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0133"
down_revision: str | Sequence[str] | None = "0132"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # drops a constraint; see the rollback floor note

_POSTGRES_FK_NAME = "ledgerentry_org_id_fkey"
_SQLITE_FK_NAME = "fk_ledgerentry_org_id_org"
_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot drop a foreign key in place. Alembic's batch mode reflects and rebuilds the
        # table; the naming convention gives its unnamed FK a stable drop target.
        with op.batch_alter_table("ledgerentry", naming_convention=_NAMING_CONVENTION) as batch:
            batch.drop_constraint(_SQLITE_FK_NAME, type_="foreignkey")
        return
    op.execute("SET LOCAL lock_timeout = '1s'")
    op.execute(f"ALTER TABLE ledgerentry DROP CONSTRAINT IF EXISTS {_POSTGRES_FK_NAME}")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("ledgerentry", naming_convention=_NAMING_CONVENTION) as batch:
            batch.create_foreign_key(_SQLITE_FK_NAME, "org", ["org_id"], ["id"])
        return
    op.execute("SET LOCAL lock_timeout = '1s'")
    op.create_foreign_key(_POSTGRES_FK_NAME, "ledgerentry", "org", ["org_id"], ["id"],
                          postgresql_not_valid=True)
