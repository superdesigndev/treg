"""tagspend: let autovacuum keep up with the table, so the tag reads stay index-only

Revision ID: 0136
Revises: 0135
Create Date: 2026-11-26

The tag spend reads (`money.tag_spent_since`, `tag_invoice_since`) are index-only scans on
`ix_tagspend_org_dim_val_created_amount` (0134). An index-only scan skips a row's heap page only
when the visibility map marks the page all-visible, and only vacuum sets that bit. Every call that
carries a tag inserts a row and its settle rewrites it, so pages leave the all-visible state as
fast as calls arrive. With the default thresholds autovacuum waits for dead rows worth a fifth of
the table, or inserts worth a fifth of it; on a large table that is days of traffic, during which
a busy tag's month read fetches the heap for a large share of its rows, inside the reserve's money
session. A manual VACUUM brings it back to almost no heap fetches; these settings make autovacuum
do the same on its own, at a hundredth of the table instead of a fifth, and analyze at a fiftieth
so the planner's row estimates for a tag's range follow the table.

Each pass after the first finds most pages already all-visible and skips them, and PostgreSQL
skips the index pass when few rows are dead, so a smaller, more frequent vacuum costs less per pass
than the default's rare, large one.

`ALTER TABLE ... SET (...)` of autovacuum storage parameters takes SHARE UPDATE EXCLUSIVE, which
does not block reads or writes. It waits for a running vacuum of the table, under the migration's
lock timeout and retries. SQLite has no autovacuum: the revision only records itself there.

Rollback floor: the expand-safety check counts the `ALTER` as non-additive, so 0136 is marked a
contract revision. It changes no column, index or constraint: code from before it reads and writes
`tagspend` exactly as before, and downgrade resets the three parameters to the server's defaults.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0136"
down_revision: str | Sequence[str] | None = "0135"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # see the rollback floor note: three table-level autovacuum parameters

SETTINGS = {
    "autovacuum_vacuum_scale_factor": "0.01",
    "autovacuum_vacuum_insert_scale_factor": "0.01",
    "autovacuum_analyze_scale_factor": "0.02",
}


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE tagspend SET ("
               + ", ".join(f"{name} = {value}" for name, value in SETTINGS.items()) + ")")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE tagspend RESET (" + ", ".join(SETTINGS) + ")")
