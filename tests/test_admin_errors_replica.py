"""Failure evidence may lag; authorization and primary pool isolation must not."""

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from treg.api import app
from treg.config import get_settings
from treg.domain.identity import session as identity_session
from treg.infra import db as infra_db
from treg.models import CallRecord, Org, User
from treg.timeutil import utcnow_naive

from test_read_replica import load_database  # noqa: F401 - isolated engine fixture


@pytest.fixture
async def error_client(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "test-admin-errors")
    get_settings.cache_clear()
    await infra_db.reset_db()
    async with infra_db.session_maker() as db:
        db.add(User(id=1, email="admin@example.test", is_superadmin=True))
        db.add(Org(id=1, name="Primary", slug="primary-team"))
        await db.flush()
        db.add(CallRecord(id=1, org_id=1, user_email="admin@example.test", tool_name="example",
                          method="POST", path="/example", status_code=500, provider="example",
                          error_response="primary evidence"))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        client.cookies.set("treg_session", identity_session.make_session(1))
        yield client
    get_settings.cache_clear()


@pytest.fixture
async def replica(error_client, tmp_path, monkeypatch, load_database):
    url = f"sqlite+aiosqlite:///{tmp_path / 'replica.db'}"
    writer = create_async_engine(url)
    async with writer.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.execute(User.__table__.insert().values(
            id=1, email="admin@example.test", is_superadmin=True))
        await conn.execute(Org.__table__.insert().values(id=1, name="Replica", slug="replica-team"))
        for call_id, age, provider, status, tier in (
            (11, 0, "example", 500, "platform"),
            (12, 0, "another", 400, None),
            (13, 15, "example", 500, "platform"),
        ):
            await conn.execute(CallRecord.__table__.insert().values(
                id=call_id, org_id=1, user_email="admin@example.test", tool_name="example",
                method="POST", path="/example", status_code=status, provider=provider,
                credential_tier=tier, created_at=utcnow_naive() - timedelta(days=age),
                error_request="replica request", error_response="replica evidence"))
    await writer.dispose()
    db = load_database(url)
    monkeypatch.setattr(infra_db, "_read_db_url", url)
    monkeypatch.setattr(infra_db, "read_session_maker", db.read_session_maker)
    return db


async def test_errors_and_org_names_come_from_replica(error_client, replica):
    primary_reads = []

    def record_primary(conn, cursor, statement, *args):
        primary_reads.append(statement.lower())

    event.listen(infra_db._admin_engine.sync_engine, "before_cursor_execute", record_primary)
    try:
        response = await error_client.get("/admin/errors?days=1&provider=example&status=500&tier=platform")
    finally:
        event.remove(infra_db._admin_engine.sync_engine, "before_cursor_execute", record_primary)
    assert response.status_code == 200, response.text
    errors = response.json()["errors"]
    assert [row["id"] for row in errors] == [11]
    assert errors[0]["org"] == "replica-team"
    assert errors[0]["response"] == "replica evidence"
    assert not any("from callrecord" in sql or "from org" in sql for sql in primary_reads)


async def test_replica_retention_order_and_empty_tier_are_preserved(error_client, replica):
    response = await error_client.get("/admin/errors?days=30&limit=2")
    errors = response.json()["errors"]
    assert [row["id"] for row in errors] == [13, 12]
    assert errors[0]["expired"] is True
    assert errors[0]["request"] is None and errors[0]["response"] is None
    own = await error_client.get("/admin/errors?tier=")
    assert [row["id"] for row in own.json()["errors"]] == [12]
    async with replica.read_session_maker() as db:
        assert (await db.get(CallRecord, 13)).error_response == "replica evidence"


@pytest.mark.parametrize("field,value", [("is_superadmin", False), ("suspended", True), ("token_version", 1)])
async def test_revocation_uses_primary_before_opening_replica(error_client, replica, field, value):
    async with infra_db.session_maker() as db:
        user = await db.get(User, 1)
        setattr(user, field, value)
        await db.commit()

    @event.listens_for(replica._read_engine.sync_engine, "do_connect")
    def forbid_replica(*args):
        pytest.fail("an unauthorized request opened the read database")

    assert (await error_client.get("/admin/errors")).status_code == 403


async def test_unconfigured_errors_keep_the_admin_pool(error_client, monkeypatch):
    def forbid_api_session():
        pytest.fail("an admin error query used the API session factory")

    monkeypatch.setattr(infra_db, "_read_db_url", "")
    monkeypatch.setattr(infra_db, "read_session_maker", forbid_api_session)
    monkeypatch.setattr(infra_db, "session_maker", forbid_api_session)
    response = await error_client.get("/admin/errors")
    assert response.status_code == 200, response.text
    assert response.json()["errors"][0]["org"] == "primary-team"


@pytest.mark.parametrize("stage", ["connect", "query"])
async def test_replica_failure_is_not_retried_on_primary(error_client, replica, stage):
    @event.listens_for(replica._read_engine.sync_engine,
                       "do_connect" if stage == "connect" else "before_cursor_execute")
    def fail_replica(*args):
        raise RuntimeError("replica unavailable")

    with pytest.raises(RuntimeError, match="replica unavailable"):
        await error_client.get("/admin/errors")
    assert replica._read_engine.pool.checkedout() == 0


@pytest.mark.skipif(infra_db._is_sqlite, reason="requires isolated PostgreSQL")
async def test_postgres_report_releases_primary_before_reading(
    error_client, load_database, monkeypatch,
):
    replica = load_database(infra_db._db_url, "read.pool_size=1")
    monkeypatch.setattr(infra_db, "_read_db_url", infra_db._db_url)
    monkeypatch.setattr(infra_db, "read_session_maker", replica.read_session_maker)
    connections_held = []

    @event.listens_for(replica._read_engine.sync_engine, "before_cursor_execute")
    def observe_query(conn, cursor, statement, parameters, context, executemany):
        if "FROM callrecord" in statement or "FROM org" in statement:
            connections_held.append(infra_db._admin_engine.pool.checkedout())

    response = await error_client.get("/admin/errors")
    assert response.status_code == 200, response.text
    assert response.json()["errors"][0]["org"] == "primary-team"
    assert connections_held == [0, 0], "authorization held a primary connection during the report"
    assert replica._read_engine.pool.checkedout() == 0
    assert infra_db._admin_engine.pool.checkedout() == 0
