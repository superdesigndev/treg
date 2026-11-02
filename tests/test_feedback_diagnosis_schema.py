"""Internal diagnosis rows hold a consistent endpoint attribution and leave with their report."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from treg.models import FeedbackDiagnosis


def test_diagnosis_migration_enforces_attribution_and_cascades(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'diagnosis.db'}")
    path = Path(__file__).parents[1] / 'src/treg/alembic/versions/0093_feedback_diagnosis.py'
    spec = importlib.util.spec_from_file_location('diagnosis_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as db:
        db.execute(text('PRAGMA foreign_keys=ON'))
        db.execute(text('CREATE TABLE feedback (id INTEGER PRIMARY KEY, message TEXT NOT NULL)'))
        db.execute(text("INSERT INTO feedback VALUES (1, 'Synthetic report'), (2, 'Another')"))
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        row = dict(feedback_id=1, endpoint_id='example.web.search', endpoint_source='call', scope='endpoint',
                   disposition='defect', topic='billing', probabilities={}, classifier='synthetic/v1')
        db.execute(FeedbackDiagnosis.__table__.insert().values(**row))
        db.execute(FeedbackDiagnosis.__table__.insert().values(
            **dict(row, feedback_id=2, endpoint_id=None, endpoint_source=None)))
        for invalid in [
            dict(row, feedback_id=2),
            dict(row, feedback_id=3, endpoint_source='guess'),
            dict(row, feedback_id=3, endpoint_source=None),
            dict(row, feedback_id=99),
        ]:
            with pytest.raises(IntegrityError), db.begin_nested():
                db.execute(FeedbackDiagnosis.__table__.insert().values(**invalid))
        db.execute(text('DELETE FROM feedback WHERE id=1'))
        assert db.execute(text('SELECT feedback_id FROM feedbackdiagnosis')).scalars().all() == [2]
        with Operations.context(MigrationContext.configure(db)):
            migration.downgrade()
        db.execute(text("INSERT INTO feedback VALUES (3, 'Still accepts reports')"))
    engine.dispose()
