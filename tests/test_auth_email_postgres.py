"""The email sign-in code's wrong-guess limit under parallel verifies, on a disposable PostgreSQL.

Opt in with TREG_TEST_DB_URL naming a local instance; SQLite serializes writers and cannot show it.
"""

from __future__ import annotations

import asyncio
import hmac
from types import SimpleNamespace

import pytest

from treg import crypto, ratestore
from treg.application import auth as auth_uc
from treg.infra.db import reset_db, session_maker

EMAIL = "parallel@example.test"
REAL = "424242"
PARALLEL = 15


@pytest.fixture
async def local_postgres():
    engine = session_maker.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires disposable PostgreSQL via TREG_TEST_DB_URL")
    assert engine.url.host in {"127.0.0.1", "localhost", "::1"}, "local test DB only"
    await reset_db()
    async with session_maker() as db:
        await ratestore.kv_put(db, auth_uc.OTP_NS, EMAIL, {
            "hash": crypto.hash_token(REAL), "attempts": auth_uc.MAX_OTP_ATTEMPTS,
            "captcha": "", "promo": False,
        }, ttl_s=600)
        await db.commit()


@pytest.fixture
def compared(monkeypatch):
    seen: list[str] = []

    def compare_digest(a, b):
        seen.append(b)
        return hmac.compare_digest(a, b)

    monkeypatch.setattr(auth_uc, "hmac", SimpleNamespace(compare_digest=compare_digest))
    return seen


async def _attempts_left() -> int | None:
    async with session_maker() as db:
        entry = await ratestore.kv_get(db, auth_uc.OTP_NS, EMAIL)
    return None if entry is None else entry["attempts"]


async def _verify_all(codes: list[str]) -> list[object]:
    return await asyncio.gather(
        *[auth_uc.verify_email_login(EMAIL, c) for c in codes], return_exceptions=True)


async def test_each_parallel_wrong_guess_spends_one_attempt(local_postgres):
    wrong = [f"{i:06d}" for i in range(auth_uc.MAX_OTP_ATTEMPTS - 1)]
    results = await _verify_all(wrong)
    assert all(isinstance(r, auth_uc.EmailAuthError) and r.kind == "invalid_code" for r in results)
    assert await _attempts_left() == 1


async def test_parallel_guesses_past_the_limit_are_never_compared(local_postgres, compared):
    results = await _verify_all([f"{i:06d}" for i in range(PARALLEL)])
    assert all(isinstance(r, auth_uc.EmailAuthError) and r.kind == "invalid_code" for r in results)
    assert len(compared) == auth_uc.MAX_OTP_ATTEMPTS
    assert await _attempts_left() is None
