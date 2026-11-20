"""signuppromoclaim: the signup gift is claimed once per inbox, not once per address

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-13

Gmail delivers `a.b+x@gmail.com` and `ab@googlemail.com` to one mailbox, and most providers
deliver `+tag` subaddresses to the base address, so one inbox could verify any number of users
and collect a gift for each. The claim now also inserts the inbox into this table, whose primary
key refuses a second claim (docs/context/architecture/money.md, "Signup credit eligibility").

The backfill seeds every inbox whose user already consumed eligibility (verified, no longer
available). That includes legacy users who verified later without a gift: a conservative
superset, so no alias of an existing account can claim. The normalization is frozen here
rather than imported, so this revision keeps meaning what it meant when it ran.

The operations are additive, but reading users for the backfill makes this a rollback floor:
downgrading drops the claims and reopens a gift to every alias of an inbox that already had one.
"""
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | Sequence[str] | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True

_DOTLESS_DOMAINS = {"gmail.com": "gmail.com", "googlemail.com": "gmail.com"}


def _inbox(email: str) -> str:
    local, _, domain = email.strip().lower().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in _DOTLESS_DOMAINS:
        local, domain = local.replace(".", ""), _DOTLESS_DOMAINS[domain]
    return f"{local}@{domain}"


def upgrade() -> None:
    claim = op.create_table(
        "signuppromoclaim",
        sa.Column("inbox", sa.String(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    users = op.get_bind().execute(sa.text(
        'SELECT id, email FROM "user" WHERE email_verified_at IS NOT NULL '
        "AND signup_promo_available = false ORDER BY id"
    )).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    first: dict[str, int] = {}
    for user_id, email in users:
        first.setdefault(_inbox(email), user_id)
    op.bulk_insert(claim, [
        {"inbox": inbox, "user_id": user_id, "created_at": now} for inbox, user_id in first.items()
    ])


def downgrade() -> None:
    op.drop_table("signuppromoclaim")
