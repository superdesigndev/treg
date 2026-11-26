"""covering (org_id, dim, val, created_at) INCLUDE (amount_micro, settled) on tagspend - a tag's
spend over a day or a month is read from the index alone

Revision ID: 0134
Revises: 0133
Create Date: 2026-11-26

`money.tag_spent_since` (the tag spend cap a reserve checks) and `money.tag_invoice_since` sum
`amount_micro` over `org_id = ? AND dim = ? AND val = ? AND created_at >= ?`, for up to the month.
`ix_tagspend_org_dim_val_created` finds the range, but `amount_micro` and `settled` are not in it,
so every row of the window costs a heap fetch. A busy tag's rows are spread over the whole month,
so on a cold cache that is one random page read per row: the cap read runs for minutes, inside
the reserve's money session and under the team's money admission lease, and every other reserve
of the team waits behind it until its admission budget runs out. Carrying the two columns makes
it an index-only scan; a row is rewritten once, at settle, and is all-visible once vacuumed.

The old index stays: this revision is additive, and it is prefix-covered by the new one, so a
later revision drops it once this one is in place.

A large table's index is built by hand before the deploy, never by this revision (0123 explains
why): it leaves a valid index alone, builds it itself (CONCURRENTLY, in an autocommit block,
INVALID debris dropped first) only when the table holds at most `BUILD_IN_MIGRATION_MAX_ROWS`
rows (a bounded count), and otherwise refuses at once with `MANUAL_BUILD`, the statements to run
by hand. A lock timeout during the small build is raised as a plain error, so the upgrade's
lock-timeout retry does not start the build again from zero.

The expand-safety linter counts the autocommit escape as non-additive, so this revision declares a
rollback floor pro forma: the operation is one additive index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import DBAPIError

revision: str = "0134"
down_revision: str | Sequence[str] | None = "0133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
contract = True  # pro forma - see the rollback floor note; the operation is one additive index

_TABLE = "tagspend"
NAME = "ix_tagspend_org_dim_val_created_amount"
_COLUMNS = ["org_id", "dim", "val", "created_at"]
_INCLUDE = ["amount_micro", "settled"]
BUILD_IN_MIGRATION_MAX_ROWS = 1_000_000
BUILD_LOCK_TIMEOUT = "180s"

_MANUAL_STEPS = (
    "From a session with no statement timeout (psql), then deploy again:\n"
    "  SET statement_timeout = 0;\n"
    f"  DROP INDEX CONCURRENTLY IF EXISTS {NAME};\n"
    f"  CREATE INDEX CONCURRENTLY {NAME} ON {_TABLE} ({', '.join(_COLUMNS)}) "
    f"INCLUDE ({', '.join(_INCLUDE)});\n"
    f"  SELECT indisvalid FROM pg_index WHERE indexrelid = '{NAME}'::regclass;  -- must be true")

MANUAL_BUILD = (
    f"Refusing migration 0134: {_TABLE} holds more than {BUILD_IN_MIGRATION_MAX_ROWS} rows and the "
    f"index {NAME} is missing or INVALID. Build it by hand before deploying. " + _MANUAL_STEPS)

_VALIDITY = sa.text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = :name")

_BOUNDED_COUNT = sa.text(
    f"SELECT count(*) FROM (SELECT 1 FROM {_TABLE} LIMIT :limit) AS bounded")


def _is_lock_timeout(exc: DBAPIError) -> bool:
    origin = exc.orig
    return "lock timeout" in str(origin or exc) or (
        origin is not None and "LockNotAvailableError" in type(origin).__name__)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":  # SQLite: no concurrent mode, no INCLUDE
        op.create_index(NAME, _TABLE, _COLUMNS)
        return
    # CONCURRENTLY cannot run inside a transaction; alembic opens one by default.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        valid = bind.execute(_VALIDITY, {"name": NAME}).scalar()
        if valid is True:
            return
        rows = bind.execute(_BOUNDED_COUNT, {"limit": BUILD_IN_MIGRATION_MAX_ROWS + 1}).scalar_one()
        if rows > BUILD_IN_MIGRATION_MAX_ROWS:
            raise RuntimeError(MANUAL_BUILD)
        settings = {name: bind.execute(sa.text(f"SHOW {name}")).scalar_one()
                    for name in ("lock_timeout", "statement_timeout")}
        try:
            bind.execute(sa.text(f"SET lock_timeout = '{BUILD_LOCK_TIMEOUT}'"))
            bind.execute(sa.text("SET statement_timeout = 0"))   # this build only; restored below
            if valid is False:  # debris from a killed build - unusable, and never repaired
                op.drop_index(NAME, table_name=_TABLE, postgresql_concurrently=True)
            op.create_index(NAME, _TABLE, _COLUMNS, postgresql_include=_INCLUDE,
                            postgresql_concurrently=True)
        except DBAPIError as exc:
            if not _is_lock_timeout(exc):
                raise
            raise RuntimeError(
                f"Migration 0134 gave up building {NAME}: a transaction open longer than the lock "
                "timeout held the build back. End that transaction and deploy again, or build the "
                "index by hand. " + _MANUAL_STEPS) from exc
        finally:
            for name, value in settings.items():
                bind.execute(sa.text("SELECT set_config(:name, :value, false)"),
                             {"name": name, "value": value})


def downgrade() -> None:
    op.drop_index(NAME, table_name=_TABLE)
