"""Signup-credit abuse regressions through HTTP, with real money and identity transactions."""
import asyncio
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlmodel import select

from conftest import verified_identity
from treg.config import get_settings
from treg.domain import money
from treg.infra.db import session_maker
from treg.domain.identity.access import _norm_email
from treg.domain.identity.promotions import promo_inbox
from treg.models import CreditBlock, Hold, LedgerEntry, Org, SignupPromoClaim, User


async def _team(client, token, name):
    r = await client.post('/orgs', json={'name': name}, headers={'X-Treg-Token': token})
    assert r.status_code == 200, r.text
    return r.json()


async def _funding(ids):
    async with session_maker() as db:
        orgs = (await db.execute(select(Org).where(Org.id.in_(ids)))).scalars().all()
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.org_id.in_(ids), LedgerEntry.kind == 'grant'))).scalars().all()
        blocks = (await db.execute(select(CreditBlock).where(CreditBlock.org_id.in_(ids)))).scalars().all()
        holds = (await db.execute(select(Hold).where(Hold.org_id.in_(ids)))).scalars().all()
    assert sum(o.balance_micro for o in orgs) == sum(b.remaining_micro for b in blocks) - sum(h.amount_micro for h in holds)
    return sum(o.balance_micro for o in orgs), entries


async def test_legacy_registration_cannot_mint_credit(clients):
    r = await clients.post('/users', json={'email': 'unverified@example.org'})
    assert r.status_code == 200
    created = r.json()
    teams = [created]
    for index in range(3):
        teams.append(await _team(clients, created['token'], f'unverified-{index}'))
    assert await _funding([t['org_id'] for t in teams]) == (0, [])
    # A legacy token never becomes proof. A real inbox proof enables ONE future team.
    token = await verified_identity(clients, 'unverified@example.org')
    teams.append(await _team(clients, token, 'after-proof'))
    teams.append(await _team(clients, created['token'], 'old-token-after-proof'))
    balance, entries = await _funding([t['org_id'] for t in teams])
    assert balance == get_settings().promo_grant_micro
    assert len(entries) == 1


@pytest.mark.parametrize('concurrent', [False, True])
async def test_verified_user_gets_one_grant_across_teams(clients, concurrent):
    token = await verified_identity(clients, 'verified@example.org')
    if concurrent:
        teams = await asyncio.gather(*(_team(clients, token, f'racing-{i}') for i in range(8)))
    else:
        teams = [await _team(clients, token, f'sequential-{i}') for i in range(4)]
    balance, entries = await _funding([t['org_id'] for t in teams])
    assert balance == get_settings().promo_grant_micro
    assert len(entries) == 1
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == 'verified@example.org'))).scalar_one()
        assert not user.signup_promo_available
        assert entries[0].meta['source'] == 'signup'
        assert entries[0].meta['user_id'] == user.id


async def test_deleting_funded_team_does_not_restore_claim(clients):
    token = await verified_identity(clients, 'delete@example.org')
    first = await _team(clients, token, 'delete-me')
    assert (await _funding([first['org_id']]))[0] == get_settings().promo_grant_micro
    deleted = await clients.request('DELETE', f"/orgs/{first['org_id']}",
        params={'confirm': first['org']}, headers={'X-Treg-Token': first['token']})
    assert deleted.status_code == 200, deleted.text
    recreated = await _team(clients, token, 'delete-me')
    assert await _funding([recreated['org_id']]) == (0, [])


async def test_invite_code_is_not_an_email_proof(clients):
    org = (await clients.get('/orgs')).json()[0]
    invite = await clients.post(f"/orgs/{org['org_id']}/invites",
        json={'email': 'invite-code@example.org', 'role': 'member'})
    assert invite.status_code == 200
    accepted = await clients.post('/invites/accept', json={
        'email': 'invite-code@example.org', 'code': invite.json()['code']})
    assert accepted.status_code == 200
    team = await _team(clients, accepted.json()['token'], 'invite-code-team')
    assert await _funding([team['org_id']]) == (0, [])


async def test_legacy_user_remains_ineligible_after_verification(clients):
    async with session_maker() as db:
        db.add(User(email='legacy@example.org', signup_promo_available=False))
        await db.commit()
    token = await verified_identity(clients, 'legacy@example.org')
    teams = [await _team(clients, token, f'legacy-{i}') for i in range(2)]
    assert await _funding([t['org_id'] for t in teams]) == (0, [])


async def test_failed_grant_rolls_back_claim_and_money(clients, monkeypatch):
    token = await verified_identity(clients, 'failure@example.org')
    original = money.grant

    async def fail_after_staging(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('synthetic post-write failure')

    with monkeypatch.context() as patch:
        patch.setattr(money, 'grant', fail_after_staging)
        first = await _team(clients, token, 'failed-grant')
    assert await _funding([first['org_id']]) == (0, [])
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == 'failure@example.org'))).scalar_one()
        assert user.signup_promo_available
    second = await _team(clients, token, 'retry-grant')
    third = await _team(clients, token, 'after-retry')
    balance, entries = await _funding([first['org_id'], second['org_id'], third['org_id']])
    assert balance == get_settings().promo_grant_micro
    assert len(entries) == 1


async def test_disabled_promo_does_not_consume_new_user_claim(clients, monkeypatch):
    token = await verified_identity(clients, 'disabled@example.org')
    with monkeypatch.context() as patch:
        patch.setattr(get_settings(), 'promo_grant_micro', 0)
        first = await _team(clients, token, 'disabled-grant')
    assert await _funding([first['org_id']]) == (0, [])
    second = await _team(clients, token, 'enabled-grant')
    assert (await _funding([second['org_id']]))[0] == get_settings().promo_grant_micro


@pytest.mark.parametrize(('address', 'inbox'), [
    ('Jane.Doe+promo@GMail.com', 'janedoe@gmail.com'),
    ('j.a.n.e.d.o.e@googlemail.com', 'janedoe@gmail.com'),
    ('jane.doe+a+b@example.org', 'jane.doe@example.org'),
    (' ops@example.org ', 'ops@example.org'),
    ('farmer@GMail.com.', 'farmer@gmail.com'),
    ('ops@example.org..', 'ops@example.org'),
    ('Far.Mer+x@GMail.com.', 'farmer@gmail.com'),
])
def test_promo_inbox_folds_aliases_of_one_mailbox(address, inbox):
    assert promo_inbox(address) == inbox


@pytest.mark.parametrize(('address', 'identity'), [
    (' Bob@X.com. ', 'bob@x.com'),
    ('bob+tag@x.com..', 'bob+tag@x.com'),   # login identity keeps the alias; only the gift folds it
    ('a@.', 'a@.'),                          # a domain of dots alone is not emptied into `a@`
    ('no-at-sign.', 'no-at-sign.'),
])
def test_login_identity_drops_only_a_trailing_domain_dot(address, identity):
    assert _norm_email(address) == identity


@pytest.mark.parametrize('concurrent', [False, True])
async def test_aliases_of_one_inbox_share_one_grant(clients, concurrent):
    aliases = ['farm.er+1@gmail.com', 'farmer+2@gmail.com', 'f.a.r.m.e.r@googlemail.com',
               'farmer@gmail.com', 'farmer+3@gmail.com.']
    tokens = [await verified_identity(clients, alias) for alias in aliases]
    if concurrent:
        teams = await asyncio.gather(*(_team(clients, t, f'alias-{i}') for i, t in enumerate(tokens)))
    else:
        teams = [await _team(clients, t, f'alias-{i}') for i, t in enumerate(tokens)]
    balance, entries = await _funding([t['org_id'] for t in teams])
    assert balance == get_settings().promo_grant_micro
    assert len(entries) == 1
    async with session_maker() as db:
        claims = (await db.execute(select(SignupPromoClaim).where(
            SignupPromoClaim.inbox == 'farmer@gmail.com'))).scalars().all()
    assert [(c.inbox, c.user_id) for c in claims] == [('farmer@gmail.com', entries[0].meta['user_id'])]


async def test_distinct_inboxes_on_one_domain_each_get_a_grant(clients):
    teams = [await _team(clients, await verified_identity(clients, email), email)
             for email in ('first@example.org', 'second@example.org')]
    balance, entries = await _funding([t['org_id'] for t in teams])
    assert balance == 2 * get_settings().promo_grant_micro
    assert len(entries) == 2


async def test_failed_grant_leaves_the_inbox_unclaimed(clients, monkeypatch):
    token = await verified_identity(clients, 'retry+1@example.org')
    original = money.grant

    async def fail_after_staging(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('synthetic post-write failure')

    with monkeypatch.context() as patch:
        patch.setattr(money, 'grant', fail_after_staging)
        failed = await _team(clients, token, 'failed-grant')
    alias = await _team(clients, await verified_identity(clients, 'retry+2@example.org'), 'alias')
    balance, entries = await _funding([failed['org_id'], alias['org_id']])
    assert balance == get_settings().promo_grant_micro
    assert len(entries) == 1


def test_claim_migration_backfills_consumed_inboxes(tmp_path):
    path = Path(__file__).parents[1] / 'src/treg/alembic/versions/0071_signup_promo_claim.py'
    spec = importlib.util.spec_from_file_location('promo_claim_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'claim.db'}")
    with engine.begin() as db:
        db.execute(text('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email TEXT NOT NULL, '
                        'email_verified_at DATETIME, signup_promo_available BOOLEAN NOT NULL)'))
        db.execute(text(
            "INSERT INTO \"user\" VALUES "
            "(1, 'a.b+1@gmail.com', '2026-01-01', 0), "   # claimed
            "(2, 'ab+2@gmail.com', '2026-01-02', 0), "    # a second alias that also claimed
            "(3, 'fresh@example.org', '2026-01-03', 1), "  # verified, not yet claimed
            "(4, 'legacy@example.org', NULL, 0)"           # never verified
        ))
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        assert db.execute(text('SELECT inbox, user_id FROM signuppromoclaim')).all() == [('ab@gmail.com', 1)]
    engine.dispose()
    assert migration._inbox('x.y+z@googlemail.com') == promo_inbox('x.y+z@googlemail.com')


def test_migration_disables_old_users_and_preserves_old_writer_default(tmp_path):
    path = Path(__file__).parents[1] / 'src/treg/alembic/versions/0033_signup_promo_eligibility.py'
    spec = importlib.util.spec_from_file_location('promo_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'promo.db'}")
    with engine.begin() as db:
        db.execute(text('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email TEXT NOT NULL)'))
        db.execute(text("INSERT INTO \"user\" VALUES (1, 'existing@example.org')"))
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        # Pre-upgrade code does not know the columns and must not enable new claims either.
        db.execute(text("INSERT INTO \"user\" (id, email) VALUES (2, 'old-writer@example.org')"))
        assert db.execute(text('SELECT signup_promo_available, email_verified_at FROM "user"')).all() == [(0, None), (0, None)]
    assert User(email='new-code@example.org').signup_promo_available is True
    engine.dispose()

