"""Alembic head is the production schema and must match SQLModel metadata."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlmodel import SQLModel
from sqlmodel import select

from treg import audit
from treg import crypto
from treg.infra import db
from treg.maintenance import _alembic_config
from treg.models import ApiKey, Membership, Org, User


async def _drop_everything() -> None:
    async with db._engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


async def _upgrade_to(revision: str) -> None:
    """Run the packaged migration environment without pooled async connections in its way."""
    await db._engine.dispose()
    await asyncio.to_thread(command.upgrade, _alembic_config(), revision)


def _autogenerate_diff(connection) -> list[Any]:
    context = MigrationContext.configure(connection)
    return compare_metadata(context, SQLModel.metadata)


async def test_alembic_head_has_no_model_drift():
    """Alembic is authoritative, so head must match SQLModel metadata exactly."""
    await audit.drain()
    await _drop_everything()

    try:
        await _upgrade_to("head")
        async with db._engine.connect() as connection:
            diff = await connection.run_sync(_autogenerate_diff)
        assert diff == []
    finally:
        await _drop_everything()
        await db.reset_db()


async def test_managed_key_migration_backfills_human_and_agent_hashes():
    """Upgrade keeps old secrets usable without needing plaintext."""
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to("0025")
        human_hash = crypto.hash_token("old-human-key")
        agent_hash = crypto.hash_token("old-agent-key")
        async with db.session_maker() as session:
            org = Org(name="Before upgrade", slug="before-upgrade")
            human = User(email="human@example.dev")
            agent = User(email="worker@agents.treg.local")
            session.add(org); session.add(human); session.add(agent)
            await session.flush()
            session.add(Membership(user_id=human.id, org_id=org.id, role="owner",
                                   token_hash=human_hash))
            session.add(Membership(user_id=agent.id, org_id=org.id, role="member",
                                   token_hash=agent_hash, created_by=human.email))
            await session.commit()

        await _upgrade_to("head")
        async with db.session_maker() as session:
            rows = (await session.execute(select(ApiKey).order_by(ApiKey.kind))).scalars().all()
        assert [(row.identity_label, row.kind, row.key_hash) for row in rows] == [
            ("worker@agents.treg.local", "agent", agent_hash),
            ("human@example.dev", "default_human", None),
            ("human@example.dev", "legacy_human", human_hash),
        ]
    finally:
        await _drop_everything()
        await db.reset_db()
