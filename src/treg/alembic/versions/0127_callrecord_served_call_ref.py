"""callrecord.served_call_ref / served_content_hash - which child's answer a routed call returned

Revision ID: 0127
Revises: 0126
Create Date: 2026-11-25

Two nullable text columns, no default, no index, no foreign key: catalog-only ALTERs on Postgres,
no table rewrite, even on the hot audit table. Set only on a routed parent row: the `call_ref` of
the child whose answer the caller received, and the sha256 of the bytes it returned as `raw`, so
a reader can tell whether an archived answer is the delivered one. A reader starts from the parent row by id and reads
the child row by that exact ref through `ix_callrecord_call_ref`, never scanning the team's
history. Rows written before this revision, and every non-routed row, keep NULL.

Rollback: an earlier build's `python -m treg upgrade` refuses a schema newer than itself, so the
code is withdrawn by a forward release that keeps this revision (rows then keep NULL here);
`downgrade` drops the column.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0127"
down_revision: str | Sequence[str] | None = "0126"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMNS = ("served_call_ref", "served_content_hash")


def _present() -> set[str] | None:
    """The columns already there, or None when generating SQL offline (`--sql`), where there is
    no database to look at and the DDL follows from the starting revision alone."""
    if op.get_context().as_sql:
        return None
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("callrecord")}


def upgrade() -> None:
    # Each ADD COLUMN commits on its own where DDL is not transactional (SQLite under Alembic): an
    # interrupted run can leave the first column with the version still 0126. A rerun adds only
    # the columns that are missing.
    present = _present()
    for name in COLUMNS:
        if present is None or name not in present:
            op.add_column("callrecord", sa.Column(name, sa.String(), nullable=True))


def downgrade() -> None:
    # A plain DROP COLUMN per column (SQLite has one since 3.35), never a batch table rebuild: an
    # interrupted rebuild leaves `_alembic_tmp_callrecord` behind and every rerun fails on it.
    # Like upgrade, a rerun drops only what is still there.
    present = _present()
    for name in reversed(COLUMNS):
        if present is None or name in present:
            op.drop_column("callrecord", name)
