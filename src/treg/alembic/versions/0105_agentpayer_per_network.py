"""agent payments: one payer team per (network, address)

Revision ID: 0105
Revises: 0104
Create Date: 2026-11-16

`agentpayer` gains `network` in its primary key. The table is read whole, rebuilt and refilled:
it holds one small row per paying wallet and is written only while `TREG_AGENT_PAYMENTS_ENABLED`
is on. An existing row takes the network of that wallet's earliest payment (`unknown` if it has
none). Downgrade keeps one row per address, the earliest.

Rollback floor: code older than this revision writes `agentpayer` without `network` and reads it
by address alone, so roll back below it only with `TREG_AGENT_PAYMENTS_ENABLED` off (the only state
in which that code writes the table) or after downgrading this revision.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "0105"
down_revision: str | Sequence[str] | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # the table is rebuilt; see the rollback floor note


_payer = sa.table("agentpayer", sa.column("network", sa.String()), sa.column("address", sa.String()),
                  sa.column("org_id", sa.Integer()), sa.column("created_at", sa.DateTime()))
_payment = sa.table("agentpayment", sa.column("id", sa.Integer()),
                    sa.column("payer_address", sa.String()), sa.column("network", sa.String()),
                    sa.column("created_at", sa.DateTime()))


def _payers() -> list[dict]:
    query = sa.select(_payer.c.address, _payer.c.org_id, _payer.c.created_at).order_by(
        _payer.c.created_at)
    return [dict(row._mapping) for row in op.get_bind().execute(query)]


def upgrade() -> None:
    payers = _payers()
    first: dict[str, str] = {}
    for address, network in op.get_bind().execute(
            sa.select(_payment.c.payer_address, _payment.c.network)
            .order_by(_payment.c.created_at, _payment.c.id)):
        first.setdefault(address, network)
    op.drop_table("agentpayer")
    table = op.create_table(
        "agentpayer",
        sa.Column("network", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("address", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("network", "address"),
        sa.UniqueConstraint("org_id"),
    )
    if payers:
        op.bulk_insert(table, [{**row, "network": first.get(row["address"], "unknown")}
                               for row in payers])


def downgrade() -> None:
    rows = _payers()
    op.drop_table("agentpayer")
    table = op.create_table(
        "agentpayer",
        sa.Column("address", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("address"),
        sa.UniqueConstraint("org_id"),
    )
    kept: dict[str, dict] = {}
    for row in rows:
        kept.setdefault(row["address"], row)
    if kept:
        op.bulk_insert(table, list(kept.values()))
