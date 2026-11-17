"""Migration 0106 keys each payer team by network and address, and keeps the rows it finds."""

from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _at_0105(connection) -> None:
    connection.execute(text("""CREATE TABLE agentpayer (
        address VARCHAR NOT NULL PRIMARY KEY, org_id INTEGER NOT NULL UNIQUE,
        created_at DATETIME NOT NULL)"""))
    connection.execute(text("""CREATE TABLE agentpayment (
        id INTEGER PRIMARY KEY, payer_address VARCHAR NOT NULL, network VARCHAR NOT NULL,
        created_at DATETIME NOT NULL)"""))
    connection.execute(text("""INSERT INTO agentpayer (address, org_id, created_at) VALUES
        ('0xaa', 1, '2026-11-01 00:00:00'), ('0xbb', 2, '2026-11-02 00:00:00')"""))
    connection.execute(text("""INSERT INTO agentpayment (id, payer_address, network, created_at) VALUES
        (1, '0xaa', 'tempo:42431', '2026-11-01 00:00:01'),
        (2, '0xaa', 'eip155:84532', '2026-11-01 00:00:02')"""))


def test_each_payer_takes_its_first_payment_network(monkeypatch):
    migration = import_module("treg.alembic.versions.0106_agentpayer_per_network")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        _at_0105(connection)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        rows = connection.execute(text(
            "SELECT network, address, org_id FROM agentpayer ORDER BY address")).all()
        assert rows == [("tempo:42431", "0xaa", 1), ("unknown", "0xbb", 2)]
        connection.execute(text("""INSERT INTO agentpayer (network, address, org_id, created_at)
            VALUES ('eip155:84532', '0xaa', 3, '2026-11-03 00:00:00')"""))
        migration.downgrade()
        rows = connection.execute(text(
            "SELECT address, org_id FROM agentpayer ORDER BY address")).all()
        assert rows == [("0xaa", 1), ("0xbb", 2)]
