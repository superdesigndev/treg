"""callreview.code_basis / answer_retry_at / answer_attempts, callrecord.surface, idempotentcall.surface

Revision ID: 0128
Revises: 0127
Create Date: 2026-11-25

A negative review is coded from its reason first and upgraded later with its call's answer
(`application.review_judge`). `callreview.code_basis` says which: NULL or `reason` (from the reason,
an upgrade may follow), `answer` (with the answer), `reason_final` (no answer can be shown: routed,
gone, or the delivery cannot be told). `answer_retry_at` and `answer_attempts` hold the back-off of
an answer that could not be read yet. `callrecord.surface` names the surface a call came through
when it is not `/call/` (`table`), so a judge never reads an archived body the caller received
reshaped. `idempotentcall.surface` (`call` or `table`) keeps an Idempotency-Key from replaying a
call across the two surfaces; a label taken before it (NULL) replays as before and expires within
the idempotency window.

Nullable columns with no default, no index, no foreign key: catalog-only ALTERs on Postgres, no table
rewrite, even on the hot audit table. Rows written before this revision keep NULL.

Rollback: an earlier build's `python -m treg upgrade` refuses a schema newer than itself, so the
code is withdrawn by a forward release that keeps this revision; `downgrade` drops the columns.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0128"
down_revision: str | Sequence[str] | None = "0127"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMNS = (
    ("callreview", "code_basis", sa.String()),
    ("callreview", "answer_retry_at", sa.DateTime()),
    ("callreview", "answer_attempts", sa.Integer()),
    ("callrecord", "surface", sa.String()),
    ("idempotentcall", "surface", sa.String()),
)


def _present(table: str) -> set[str] | None:
    """The columns already there, or None when generating SQL offline (`--sql`)."""
    if op.get_context().as_sql:
        return None
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Each ADD COLUMN commits on its own where DDL is not transactional (SQLite under Alembic): a
    # rerun adds only the columns that are missing.
    for table, name, kind in COLUMNS:
        present = _present(table)
        if present is None or name not in present:
            op.add_column(table, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    # A plain DROP COLUMN per column, never a batch table rebuild; a rerun drops only what is there.
    for table, name, _ in reversed(COLUMNS):
        present = _present(table)
        if present is None or name in present:
            op.drop_column(table, name)
