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


def _drop_present(connection) -> None:
    # Drop what is ACTUALLY in the database, not only the tables the models know: a serial run
    # reuses one sqlite file, and a table left behind by another branch or by the private admin
    # app (apikey, apikeyevent) is invisible to `metadata.drop_all` and then shows up in the
    # autogenerate diff as drift this repo cannot fix. Plain DROPs by name, not reflection: a
    # leftover may hold a foreign key to a table an earlier partial drop already removed.
    from sqlalchemy import inspect
    cascade = " CASCADE" if connection.dialect.name == "postgresql" else ""
    for name in inspect(connection).get_table_names():
        connection.execute(text(f'DROP TABLE IF EXISTS "{name}"{cascade}'))


async def _drop_everything() -> None:
    async with db._engine.begin() as connection:
        await connection.run_sync(_drop_present)
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
        await _upgrade_to("0033")
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


async def test_signup_upgrade_does_not_reopen_existing_user_claims():
    """Upgrade actual pre-fix rows on SQLite and the serial PostgreSQL CI database."""
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to('0032')
        async with db._engine.begin() as connection:
            await connection.execute(text('''
                INSERT INTO "user" (email, is_superadmin, suspended, token_version,
                                    onboarded, demo, created_at)
                VALUES ('pre-upgrade@example.org', false, false, 0, false, false, CURRENT_TIMESTAMP)
            '''))
        await _upgrade_to('head')
        async with db._engine.begin() as connection:
            row = (await connection.execute(text('''
                SELECT email_verified_at, signup_promo_available
                FROM "user" WHERE email = 'pre-upgrade@example.org'
            '''))).one()
            assert row.email_verified_at is None
            assert not row.signup_promo_available
            # Even a successful proof later must not undo the migration's decision.
            await connection.execute(text('''
                UPDATE "user" SET email_verified_at = CURRENT_TIMESTAMP
                WHERE email = 'pre-upgrade@example.org'
            '''))
            assert not (await connection.execute(text('''
                SELECT signup_promo_available FROM "user" WHERE email = 'pre-upgrade@example.org'
            '''))).scalar_one()
    finally:
        await _drop_everything()
        await db.reset_db()


async def test_activity_key_index_matches_newest_first_query():
    """A key lookup must avoid scanning or sorting the full Activity history."""
    if db._engine.dialect.name != 'sqlite':
        return  # PostgreSQL schema equivalence is covered by the head comparison above.
    async with db._engine.connect() as connection:
        for table in ('callrecord', 'runrecord'):
            rows = (await connection.execute(text(
                f'EXPLAIN QUERY PLAN SELECT * FROM {table} '
                'WHERE org_id = 1 AND api_key_id = 2 ORDER BY id DESC LIMIT 50'
            ))).all()
            plan = ' '.join(str(row) for row in rows)
            assert f'ix_{table}_org_key_id' in plan
            assert 'TEMP B-TREE' not in plan
