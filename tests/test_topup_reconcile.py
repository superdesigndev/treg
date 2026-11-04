"""A paid top-up always ends up in the ledger, bonus included.

A delivery whose bonus grant failed is failed back to Stripe and the redelivery grants the bonus once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlmodel import select

from conftest import make_upstream, verified_signup

from treg.api import app
from treg.application import billing
from treg.config import get_settings
from treg.domain import money
from treg.infra.db import reset_db, session_maker
from treg.models import CreditBlock, LedgerEntry

WHSEC = "whsec_test_secret_for_the_suite"


@pytest.fixture
async def c(monkeypatch):
    await reset_db()
    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", "sk_test_suite", raising=False)
    monkeypatch.setattr(s, "stripe_webhook_secret", WHSEC, raising=False)
    monkeypatch.setattr(s, "topup_bonus_tiers", {10: 0, 50: 5, 100: 10, 200: 15}, raising=False)
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    billing._locks.clear()
    billing._scheduled.clear()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


async def _no_sdk(fn, /, **kw):
    raise AssertionError(f"unexpected Stripe call: {fn} {kw}")


async def _org(c: AsyncClient, email: str = "payer@x.dev") -> int:
    r = await verified_signup(c, json={"email": email})
    assert r.status_code == 200, r.text
    return r.json()["org_id"]


def _pi(org_id: int, pi: str, cents: int, *, auto: str = "0", created: int | None = None) -> dict:
    return {"id": pi, "object": "payment_intent", "amount": cents, "amount_received": cents,
            "currency": "usd", "status": "succeeded", "payment_method": "pm_card_visa",
            "created": int(time.time()) - 3600 if created is None else created,
            "metadata": {"treg_org_id": str(org_id), "treg_kind": "topup", "treg_auto": auto}}


async def _deliver(c: AsyncClient, pi: dict):
    payload = json.dumps({"id": f"evt_{pi['id']}", "type": "payment_intent.succeeded",
                          "data": {"object": pi}}).encode()
    t = int(time.time())
    mac = hmac.new(WHSEC.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return await c.post("/billing/stripe/webhook", content=payload,
                        headers={"stripe-signature": f"t={t},v1={mac}", "content-type": "application/json"})


async def _bonuses(org_id: int, pi: str) -> list[int]:
    async with session_maker() as db:
        rows = (await db.execute(select(LedgerEntry.amount_micro, LedgerEntry.meta).where(
            LedgerEntry.org_id == org_id, LedgerEntry.kind == "grant"))).all()
    return [amount for amount, meta in rows
            if meta.get("source") == "topup_bonus" and meta.get("payment_intent") == pi]


def _fail_bonus_grants(monkeypatch, times: int) -> None:
    real, left = money.grant, [times]

    async def grant(db, org_id, **kw):
        if kw.get("kind") == "bonus" and left[0] > 0:
            left[0] -= 1
            raise RuntimeError("integer out of range")
        return await real(db, org_id, **kw)

    monkeypatch.setattr(money, "grant", grant)


async def test_a_failed_bonus_fails_the_delivery_and_a_redelivery_grants_it_once(c, monkeypatch):
    org_id = await _org(c)
    _fail_bonus_grants(monkeypatch, times=1)
    pi = _pi(org_id, "pi_heal", 10_000)

    first = await _deliver(c, pi)
    assert first.status_code == 500, "Stripe must redeliver a top-up whose bonus is missing"
    async with session_maker() as db:
        block = (await db.execute(select(CreditBlock).where(
            CreditBlock.stripe_payment_intent == "pi_heal"))).scalar_one()
    assert block.amount_micro == 100_000_000, "the purchase itself stays credited"
    assert await _bonuses(org_id, "pi_heal") == []

    second = await _deliver(c, pi)
    assert second.status_code == 200 and second.json()["credited"] is False
    assert await _bonuses(org_id, "pi_heal") == [10_000_000]

    assert (await _deliver(c, pi)).status_code == 200
    assert await _bonuses(org_id, "pi_heal") == [10_000_000], "a third delivery grants nothing more"


async def test_two_deliveries_of_one_payment_at_once_grant_one_bonus(c):
    """`checkout.session.completed` and `payment_intent.succeeded` arrive together for every Checkout
    purchase. The row lock that keeps them to one bonus exists only on Postgres (the CI job)."""
    import asyncio

    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires an isolated PostgreSQL database via TREG_TEST_DB_URL")
    org_id = await _org(c)
    pi = _pi(org_id, "pi_race", 10_000)
    first, second = await asyncio.gather(_deliver(c, pi), _deliver(c, {**pi}))
    assert {first.status_code, second.status_code} == {200}
    assert await _bonuses(org_id, "pi_race") == [10_000_000]


async def test_a_redelivery_past_stripes_retry_window_leaves_the_bonus_to_a_human(c, monkeypatch):
    org_id = await _org(c)
    _fail_bonus_grants(monkeypatch, times=1)
    pi = _pi(org_id, "pi_old", 200_000)
    assert (await _deliver(c, pi)).status_code == 500
    async with session_maker() as db:
        await db.execute(update(CreditBlock).where(CreditBlock.stripe_payment_intent == "pi_old")
                         .values(created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4)))
        await db.commit()

    assert (await _deliver(c, pi)).status_code == 200
    assert await _bonuses(org_id, "pi_old") == []
