"""searchlog + searchmiss: what the job-first find (find_engine v2) records

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-30

`searchlog` gains the engine that answered a web find (v1 | v2; shadow mode writes one row for
each) and v2's own readings: the judge's platform choice and its confidence, the name probability,
recall and embedding times, the embedding error, and every unit the judge read as [kind, id, p].
`searchmiss` gains why a find came back empty (gap | not_task | judge_off | scope) and which engine
served it (shadow mode files a miss for the served engine only). All nullable, no
defaults: metadata-only on Postgres, and rows written before read as unknown.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | Sequence[str] | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEARCHLOG = (
    ("engine", sa.String()),
    ("platform_choice", sa.String()),
    ("platform_conf", sa.Float()),
    ("name_p", sa.Float()),
    ("recall_ms", sa.Integer()),
    ("embed_ms", sa.Integer()),
    ("embed_error", sa.String()),
    ("units", sa.JSON()),
)


def upgrade() -> None:
    for name, type_ in _SEARCHLOG:
        op.add_column("searchlog", sa.Column(name, type_, nullable=True))
    op.add_column("searchmiss", sa.Column("reason", sa.String(), nullable=True))
    op.add_column("searchmiss", sa.Column("engine", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("searchmiss") as batch:
        batch.drop_column("engine")
        batch.drop_column("reason")
    with op.batch_alter_table("searchlog") as batch:
        for name, _ in reversed(_SEARCHLOG):
            batch.drop_column(name)
