"""Operational fixes: dev-code locked out of prod."""
from __future__ import annotations


def test_dev_code_exposed_only_on_local_sqlite():
    from treg.config import Settings
    assert Settings(email_dev_mode=True, database_url="sqlite+aiosqlite:///./x.db").expose_dev_code is True
    # a real (Postgres) deploy NEVER exposes the code, even if the flag is on by mistake
    assert Settings(email_dev_mode=True, database_url="postgresql+asyncpg://u@h/db").expose_dev_code is False
    assert Settings(email_dev_mode=False, database_url="sqlite+aiosqlite:///./x.db").expose_dev_code is False
