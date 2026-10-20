"""Lens migrations match runtime models and retain the team's hard isolation boundary."""
from datetime import datetime, timedelta
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from treg.domain.governance.teams import cascade_delete_org
from treg.infra import db
from treg.models import LensProject, LensRun, Org
from treg.timeutil import utcnow_naive

migration = import_module("treg.alembic.versions.0078_lens")
MODELS = (LensProject, LensRun)
NOW = datetime(2026, 1, 1)


def _apply(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


def _project(org_id, project_id=None, **overrides):
    return {"id": project_id or f"project-{org_id}", "org_id": org_id, "user_id": 1,
            "request_id": "create-profile", "fingerprint": "profile-fingerprint",
            "payload": "encrypted-profile", "revision": 1, "created_at": NOW,
            "updated_at": NOW, **overrides}


def _run(org_id):
    return {"id": f"run-{org_id}", "org_id": org_id, "user_id": 1,
            "project_id": f"project-{org_id}", "project_revision": 1,
            "kind": "audit", "state": "quoted", "payload": "encrypted-plan",
            "created_at": NOW, "expires_at": NOW}


@pytest.fixture(params=["model", "migration"])
async def lens_connection(request):
    await db.reset_db()
    async with db._engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            await connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            await connection.commit()
        transaction = await connection.begin()
        try:
            if request.param == "migration":
                for model in reversed(MODELS):
                    await connection.run_sync(model.__table__.drop)
                await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
            for org_id in (1, 2):
                await connection.execute(Org.__table__.insert().values(
                    id=org_id, name=f"Lens {org_id}", slug=f"lens-{org_id}", balance_micro=123))
            yield connection
        finally:
            await transaction.rollback()
            if sqlite:
                await connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
                await connection.commit()


async def test_lens_migration_matches_models_without_changing_teams(lens_connection):
    names = {model.__tablename__ for model in MODELS}
    diff = await lens_connection.run_sync(lambda sync: compare_metadata(
        MigrationContext.configure(sync, opts={
            "compare_server_default": True,
            "include_object": lambda obj, name, kind, reflected, other: kind != "table" or name in names,
        }), SQLModel.metadata))
    assert diff == []
    for model in MODELS:
        assert (await lens_connection.execute(sa.select(sa.func.count()).select_from(model))).scalar_one() == 0
    assert (await lens_connection.execute(sa.select(Org.balance_micro))).scalars().all() == [123, 123]


async def test_profile_creation_identifier_is_unique_within_its_team(lens_connection):
    table = LensProject.__table__
    for org_id in (1, 2):
        await lens_connection.execute(table.insert().values(**_project(org_id)))
    with pytest.raises(sa.exc.IntegrityError):
        async with lens_connection.begin_nested():
            await lens_connection.execute(table.insert().values(**_project(1, "duplicate")))
    assert (await lens_connection.execute(sa.select(sa.func.count()).select_from(table))).scalar_one() == 2


async def test_team_deletion_removes_runs_before_profiles_and_preserves_other_team(lens_connection):
    for org_id in (1, 2):
        await lens_connection.execute(LensProject.__table__.insert().values(**_project(org_id)))
        await lens_connection.execute(LensRun.__table__.insert().values(**_run(org_id)))
    async with AsyncSession(bind=lens_connection, expire_on_commit=False) as session:
        await cascade_delete_org(await session.get(Org, 1), session)
        await session.flush()
    for model in MODELS:
        assert (await lens_connection.execute(sa.select(model.org_id))).scalars().all() == [2]
    assert (await lens_connection.execute(sa.select(Org.id, Org.balance_micro))).all() == [(2, 123)]


async def test_lens_downgrade_and_reupgrade_preserve_unrelated_rows(lens_connection):
    await lens_connection.execute(LensProject.__table__.insert().values(**_project(1)))
    await lens_connection.execute(LensRun.__table__.insert().values(**_run(1)))
    await lens_connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    await lens_connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    for model in MODELS:
        assert (await lens_connection.execute(sa.select(sa.func.count()).select_from(model))).scalar_one() == 0
    assert (await lens_connection.execute(sa.select(Org.id, Org.balance_micro).order_by(Org.id))).all() == [(1, 123), (2, 123)]


async def test_interrupted_recovery_preserves_receipts_written_after_the_read_snapshot():
    from treg.application import lens

    await db.reset_db()
    async with db.session_maker() as session:
        org = Org(id=1, name="Recovery", slug="lens-recovery")
        session.add(org)
        await session.flush()
        session.add(LensRun(id="recover", org_id=1, user_id=1, kind="audit", state="running",
            expires_at=utcnow_naive() + timedelta(minutes=5),
            deadline_at=utcnow_naive() - timedelta(seconds=1),
            payload=lens._pack({"plan": {}, "result": {"attempts": [
                {"id": "finished", "state": "running", "charged_micro": None},
                {"id": "pending", "state": "running", "charged_micro": None}],
                "known_cost_micro": 0, "cost_known": False, "total_cost_micro": None}})))
        await session.commit()
    async with db.session_maker() as reader:
        stale = await reader.get(LensRun, "recover")
        # Retain a stale ORM identity while allowing another writer's receipt to commit.
        await reader.commit()
        async with db.session_maker() as writer:
            current = await writer.get(LensRun, "recover")
            payload = lens._unpack(current.payload)
            payload["result"]["attempts"][0].update(state="completed", charged_micro=123, call_ref="receipt")
            payload["result"]["known_cost_micro"] = 123
            current.payload = lens._pack(payload)
            writer.add(current)
            await writer.commit()
        await lens._recover(reader, stale)
        recovered = lens._unpack(stale.payload)["result"]
        assert stale.state == "interrupted"
        assert recovered["attempts"][0] == {
            "id": "finished", "state": "completed", "charged_micro": 123, "call_ref": "receipt"}
        assert recovered["attempts"][1]["state"] == "unknown"
        assert recovered["known_cost_micro"] == 123
        assert recovered["cost_known"] is False and recovered["total_cost_micro"] is None
