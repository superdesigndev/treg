"""arenainsightstate.resume_cursor - where the next insights cycle starts when the rewind finds nothing

Revision ID: 0129
Revises: 0128
Create Date: 2026-11-26

One nullable integer on a table of one row per catalog version: a catalog-only ALTER. `cursor` is
a cycle's scan progress and may stand at the end of an id span the cycle found empty, past a row
still being committed; `resume_cursor` is the cycle's starting cursor, moved up only to rows the
cycle actually collected, and the next cycle starts there when the rewind finds no recent row.
Rows written before this revision keep NULL, read as their `cursor`.

Rollback: an earlier build's `python -m treg upgrade` refuses a schema newer than itself, so the
code is withdrawn by a forward release that keeps this revision; `downgrade` drops the column.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0129"
down_revision: str | Sequence[str] | None = "0128"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _present() -> bool | None:
    if op.get_context().as_sql:
        return None
    return "resume_cursor" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("arenainsightstate")}


def upgrade() -> None:
    if not _present():
        op.add_column("arenainsightstate", sa.Column("resume_cursor", sa.Integer(), nullable=True))


def downgrade() -> None:
    if _present() is not False:
        op.drop_column("arenainsightstate", "resume_cursor")
