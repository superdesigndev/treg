"""media: `object_key` names a body kept in the object store; `body` becomes nullable

Revision ID: 0092
Revises: 0091
Create Date: 2026-10-31

A row with `object_key` set has no `body`: its bytes live in the archive's bucket
(`media_body_write = "r2"`). Rows without it keep their bytes in `body`, so rows written before
the switch are served as they are and leave by TTL; nothing is backfilled.

Dropping NOT NULL is metadata only on Postgres (no rewrite, no scan). Rollback floor: once rows with
`object_key` exist, code older than this revision would serve them as empty bodies, so roll back
only with `media_body_write = "db"` and after those rows have expired.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0092"
down_revision: str | Sequence[str] | None = "0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # body loses NOT NULL; see the rollback floor note


def upgrade() -> None:
    with op.batch_alter_table("media") as batch:
        batch.add_column(sa.Column("object_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch.alter_column("body", existing_type=sa.LargeBinary(), nullable=True)


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM media WHERE body IS NULL"))
    with op.batch_alter_table("media") as batch:
        batch.alter_column("body", existing_type=sa.LargeBinary(), nullable=False)
        batch.drop_column("object_key")
