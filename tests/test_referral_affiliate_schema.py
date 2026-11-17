"""Affiliate migrations keep legacy users capped and operation history durable."""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from treg.models import ReferralAffiliateEvent, User


def test_affiliate_migration_defaults_constraints_history_and_downgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'affiliate.db'}")
    path = Path(__file__).parents[1] / "src/treg/alembic/versions/0097_referral_affiliate.py"
    spec = importlib.util.spec_from_file_location("affiliate_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        with engine.begin() as db:
            db.execute(text("PRAGMA foreign_keys=ON"))
            db.execute(text('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email TEXT NOT NULL)'))
            db.execute(text('INSERT INTO "user" VALUES (1, \'existing@example.test\')'))
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()
            db.execute(text('INSERT INTO "user" (id, email) VALUES (2, \'new@example.test\')'))
            assert db.execute(text(
                'SELECT id, referral_affiliate, referral_affiliate_version FROM "user" ORDER BY id'
            )).all() == [(1, 0, 0), (2, 0, 0)]
            user = User(email="model@example.test")
            assert user.referral_affiliate is False
            assert user.referral_affiliate_version == 0
            for column in ("referral_affiliate", "referral_affiliate_version"):
                with pytest.raises(IntegrityError), db.begin_nested():
                    db.execute(text(f'UPDATE "user" SET {column}=NULL WHERE id=1'))

            created_at = datetime(2026, 1, 1)
            event = dict(operation_id="first-operation", user_id=1, user_created_at=created_at, version=1,
                         previous_affiliate=False, affiliate=True,
                         reason="Approved for referral credit", principal="env-admin")
            db.execute(ReferralAffiliateEvent.__table__.insert().values(**event))
            for invalid in [
                dict(event, user_id=2, version=2),
                dict(event, operation_id="duplicate-user-version"),
                dict(event, operation_id="zero-version", version=0),
                dict(event, operation_id="empty-reason", version=2, reason=""),
                dict(event, operation_id="blank-reason", version=2, reason="   "),
                dict(event, operation_id="empty-principal", version=2, principal=""),
                dict(event, operation_id="blank-principal", version=2, principal="   "),
                dict(event, operation_id="null-reason", version=2, reason=None),
                dict(event, operation_id="null-principal", version=2, principal=None),
                dict(event, operation_id="null-user-created-at", version=2, user_created_at=None),
            ]:
                with pytest.raises(IntegrityError), db.begin_nested():
                    db.execute(ReferralAffiliateEvent.__table__.insert().values(**invalid))
            db.execute(ReferralAffiliateEvent.__table__.insert().values(
                **dict(event, operation_id="second-operation", version=2,
                       previous_affiliate=True, affiliate=False),
            ))
            db.execute(text('DELETE FROM "user" WHERE id=1'))
            assert db.execute(text(
                "SELECT operation_id, version, previous_affiliate, affiliate "
                "FROM referralaffiliateevent ORDER BY version"
            )).all() == [("first-operation", 1, 0, 1), ("second-operation", 2, 1, 0)]
            assert inspect(db).get_foreign_keys("referralaffiliateevent") == []
            db.execute(text('INSERT INTO "user" (id, email) VALUES (1, \'replacement@example.test\')'))
            replacement = dict(event, operation_id="replacement-operation",
                               user_created_at=created_at + timedelta(days=1))
            db.execute(ReferralAffiliateEvent.__table__.insert().values(**replacement))
            with pytest.raises(IntegrityError), db.begin_nested():
                db.execute(ReferralAffiliateEvent.__table__.insert().values(
                    **dict(replacement, operation_id="duplicate-replacement-version"),
                ))
            assert db.execute(text(
                "SELECT count(*) FROM referralaffiliateevent WHERE user_id=1 AND version=1"
            )).scalar_one() == 2

            with Operations.context(MigrationContext.configure(db)):
                migration.downgrade()
            assert "referralaffiliateevent" not in inspect(db).get_table_names()
            assert {column["name"] for column in inspect(db).get_columns("user")} == {"id", "email"}
            assert db.execute(text('SELECT id, email FROM "user" ORDER BY id')).all() == [
                (1, "replacement@example.test"), (2, "new@example.test"),
            ]
            db.execute(text('INSERT INTO "user" VALUES (3, \'after-downgrade@example.test\')'))
    finally:
        engine.dispose()
