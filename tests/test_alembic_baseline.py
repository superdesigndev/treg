"""Alembic head is the production schema and must match SQLModel metadata."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import insert, text
from sqlalchemy import inspect as sa_inspect
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


async def _insert_org_at_revision(session, **fields) -> int:
    """Insert an org row using only the columns the CURRENT schema revision has."""
    present = {c["name"] for c in await session.run_sync(
        lambda sync: sa_inspect(sync.connection()).get_columns("org"))}
    values = {k: v for k, v in Org(**fields).model_dump().items() if k in present and v is not None}
    # A table of exactly those columns: inserting through `Org.__table__` would add the model's
    # defaults for every column it knows, including ones the old revision does not have yet.
    from sqlalchemy import column, table
    org = table("org", *(column(k) for k in values))
    await session.execute(insert(org).values(**values))
    return (await session.execute(select(Org.id).where(Org.slug == fields["slug"]))).scalar_one()


async def test_managed_key_migration_backfills_human_and_agent_hashes():
    """Upgrade keeps old secrets usable without needing plaintext."""
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to("0033")
        human_hash = crypto.hash_token("old-human-key")
        agent_hash = crypto.hash_token("old-agent-key")
        async with db.session_maker() as session:
            # The org table at 0033 predates columns the current model carries; insert only the
            # columns that exist there, with the model's own defaults.
            org_id = await _insert_org_at_revision(session, name="Before upgrade", slug="before-upgrade")
            org = Org(id=org_id, name="Before upgrade", slug="before-upgrade")
            human = User(email="human@example.dev")
            agent = User(email="worker@agents.treg.local")
            session.add(human); session.add(agent)
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


async def test_pinned_scope_migration_leaves_legacy_ownership_unattributed():
    """An additive upgrade preserves records without guessing a customer from today's membership."""
    await audit.drain()
    await _drop_everything()
    try:
        await _upgrade_to('0041')
        from treg.models import RunRecord, AsyncTaskRecord, AsyncResourceRecord
        from treg.timeutil import utcnow_naive
        async with db.session_maker() as session:
            org_id = await _insert_org_at_revision(session, name='migration', slug='migration')
            rows = [
                RunRecord(org_id=org_id, user_email='test@example.invalid', bundle_name='echo',
                          exit_code=0, duration_ms=1),
                AsyncTaskRecord(call_id='old', org_id=org_id, provider='synthetic', endpoint_id='test',
                                reserved_micro=1, next_check_at=utcnow_naive()),
                AsyncResourceRecord(org_id=org_id, provider='synthetic', resource_kind='result',
                                    resource_id='old-file', source_call_id='old'),
            ]
            for row in rows:
                values = row.model_dump(exclude={'tags'})
                values = {key: value for key, value in values.items() if value is not None}
                await session.execute(insert(type(row).__table__).values(**values))
            await session.commit()
        await _upgrade_to('head')
        async with db.session_maker() as session:
            for model in (RunRecord, AsyncTaskRecord, AsyncResourceRecord):
                records = (await session.execute(select(model))).scalars().all()
                assert len(records) == 1 and records[0].tags is None
    finally:
        await _drop_everything()
        await db.reset_db()
