"""The host migration changes connected tools without taking ownership of manual tools."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_context_dev_host_migration_updates_only_provider_created_tools():
    path = (Path(__file__).parents[1] / "src/treg/alembic/versions/0051_context_dev_tool_host.py")
    spec = importlib.util.spec_from_file_location("context_dev_host_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    metadata = sa.MetaData()
    secrets = sa.Table(
        "secret", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer),
        sa.Column("name", sa.String),
        sa.Column("provider", sa.String),
    )
    tools = sa.Table(
        "tool", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer),
        sa.Column("name", sa.String),
        sa.Column("base_url", sa.String),
        sa.Column("host", sa.String),
        sa.Column("bindings", sa.JSON),
    )
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    old, new = migration.OLD_BASE, migration.NEW_BASE
    with engine.begin() as db:
        db.execute(secrets.insert(), [
            {"id": 1, "org_id": 1, "name": "branddev", "provider": "branddev"},
            {"id": 2, "org_id": 1, "name": "branddev-2", "provider": "branddev"},
            {"id": 3, "org_id": 2, "name": "branddev", "provider": "branddev"},
            {"id": 4, "org_id": 2, "name": "other", "provider": "other"},
        ])
        db.execute(tools.insert(), [
            {"id": 10, "org_id": 1, "name": "branddev", "base_url": old,
             "host": "api.brand.dev", "bindings": [{"secret_id": 1}]},
            {"id": 11, "org_id": 1, "name": "branddev-2", "base_url": old,
             "host": "api.brand.dev", "bindings": [{"secret_id": 2}]},
            {"id": 12, "org_id": 1, "name": "manual", "base_url": old,
             "host": "api.brand.dev", "bindings": [{"secret_id": 1}]},
            {"id": 13, "org_id": 2, "name": "branddev", "base_url": old,
             "host": "api.brand.dev", "bindings": [{"secret_id": 4}]},
            {"id": 14, "org_id": 1, "name": "custom-path", "base_url": old + "/custom",
             "host": "api.brand.dev", "bindings": [{"secret_id": 1}]},
        ])

        def locations():
            return {row.id: (row.base_url, row.host) for row in db.execute(
                sa.select(tools.c.id, tools.c.base_url, tools.c.host)
            )}

        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        migrated = locations()
        assert migrated[10] == (new, "api.context.dev")
        assert migrated[11] == (new, "api.context.dev")
        assert {key: migrated[key] for key in (12, 13, 14)} == {
            12: (old, "api.brand.dev"),
            13: (old, "api.brand.dev"),
            14: (old + "/custom", "api.brand.dev"),
        }
        with Operations.context(MigrationContext.configure(db)):
            migration.downgrade()
        assert locations()[10] == (old, "api.brand.dev")
        assert locations()[11] == (old, "api.brand.dev")
    engine.dispose()
