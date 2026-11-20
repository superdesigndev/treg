"""Migration keeps earlier manual removals when WorkOS identity rows are retired."""

from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def test_existing_manual_removals_are_backfilled(monkeypatch):
    migration = import_module("treg.alembic.versions.0085_enterprise_sso_blocks")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE org (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE user (id INTEGER PRIMARY KEY)"))
        connection.execute(text("""CREATE TABLE enterprisessoidentity (
            org_id INTEGER NOT NULL, user_id INTEGER NOT NULL, manually_blocked BOOLEAN NOT NULL)"""))
        connection.execute(text("INSERT INTO org (id) VALUES (1)"))
        connection.execute(text("INSERT INTO user (id) VALUES (10), (11)"))
        connection.execute(text("""INSERT INTO enterprisessoidentity
            (org_id, user_id, manually_blocked) VALUES (1, 10, 1), (1, 11, 0)"""))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        rows = connection.execute(text("SELECT org_id, user_id FROM enterprisessoblock")).all()
        assert rows == [(1, 10)]
