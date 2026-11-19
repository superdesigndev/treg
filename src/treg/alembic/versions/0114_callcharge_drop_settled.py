"""callcharge: remove queued charges whose call is already settled

Revision ID: 0114
Revises: 0113
Create Date: 2026-11-19

Data only. A fold used to look every claimed charge up in the ledger and drop the ones whose
call already had a `settle` entry, which a rewrite of an unconfirmed write could leave queued.
Rewrites now take such a row out themselves and the fold applies every row it claims, so a row
left from before must not reach it: it would be charged a second time. This removes them, the
same rows the fold used to drop. Nothing to undo.

Run it only once no process whose charge writer leaves such a row queued is still serving
calls: one that queues another after this has run is not cleaned up after, since a revision
runs once.

Rollback floor: with the database at this revision, code may go back no further than the build
whose charge writer takes back a re-queued settled call. That build and every later one run
unchanged on what is left, so such a rollback needs no downgrade. An earlier writer can leave a
settled call queued again, and this revision, already recorded, does not run again on the next
upgrade: a fold that applies every row it claims would then charge that call twice. If an
earlier writer has run, stop writes, let transactions in flight end, and delete those rows
again (the statement below) before a build with such a fold starts.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0114"
down_revision: str | Sequence[str] | None = "0113"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True


def upgrade() -> None:
    op.execute(sa.text("""
        DELETE FROM callcharge
        WHERE EXISTS (
            SELECT 1 FROM ledgerentry
            WHERE ledgerentry.call_id = callcharge.call_id
              AND ledgerentry.org_id = callcharge.org_id
              AND ledgerentry.kind = 'settle')
    """))


def downgrade() -> None:
    pass
