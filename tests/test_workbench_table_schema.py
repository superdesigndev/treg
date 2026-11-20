from datetime import datetime
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from treg.domain.governance.teams import cascade_delete_org
from treg.infra import db
from treg.models import Org, TableDoc, TeamDatabase


migration = import_module("treg.alembic.versions.0077_workbench_table_metadata")
MODELS = (TeamDatabase, TableDoc)
NOW = datetime(2026, 1, 1)


def _apply(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


def _database(team_id=1, **overrides):
    return {
        "id": team_id, "org_id": team_id,
        "turso_database_ref": f"test-organization/team-{team_id}",
        "created_at": NOW, "updated_at": NOW, **overrides,
    }


def _table(org_id=1, name="leads", **overrides):
    return {
        "id": f"tbl_{org_id}_{name}", "org_id": org_id, "team_database_id": org_id,
        "name": name, "sql_name": f"data_{name}", "creation_key": f"create-{name}",
        "creation_fingerprint": "table-fingerprint",
        "column_metadata": [{"id": "name", "name": "Name", "type": "text"}],
        "created_by": "jeremy@example.test", "created_at": NOW, "updated_at": NOW, **overrides,
    }


@pytest.fixture(params=["model", "migration"])
async def store_connection(request):
    await db.reset_db()
    async with db._engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            await connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            await connection.commit()
        transaction = await connection.begin()
        try:
            for org_id in (1, 2):
                await connection.execute(Org.__table__.insert().values(
                    id=org_id, name=f"Workbench {org_id}", slug=f"workbench-{org_id}", balance_micro=123,
                ))
            if request.param == "migration":
                for model in reversed(MODELS):
                    await connection.run_sync(model.__table__.drop)
                await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
            yield connection
        finally:
            await transaction.rollback()
            if sqlite:
                await connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
                await connection.commit()


async def test_empty_upgrade_matches_models_and_preserves_existing_teams(store_connection):
    connection = store_connection
    names = {model.__tablename__ for model in MODELS}
    diff = await connection.run_sync(lambda sync: compare_metadata(
        MigrationContext.configure(sync, opts={
            "compare_server_default": True,
            "include_object": lambda obj, name, kind, reflected, other: kind != "table" or name in names,
        }), SQLModel.metadata,
    ))
    assert diff == []
    for model in MODELS:
        assert (await connection.execute(sa.select(sa.func.count()).select_from(model))).scalar_one() == 0
    assert (await connection.execute(sa.select(Org.id, Org.balance_micro).order_by(Org.id))).all() == [(1, 123), (2, 123)]


async def test_management_tables_have_no_foreign_keys(store_connection):
    foreign_keys = await store_connection.run_sync(lambda sync: {
        model.__tablename__: sa.inspect(sync).get_foreign_keys(model.__tablename__) for model in MODELS
    })
    assert foreign_keys == {model.__tablename__: [] for model in MODELS}


@pytest.mark.parametrize("overrides", [
    {"org_id": 1}, {"turso_database_ref": "test-organization/team-1"},
])
async def test_database_identities_are_unique(store_connection, overrides):
    table = TeamDatabase.__table__
    await store_connection.execute(table.insert().values(**_database()))
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(table.insert().values(**_database(2, **overrides)))
    await store_connection.execute(table.insert().values(**_database(2)))
    assert (await store_connection.execute(sa.select(table.c.org_id).order_by(table.c.org_id))).scalars().all() == [1, 2]


@pytest.mark.parametrize("duplicate", ["name", "sql_name", "creation_key"])
async def test_table_identities_are_unique_within_their_team(store_connection, duplicate):
    for org_id in (1, 2):
        await store_connection.execute(TeamDatabase.__table__.insert().values(**_database(org_id)))
        await store_connection.execute(TableDoc.__table__.insert().values(**_table(org_id)))
    candidate = {**_table(name="other"), duplicate: _table()[duplicate]}
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(TableDoc.__table__.insert().values(**candidate))
    await store_connection.execute(TableDoc.__table__.insert().values(**_table(name="other")))
    assert (await store_connection.execute(sa.select(sa.func.count()).select_from(TableDoc))).scalar_one() == 3


@pytest.mark.parametrize("sql_name", [
    "", " ", "\t", "Leads", "lead-list", "lead.list", "lead\n", "\nlead", "naïve",
    "\x00", "lead\x00list", "1lead", "sqlite_leads", "sqlite_",
])
async def test_table_sql_name_rejects_noncanonical_or_reserved_names(store_connection, sql_name):
    connection = store_connection
    await connection.execute(TeamDatabase.__table__.insert().values(**_database()))
    expected = sa.exc.IntegrityError
    if "\x00" in sql_name and connection.dialect.name == "postgresql":
        expected = sa.exc.DBAPIError
    with pytest.raises(expected):
        async with connection.begin_nested():
            await connection.execute(TableDoc.__table__.insert().values(**_table(sql_name=sql_name)))
    assert (await connection.execute(sa.select(sa.func.count()).select_from(TableDoc))).scalar_one() == 0


@pytest.mark.parametrize("sql_name", ["a", "_", "_0", "a0_", "lead_2", "sqlite", "sqlitex"])
async def test_table_sql_name_accepts_canonical_identifiers(store_connection, sql_name):
    await store_connection.execute(TeamDatabase.__table__.insert().values(**_database()))
    await store_connection.execute(TableDoc.__table__.insert().values(**_table(sql_name=sql_name)))
    assert (await store_connection.execute(sa.select(TableDoc.sql_name))).scalar_one() == sql_name


async def test_database_connection_reference_is_unique_when_present(store_connection):
    connection = store_connection
    table = TeamDatabase.__table__
    for org_id in (1, 2):
        await connection.execute(table.insert().values(**_database(org_id)))
    assert (await connection.execute(sa.select(table.c.connection_ref))).scalars().all() == [None, None]
    await connection.execute(table.update().where(table.c.id == 1).values(connection_ref="test-connection"))
    with pytest.raises(sa.exc.IntegrityError):
        async with connection.begin_nested():
            await connection.execute(table.update().where(table.c.id == 2).values(connection_ref="test-connection"))
    await connection.execute(table.update().where(table.c.id == 2).values(connection_ref="another-connection"))
    assert (await connection.execute(sa.select(table.c.connection_ref).order_by(table.c.id))).scalars().all() == [
        "test-connection", "another-connection",
    ]


@pytest.mark.parametrize("field", ["id", "name", "sql_name", "creation_key", "creation_fingerprint"])
@pytest.mark.parametrize("blank", ["", " "])
async def test_table_identity_fields_cannot_be_blank(store_connection, field, blank):
    table = TableDoc.__table__
    await store_connection.execute(TeamDatabase.__table__.insert().values(**_database()))
    await store_connection.execute(table.insert().values(**_table()))
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(table.update().values(**{field: blank}))
    row = (await store_connection.execute(sa.select(table))).mappings().one()
    assert row[field] == _table()[field]


async def test_column_metadata_uses_the_database_json_type(store_connection):
    columns = await store_connection.run_sync(lambda sync: sa.inspect(sync).get_columns(TableDoc.__tablename__))
    column_type = next(column["type"] for column in columns if column["name"] == "column_metadata")
    assert isinstance(column_type, JSONB if store_connection.dialect.name == "postgresql" else sa.JSON)


@pytest.mark.parametrize("invalid", [
    {"turso_database_ref": None}, {"turso_database_ref": ""}, {"turso_database_ref": " "},
    {"database_generation": 0}, {"database_generation": -1},
    {"status": "ready"}, {"status": "ready", "connection_ref": ""},
    {"status": "ready", "connection_ref": " "},
])
async def test_database_requires_identity_generation_and_ready_connection(store_connection, invalid):
    table = TeamDatabase.__table__
    await store_connection.execute(table.insert().values(**_database()))
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(table.update().values(**invalid))
    row = (await store_connection.execute(sa.select(table))).mappings().one()
    assert row["status"] == "provisioning" and row["connection_ref"] is None
    assert row["database_generation"] == 1


@pytest.mark.parametrize("invalid", [
    {"schema_version": 0}, {"schema_version": -1},
    {"metadata_revision": 0}, {"metadata_revision": -1},
    {"database_generation": 0}, {"database_generation": -1},
    {"column_metadata": None}, {"status": "ready"},
    {"status": "ready", "database_generation": 1},
])
async def test_table_revisions_and_ready_schema_are_validated(store_connection, invalid):
    table = TableDoc.__table__
    await store_connection.execute(TeamDatabase.__table__.insert().values(**_database()))
    await store_connection.execute(table.insert().values(**_table()))
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(table.update().values(**invalid))
    row = (await store_connection.execute(sa.select(table))).mappings().one()
    assert row["status"] == "creating" and row["schema_version"] is None
    assert row["database_generation"] is None
    assert row["metadata_revision"] == 1 and row["column_metadata"] == _table()["column_metadata"]


async def test_ready_table_requires_a_validated_database_generation(store_connection):
    await store_connection.execute(TeamDatabase.__table__.insert().values(**_database()))
    with pytest.raises(sa.exc.IntegrityError):
        async with store_connection.begin_nested():
            await store_connection.execute(TableDoc.__table__.insert().values(
                **_table(status="ready", schema_version=1),
            ))


async def test_database_and_table_can_be_promoted_from_pending_to_ready(store_connection):
    connection = store_connection
    database, table = (model.__table__ for model in MODELS)
    await connection.execute(database.insert().values(**_database()))
    await connection.execute(table.insert().values(**_table()))
    await connection.execute(database.update().values(status="ready", connection_ref="test-connection", database_generation=2))
    await connection.execute(table.update().values(
        status="ready", schema_version=1, metadata_revision=2, database_generation=2,
    ))
    database_row = (await connection.execute(sa.select(database))).mappings().one()
    table_row = (await connection.execute(sa.select(table))).mappings().one()
    assert database_row["status"] == "ready" and database_row["database_generation"] == 2
    assert table_row["status"] == "ready" and table_row["schema_version"] == 1
    assert table_row["database_generation"] == 2
    assert table_row["metadata_revision"] == 2
    assert table_row["created_at"] == table_row["updated_at"] == NOW
    assert table_row["creation_fingerprint"] == "table-fingerprint"


async def test_database_defaults_apply_without_model_insert_defaults(store_connection):
    connection = store_connection
    tables = await connection.run_sync(lambda sync: {
        model: sa.Table(model.__tablename__, sa.MetaData(), autoload_with=sync) for model in MODELS
    })
    await connection.execute(tables[TeamDatabase].insert().values(**_database()))
    await connection.execute(tables[TableDoc].insert().values(**_table()))
    database_row = (await connection.execute(sa.select(tables[TeamDatabase]))).mappings().one()
    table_row = (await connection.execute(sa.select(tables[TableDoc]))).mappings().one()
    assert database_row["status"] == "provisioning" and database_row["database_generation"] == 1
    assert database_row["connection_ref"] is None
    assert table_row["status"] == "creating" and table_row["metadata_revision"] == 1
    assert table_row["schema_version"] is None
    assert table_row["database_generation"] is None
    assert tables[TableDoc].c.database_generation.server_default is None


async def test_restore_requires_revalidation_before_generation_matched_selection(store_connection):
    connection = store_connection
    database, table = (model.__table__ for model in MODELS)
    await connection.execute(database.insert().values(**_database(
        status="ready", connection_ref="test-connection", database_generation=1,
    )))
    for name in ("leads", "companies"):
        await connection.execute(table.insert().values(**_table(
            name=name, status="ready", schema_version=1, database_generation=1,
        )))
    eligible = sa.select(table.c.id).join(database, sa.and_(
        table.c.team_database_id == database.c.id, table.c.org_id == database.c.org_id,
    )).where(
        table.c.status == "ready", database.c.status == "ready",
        table.c.database_generation == database.c.database_generation,
    ).order_by(table.c.id)
    assert (await connection.execute(eligible)).scalars().all() == ["tbl_1_companies", "tbl_1_leads"]

    await connection.execute(database.update().where(database.c.id == 1).values(database_generation=2))
    assert (await connection.execute(sa.select(table.c.database_generation))).scalars().all() == [1, 1]
    assert (await connection.execute(eligible)).scalars().all() == []

    await connection.execute(table.update().where(table.c.id == "tbl_1_leads").values(database_generation=2))
    assert (await connection.execute(eligible)).scalars().all() == ["tbl_1_leads"]


async def test_team_deletion_removes_only_its_database_and_table_metadata(store_connection):
    connection = store_connection
    for org_id in (1, 2):
        await connection.execute(TeamDatabase.__table__.insert().values(**_database(org_id)))
        await connection.execute(TableDoc.__table__.insert().values(**_table(org_id)))
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        org = await session.get(Org, 1)
        await cascade_delete_org(org, session)
        await session.flush()
    for model in MODELS:
        assert (await connection.execute(sa.select(model.org_id))).scalars().all() == [2]
    assert (await connection.execute(sa.select(Org.id, Org.balance_micro))).all() == [(2, 123)]


async def test_downgrade_preserves_teams_and_allows_a_fresh_upgrade(store_connection):
    connection = store_connection
    await connection.execute(TeamDatabase.__table__.insert().values(**_database()))
    await connection.execute(TableDoc.__table__.insert().values(**_table()))
    await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    names = await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names())
    assert all(model.__tablename__ not in names for model in MODELS)
    assert (await connection.execute(sa.select(Org.id, Org.balance_micro).order_by(Org.id))).all() == [(1, 123), (2, 123)]
    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    for model in MODELS:
        assert (await connection.execute(sa.select(sa.func.count()).select_from(model))).scalar_one() == 0


async def test_postgres_upgrade_does_not_lock_org_while_balance_writer_is_open():
    if db._engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL relation locks")
    await db.reset_db()
    async with db._engine.begin() as connection:
        for model in reversed(MODELS):
            await connection.run_sync(model.__table__.drop)
        await connection.execute(Org.__table__.insert().values(
            id=1, name="Workbench migration lock test", slug="workbench-lock-test", balance_micro=123,
        ))
    try:
        async with db._engine.connect() as writer:
            transaction = await writer.begin()
            try:
                await writer.execute(sa.update(Org).where(Org.id == 1).values(balance_micro=124))
                async with db._engine.begin() as connection:
                    await connection.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
                    await connection.execute(sa.text("SET LOCAL statement_timeout = '5s'"))
                    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
                    assert (await connection.execute(sa.text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE pid = pg_backend_pid() AND relation = 'org'::regclass"
                    ))).scalar_one() == 0
                    for model in MODELS:
                        assert await connection.run_sync(
                            lambda sync, model=model: sa.inspect(sync).has_table(model.__tablename__))
            finally:
                await transaction.rollback()
    finally:
        await db.reset_db()
