"""Configuration storage on both model-created and migrated SQLite/PostgreSQL schemas."""

from datetime import datetime
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from treg.domain.governance.teams import cascade_delete_org
from treg.infra import db
from treg.models import Org, OrgFlowControlVersion


migration = import_module("treg.alembic.versions.0073_org_flow_control_version")
table = OrgFlowControlVersion.__table__
NOW = datetime(2026, 1, 1)
CONFIG = {
    "schema_version": 1,
    "pause_until": None,
    "rate_limit": {"rps": 3, "burst": 5},
    "concurrency_limit": {"max_in_flight": 2},
}


def _apply(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


def _row(**overrides):
    return {
        "org_id": 1, "version": 1, "config": CONFIG,
        "created_by": "test-operator", "reason": "Synthetic storage test",
        "created_at": NOW, **overrides,
    }


@pytest.fixture(params=["model", "migration"])
async def store_connection(request):
    await db.reset_db()
    async with db._engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # Enable other tables' foreign keys when testing application Org cleanup;
            # the configuration table itself intentionally has no foreign key.
            await connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            await connection.commit()
        transaction = await connection.begin()
        try:
            for org_id in (1, 2):
                await connection.execute(Org.__table__.insert().values(
                    id=org_id, name=f"Storage {org_id}", slug=f"storage-{org_id}",
                    balance_micro=123,
                ))
            if request.param == "migration":
                # Start from populated Org rows with no configuration table.
                await connection.run_sync(table.drop)
                await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
            yield connection
        finally:
            await transaction.rollback()
            if sqlite:
                await connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
                await connection.commit()


async def test_upgrade_is_empty_and_versions_round_trip_per_org(store_connection):
    connection = store_connection
    assert (await connection.execute(sa.select(sa.func.count()).select_from(table))).scalar_one() == 0
    assert (await connection.execute(sa.select(Org.balance_micro).order_by(Org.id))).scalars().all() == [123, 123]

    for values in (_row(), _row(version=2, config={**CONFIG, "rate_limit": None}), _row(org_id=2)):
        await connection.execute(table.insert().values(**values))
    rows = (await connection.execute(
        sa.select(table).where(table.c.org_id == 1).order_by(table.c.version.desc()).limit(2)
    )).mappings().all()
    assert [row["version"] for row in rows] == [2, 1]
    assert rows[0]["config"] == {**CONFIG, "rate_limit": None}
    assert rows[1]["config"] == CONFIG
    assert rows[1]["publish_status"] == "unpublished"
    assert rows[1]["published_at"] is None and rows[1]["published_by"] is None
    assert rows[1]["created_at"] == NOW
    with pytest.raises(sa.exc.IntegrityError):
        async with connection.begin_nested():
            await connection.execute(table.insert().values(**_row()))

    columns = await connection.run_sync(lambda sync: sa.inspect(sync).get_columns(table.name))
    config_type = next(column["type"] for column in columns if column["name"] == "config")
    assert isinstance(config_type, JSONB if connection.dialect.name == "postgresql" else sa.JSON)


@pytest.mark.parametrize("invalid", [
    {"version": 0},
    {"version": -1},
    {"config": None},
    {"publish_status": "active"},
    {"created_by": " "},
    {"reason": " "},
    {"published_by": " "},
    {"publish_status": "published"},
    {"publish_status": "published", "published_by": "test-publisher"},
    {"publish_status": "published", "published_at": NOW},
])
async def test_storage_rejects_invalid_version_or_publication(store_connection, invalid):
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(table.insert().values(**_row(**invalid)))


async def test_publication_metadata_can_change_without_replacing_snapshot(store_connection):
    connection = store_connection
    await connection.execute(table.insert().values(**_row()))
    for status in ("pending", "failed", "published"):
        await connection.execute(table.update().where(
            table.c.org_id == 1, table.c.version == 1,
        ).values(
            publish_status=status, published_by="test-publisher",
            published_at=NOW if status == "published" else None,
        ))
    row = (await connection.execute(sa.select(table))).mappings().one()
    assert row["publish_status"] == "published" and row["published_at"] == NOW
    assert row["published_by"] == "test-publisher"
    assert row["config"] == CONFIG and row["created_by"] == "test-operator"
    assert row["reason"] == "Synthetic storage test" and row["created_at"] == NOW


async def test_application_org_deletion_removes_only_its_configuration_history(store_connection):
    connection = store_connection
    for values in (_row(), _row(version=2), _row(org_id=2)):
        await connection.execute(table.insert().values(**values))
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        org = await session.get(Org, 1)
        await cascade_delete_org(org, session)
        await session.flush()
    assert (await connection.execute(
        sa.select(table.c.org_id, table.c.version)
    )).all() == [(2, 1)]
    assert (await connection.execute(sa.select(Org.id))).scalars().all() == [2]


async def test_schema_downgrade_preserves_org_data_and_can_upgrade_again(store_connection):
    connection = store_connection
    await connection.execute(table.insert().values(**_row()))
    await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    assert not await connection.run_sync(lambda sync: sa.inspect(sync).has_table(table.name))
    assert (await connection.execute(
        sa.select(Org.id, Org.balance_micro).order_by(Org.id)
    )).all() == [(1, 123), (2, 123)]
    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    assert (await connection.execute(sa.select(sa.func.count()).select_from(table))).scalar_one() == 0


async def test_postgres_migration_does_not_lock_org_while_balance_writer_is_open():
    if db._engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL relation locks")
    await db.reset_db()
    async with db._engine.begin() as connection:
        await connection.run_sync(table.drop)
        await connection.execute(Org.__table__.insert().values(
            id=1, name="Migration lock test", slug="migration-lock-test", balance_micro=123,
        ))
    try:
        async with db._engine.connect() as writer:
            transaction = await writer.begin()
            try:
                # A live balance update holds ROW EXCLUSIVE until its transaction ends.
                await writer.execute(sa.update(Org).where(Org.id == 1).values(balance_micro=124))
                async with db._engine.begin() as connection:
                    await connection.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
                    await connection.execute(sa.text("SET LOCAL statement_timeout = '5s'"))
                    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
                    # Creating an FK would wait on the writer above. This migration must not
                    # acquire any relation lock on org, even when it can proceed immediately.
                    assert (await connection.execute(sa.text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE pid = pg_backend_pid() AND relation = 'org'::regclass"
                    ))).scalar_one() == 0
                    assert await connection.run_sync(
                        lambda sync: sa.inspect(sync).has_table(table.name))
            finally:
                await transaction.rollback()
    finally:
        await db.reset_db()
