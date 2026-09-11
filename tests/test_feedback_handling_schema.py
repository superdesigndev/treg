"""Internal handling schema preserves reports and cascades with their retention lifecycle."""

import importlib.util

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from treg.models import FeedbackHandling, FeedbackHandlingEvent
from pathlib import Path


def test_handling_migration_preserves_reports_and_enforces_history(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'handling.db'}")
    path = Path(__file__).parents[1] / 'src/treg/alembic/versions/0030_feedback_handling.py'
    spec = importlib.util.spec_from_file_location('handling_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as db:
        db.execute(text('PRAGMA foreign_keys=ON'))
        db.execute(text('CREATE TABLE feedback (id INTEGER PRIMARY KEY, message TEXT NOT NULL)'))
        db.execute(text("INSERT INTO feedback VALUES (1, 'Synthetic report')"))
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        assert db.execute(text('SELECT message FROM feedback')).scalar_one() == 'Synthetic report'
        assert db.execute(text('SELECT count(*) FROM feedbackhandling')).scalar_one() == 0
        db.execute(FeedbackHandling.__table__.insert().values(feedback_id=1))
        event = dict(id='synthetic-event', feedback_id=1, version=1, from_status='open',
                     to_status='investigating', actor='shared-admin', source='api', links=[])
        db.execute(FeedbackHandlingEvent.__table__.insert().values(**event))
        for invalid in [
            dict(event, id='duplicate-version'),
            dict(event, id='unknown-status', version=2, to_status='unknown'),
            dict(event, id='no-close-reason', version=2, to_status='resolved'),
            dict(event, id='missing-report', feedback_id=99),
        ]:
            with pytest.raises(IntegrityError), db.begin_nested():
                db.execute(FeedbackHandlingEvent.__table__.insert().values(**invalid))
        db.execute(text('DELETE FROM feedback WHERE id=1'))
        for table in ['feedbackhandling', 'feedbackhandlingevent']:
            assert db.execute(text(f'SELECT count(*) FROM {table}')).scalar_one() == 0
        with Operations.context(MigrationContext.configure(db)):
            migration.downgrade()
        db.execute(text("INSERT INTO feedback VALUES (2, 'Still accepts reports')"))
    engine.dispose()
