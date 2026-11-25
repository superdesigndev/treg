"""callrecord.served_call_ref - which child attempt's answer a routed call returned

Revision ID: 0127
Revises: 0126
Create Date: 2026-11-25

One nullable text column, no default, no index, no foreign key: a catalog-only ALTER on Postgres,
no table rewrite, even on the hot audit table. Set only on a routed parent row: the `call_ref` of
the child whose answer the caller received. A reader starts from the parent row by id and reads
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


def upgrade() -> None:
    op.add_column("callrecord", sa.Column("served_call_ref", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("callrecord") as batch:
        batch.drop_column("served_call_ref")
