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
of the team waits behind it until its admission budget runs out. Carrying the two columns lets
the `TagSpend` sum be an index-only scan: a heap page is skipped only once vacuum has marked it
all-visible, so recent rows can still be fetched. The price is on the write side: settle rewrites
`amount_micro` and `settled`, so that update is never HOT and touches every index of the table.

The old index stays: this revision is additive, and it is prefix-covered by the new one, so a
later revision drops it once this one is in place.

A large table's index is built by hand before the deploy, never by this revision (0123 explains
why). It leaves alone a valid index whose table, method, key columns in order and INCLUDE columns
match this one; refuses a valid index of that name defined otherwise and never drops it; builds
the index itself (CONCURRENTLY, in an autocommit block, an INVALID leftover dropped first) only
when the table holds at most `BUILD_IN_MIGRATION_MAX_ROWS` rows (a bounded count); and otherwise
refuses at once with `MANUAL_BUILD`, the statements to run by hand. A lock timeout during the
small build is raised as a plain error, so the upgrade's lock-timeout retry does not start the
build again from zero.

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

_DEFINITION = (f"CREATE INDEX CONCURRENTLY {NAME} ON {_TABLE} ({', '.join(_COLUMNS)}) "
               f"INCLUDE ({', '.join(_INCLUDE)})")
_MANUAL_STEPS = (
    "From a session with no statement timeout (psql), then deploy again:\n"
    "  SET statement_timeout = 0;\n"
    f"  SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('{NAME}');\n"
    f"  -- only if that returned false (an INVALID leftover of a killed build): "
    f"DROP INDEX CONCURRENTLY {NAME};\n"
    f"  {_DEFINITION};\n"
    f"  SELECT pg_get_indexdef(indexrelid), indisvalid FROM pg_index "
    f"WHERE indexrelid = to_regclass('{NAME}');  -- must match the line above and be true")

MANUAL_BUILD = (
    f"Refusing migration 0134: {_TABLE} holds more than {BUILD_IN_MIGRATION_MAX_ROWS} rows and the "
    f"index {NAME} is missing or INVALID. Build it by hand before deploying. " + _MANUAL_STEPS)

_STATE = sa.text(
    "SELECT i.indisvalid, t.relname, am.amname, i.indnkeyatts, "
    "i.indpred IS NULL AND i.indexprs IS NULL AS plain, "
    "array(SELECT a.attname::text FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum ORDER BY k.ord) "
    "AS columns "
    "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_am am ON am.oid = c.relam "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = :name")


def _matches(state) -> bool:
    keys, included = state.columns[:state.indnkeyatts], state.columns[state.indnkeyatts:]
    return (state.relname == _TABLE and state.amname == "btree" and state.plain
            and keys == _COLUMNS and sorted(included) == sorted(_INCLUDE))


def _mismatch(state) -> str:
    return (
        f"Refusing migration 0134: a valid index {NAME} exists but is not {_DEFINITION} "
        f"(it is on {state.relname}, {state.amname}, columns {state.columns}, "
        f"{state.indnkeyatts} of them keys"
        f"{'' if state.plain else ', partial or on expressions'}). It was built with another "
        "definition; it is left in place. Check what uses it, drop it by hand "
        f"(DROP INDEX CONCURRENTLY {NAME}), build it as below, then deploy again. " + _MANUAL_STEPS)

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
        state = bind.execute(_STATE, {"name": NAME}).first()
        valid = None if state is None else state.indisvalid
        if valid is True:
            if not _matches(state):
                raise RuntimeError(_mismatch(state))
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
