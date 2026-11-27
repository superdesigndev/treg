"""callcomparison.jev_choice / jev_orders / jev_version / jev_judged_at / jev_retry_at / jev_attempts

Revision ID: 0139
Revises: 0138
Create Date: 2026-11-27

Jev judges a stored comparison blind, apart from the team's agent (`application.compare_judge`).
Its verdict lives beside the agent's and never in `choice` or `status`: `jev_choice` (a, b, tie,
both_bad, or split when its two orders disagreed), `jev_orders` (its verdict with A shown first,
then with B shown first), `jev_version` (the rubric it judged under) and `jev_judged_at`.
`jev_retry_at` and `jev_attempts` hold the back-off of a pair it could not judge yet.

Nullable columns with no default, no index, no foreign key: catalog-only ALTERs on Postgres, no table
rewrite. Rows written before this revision keep NULL (not judged).

Rollback: an earlier build's `python -m treg upgrade` refuses a schema newer than itself, so the
code is withdrawn by a forward release that keeps this revision; `downgrade` drops the columns.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0139"
down_revision: str | Sequence[str] | None = "0138"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMNS = (
    ("jev_choice", sa.String()),
    ("jev_orders", sa.String()),
    ("jev_version", sa.String()),
    ("jev_judged_at", sa.DateTime()),
    ("jev_retry_at", sa.DateTime()),
    ("jev_attempts", sa.Integer()),
)


def _present() -> set[str] | None:
    """The columns already there, or None when generating SQL offline (`--sql`)."""
    if op.get_context().as_sql:
        return None
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("callcomparison")}


def upgrade() -> None:
    # Each ADD COLUMN commits on its own where DDL is not transactional (SQLite under Alembic): a
    # rerun adds only the columns that are missing.
    for name, kind in COLUMNS:
        present = _present()
        if present is None or name not in present:
            op.add_column("callcomparison", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    # A plain DROP COLUMN per column, never a batch table rebuild; a rerun drops only what is there.
    for name, _ in reversed(COLUMNS):
        present = _present()
        if present is None or name in present:
            op.drop_column("callcomparison", name)
