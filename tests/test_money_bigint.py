"""A team's balance can pass $2,147.48, the most a 32-bit integer holds in micro-USD.

These run in the Postgres CI job, where an `integer` column fails with "integer out of range".
SQLite stores 64-bit integers either way, so there they only check the arithmetic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream, verified_signup

from treg.api import app
from treg.application import billing
from treg.config import get_settings
from treg.domain import money
from treg.infra.db import reset_db, session_maker
from treg.models import CreditBlock, Org

ADMIN = "ENV-ADMIN-SECRET"
WHSEC = "whsec_test_secret_for_the_suite"
START = 2_100_000_000  # $2,100
CREDIT = 900_000_000  # $900
INT4_MAX = 2_147_483_647


@pytest.fixture
async def c(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    await reset_db()
    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", "sk_test_suite", raising=False)
    monkeypatch.setattr(s, "stripe_webhook_secret", WHSEC, raising=False)
    monkeypatch.setattr(s, "topup_bonus_tiers", {}, raising=False)  # the bonus is its own block
    monkeypatch.setattr(billing, "_sdk", _no_sdk)
    billing._locks.clear()
    billing._scheduled.clear()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()
    get_settings.cache_clear()


async def _no_sdk(fn, /, **kw):
    raise AssertionError(f"unexpected Stripe call: {fn} {kw}")


async def _org_at_start(c: AsyncClient) -> int:
    user = (await verified_signup(c, json={"email": "big@x.dev"})).json()
    org_id = (await c.post("/orgs", headers={"X-Treg-Token": user["token"]},
                           json={"name": "Big Team"})).json()["org_id"]
    async with session_maker() as db:
        balance = (await db.get(Org, org_id)).balance_micro
        await money.grant(db, org_id, amount_micro=START - balance, kind="promotional", once=False,
                          meta={"source": "test"})
        await db.commit()
    return org_id


async def _assert_ends_at_3000(org_id: int) -> None:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        blocks = (await db.execute(select(CreditBlock).where(CreditBlock.org_id == org_id))).scalars().all()
    assert org.balance_micro == START + CREDIT > INT4_MAX
    assert sum(b.remaining_micro for b in blocks) == org.balance_micro


async def test_a_webhook_top_up_takes_a_balance_past_int4(c: AsyncClient):
    org_id = await _org_at_start(c)
    event = {"id": "evt_pi_big", "type": "payment_intent.succeeded", "data": {"object": {
        "id": "pi_big", "object": "payment_intent", "amount": 90_000, "amount_received": 90_000,
        "currency": "usd", "status": "succeeded", "payment_method": "pm_card_visa",
        "metadata": {"treg_org_id": str(org_id), "treg_kind": "topup", "treg_auto": "0"}}}}
    payload = json.dumps(event).encode()
    t = int(time.time())
    mac = hmac.new(WHSEC.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()

    r = await c.post("/billing/stripe/webhook", content=payload,
                     headers={"stripe-signature": f"t={t},v1={mac}", "content-type": "application/json"})

    assert r.status_code == 200, r.text
    assert r.json()["credited"] is True
    await _assert_ends_at_3000(org_id)


async def test_an_admin_credit_takes_a_balance_past_int4(c: AsyncClient):
    org_id = await _org_at_start(c)

    r = await c.post(f"/admin/orgs/{org_id}/credit", headers={"X-Treg-Token": ADMIN},
                     json={"amount_usd": "900", "ref": "big-1", "reason": "test"})

    assert r.status_code == 200, r.text
    assert r.json()["balance_micro"] == START + CREDIT
    await _assert_ends_at_3000(org_id)
