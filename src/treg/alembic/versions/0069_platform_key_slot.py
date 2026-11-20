"""key_slot on rows a treg-owned provider account owns

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-12

A provider can sit behind several treg-owned accounts (`TREG_PLATFORM_ACCOUNTS`). A task, result
or resource created on one account exists only there, so its later polls, fetches and edits must
use the same account. `key_slot` names it by the operator's label (`primary` for the deployment's
own key), never by a credential. NULL is every row created before pools: the primary account.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: str | Sequence[str] | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("asynctaskrecord", "asyncresourcerecord", "providerresource")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("key_slot", sa.String(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("key_slot")
