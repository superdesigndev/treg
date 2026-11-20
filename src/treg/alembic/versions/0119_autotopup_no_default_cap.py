"""org.autotopup_monthly_cap_micro - the stored former default becomes no cap

Revision ID: 0119
Revises: 0118
Create Date: 2026-11-20

Auto top-up no longer has a default monthly cap: 0 means none. The dashboard used to store the
default it displayed ($2,000) as if the team had typed it, so that value is cleared with it. Any
other stored value is a cap the team chose and stays. Data only; downgrade cannot tell which rows
were changed and leaves them.

Rollback floor: none in practice. A release from before this change reads a stored 0 as its $2,000
default, which is what these rows meant to it already.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0119"
down_revision: str | Sequence[str] | None = "0118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

contract = True  # pro forma, see the rollback floor note: the operation rewrites data


def upgrade() -> None:
    op.execute("UPDATE org SET autotopup_monthly_cap_micro = 0 WHERE autotopup_monthly_cap_micro = 2000000000")


def downgrade() -> None:
    pass
