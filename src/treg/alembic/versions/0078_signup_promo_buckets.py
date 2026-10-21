"""Add transactional signup-credit rate buckets and IDNA aliases for permanent inbox claims.

Revision ID: 0078
Revises: 0077

Retain old claim keys for mixed-version readers and add canonical aliases, including claims
whose users were deleted. Normalization is frozen here rather than imported from runtime code.
This is a rollback floor: downgrading removes consumed rate budgets; older writers also omit
canonical aliases. Pause promo grants before rolling back. Added permanent claims are retained.
"""
from alembic import op
import idna
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None
contract = True


def _inbox(email: str) -> str:
    local, _, domain = email.strip().lower().rstrip(".").rpartition("@")
    domain = idna.encode(domain, uts46=True, std3_rules=True).decode("ascii").lower().rstrip(".")
    local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def upgrade() -> None:
    op.create_table(
        "signuppromobucket",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("hits", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_signuppromobucket_expires_at", "signuppromobucket", ["expires_at"])
    claim = sa.table("signuppromoclaim", sa.column("inbox", sa.String()),
                     sa.column("user_id", sa.Integer()), sa.column("created_at", sa.DateTime()))
    connection = op.get_bind()
    insert = postgresql.insert if connection.dialect.name == "postgresql" else sqlite.insert
    cursor = ""
    while rows := connection.execute(sa.select(claim).where(claim.c.inbox > cursor)
                                    .order_by(claim.c.inbox).limit(1000)).mappings().all():
        for row in rows:
            try:
                canonical = _inbox(row["inbox"])
            except idna.IDNAError:
                continue
            if canonical != row["inbox"]:
                connection.execute(insert(claim).values(
                    inbox=canonical, user_id=row["user_id"], created_at=row["created_at"],
                ).on_conflict_do_nothing(index_elements=[claim.c.inbox]))
        cursor = rows[-1]["inbox"]


def downgrade() -> None:
    op.drop_index("ix_signuppromobucket_expires_at", table_name="signuppromobucket")
    op.drop_table("signuppromobucket")
