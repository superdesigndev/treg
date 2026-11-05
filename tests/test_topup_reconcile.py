"""A paid top-up always ends up in the ledger, bonus included, or an operator hears about it.

Two halves: a delivery whose bonus grant failed is failed back to Stripe and the redelivery grants
the bonus once; and `treg-worker billing reconcile` checks what Stripe collected against the ledger.
`billing._sdk` is the single Stripe funnel, so patching it fakes every Stripe call.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlmodel import select

from conftest import make_upstream, verified_signup

from treg import worker
from treg.api import app
from treg.application import billing
from treg.config import get_settings
from treg.domain import money
from treg.infra import db as infra_db
from treg.infra import slack
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
    monkeypatch.setattr(s, "ops_slack_webhook_url", "", raising=False)
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


async def _schema_ok() -> None:
    """The worker's startup schema check, which a test database does not need to pass."""


def _stripe_lists(*pages: list[dict]):
    async def sdk(fn, /, **kw):
        assert fn == "PaymentIntent.list", fn
        index = 0 if "starting_after" not in kw else next(
            i + 1 for i, page in enumerate(pages) if page and page[-1]["id"] == kw["starting_after"])
        return {"data": pages[index], "has_more": index + 1 < len(pages)}
    return sdk


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


async def test_a_bonus_healed_after_a_refund_keeps_only_the_unrefunded_share(c, monkeypatch):
    """A refund processed before the redelivery could not see the bonus; the heal applies it."""
    org_id = await _org(c)
    _fail_bonus_grants(monkeypatch, times=1)
    pi = _pi(org_id, "pi_refund_first", 10_000)
    assert (await _deliver(c, pi)).status_code == 500
    async with session_maker() as db:
        await billing.revoke_refunded(db, "pi_refund_first", 50_000_000, source="test")
    assert (await _deliver(c, pi)).status_code == 200
    assert await _bonuses(org_id, "pi_refund_first") == [10_000_000]
    async with session_maker() as db:
        bonus = (await db.execute(select(CreditBlock).where(
            CreditBlock.org_id == org_id, CreditBlock.kind == "bonus"))).scalar_one()
        assert bonus.remaining_micro == 5_000_000, "half the payment was refunded, so half the bonus goes"
        await billing.revoke_refunded(db, "pi_refund_first", 50_000_000, source="test")
        await db.refresh(bonus)
        assert bonus.remaining_micro == 5_000_000, "the same refund again takes nothing twice"


async def test_a_full_refund_racing_a_bonus_heal_leaves_no_bonus(c, monkeypatch):
    """Whichever of the refund and the healing redelivery takes the payment's lock first, a fully
    refunded purchase keeps none of its bonus."""
    import asyncio

    org_id = await _org(c)
    _fail_bonus_grants(monkeypatch, times=1)
    pi = _pi(org_id, "pi_refund_race", 10_000)
    assert (await _deliver(c, pi)).status_code == 500

    async def refund():
        async with session_maker() as db:
            await billing.revoke_refunded(db, "pi_refund_race", 100_000_000, source="test")

    await asyncio.gather(refund(), _deliver(c, pi))
    async with session_maker() as db:
        blocks = (await db.execute(select(CreditBlock).where(
            CreditBlock.org_id == org_id, CreditBlock.kind == "bonus"))).scalars().all()
    assert sum(b.remaining_micro for b in blocks) == 0


async def test_a_refund_racing_a_settlement_does_not_deadlock(c):
    """Settlement row-locks an org's blocks in id order. A refund must take its blocks in that order
    too: when the bonus block sorts first, locking the purchased block first deadlocks."""
    import asyncio

    if session_maker.kw["bind"].dialect.name != "postgresql":
        pytest.skip("row-lock order needs an isolated PostgreSQL database via TREG_TEST_DB_URL")
    org_id = await _org(c)
    for n in range(64):
        pi = f"pi_order_{n}"
        assert (await _deliver(c, _pi(org_id, pi, 10_000))).status_code == 200
        async with session_maker() as db:
            purchased = (await money.block_for_payment(db, pi)).id
            bonus = (await db.execute(select(LedgerEntry.block_id).where(
                LedgerEntry.org_id == org_id, LedgerEntry.kind == "grant"))).scalars().all()
        bonus = [b for b in bonus if b != purchased and b < purchased]
        if bonus:
            break
    else:
        pytest.fail("no bonus block sorted before its purchased block")

    async with session_maker() as settle:
        await settle.execute(select(CreditBlock.id).where(CreditBlock.id == bonus[0]).with_for_update())

        async def refund():
            async with session_maker() as db:
                return await billing.revoke_refunded(db, pi, 100_000_000, source="test")

        refunding = asyncio.create_task(refund())
        await asyncio.sleep(0.5)
        await settle.execute(select(CreditBlock.id).where(CreditBlock.id == purchased).with_for_update())
        await settle.commit()
    assert (await asyncio.wait_for(refunding, 10))["handled"] is True


async def test_two_deliveries_of_one_payment_at_once_grant_one_bonus(c):
    """`checkout.session.completed` and `payment_intent.succeeded` arrive together for every Checkout
    purchase. The lock on the purchased block keeps them to one bonus on Postgres and SQLite alike."""
    import asyncio

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


async def test_reconcile_reports_every_paid_top_up_the_ledger_does_not_match(c, monkeypatch):
    org_id = await _org(c)
    ok, auto, unbonused, lost = (_pi(org_id, "pi_ok", 10_000), _pi(org_id, "pi_auto", 10_000, auto="1"),
                                 _pi(org_id, "pi_unbonused", 200_000), _pi(org_id, "pi_lost", 92_000))
    for pi in (ok, auto):
        assert (await _deliver(c, pi)).status_code == 200
    auto_unbonused = _pi(org_id, "pi_auto_unbonused", 10_000, auto="1")
    _fail_bonus_grants(monkeypatch, times=2)
    assert (await _deliver(c, unbonused)).status_code == 500
    assert (await _deliver(c, auto_unbonused)).status_code == 500
    stranger = {**_pi(org_id, "pi_demo", 500), "metadata": {}, "customer": "cus_not_a_team"}
    refused = {**_pi(org_id, "pi_declined", 5_000), "status": "requires_payment_method"}
    monkeypatch.setattr(billing, "_sdk", _stripe_lists([lost, unbonused, stranger],
                                                       [refused, auto, auto_unbonused, ok]))

    until = datetime.now(timezone.utc).replace(tzinfo=None)
    report = await billing.reconcile_topups(since=until - timedelta(hours=48), until=until)

    assert report["topups"] == 5
    assert {(g["gap"], g["payment_intent"]) for g in report["gaps"]} == {
        (billing.GAP_NOT_CREDITED, "pi_lost"), (billing.GAP_BONUS_MISSING, "pi_unbonused"),
        (billing.GAP_BONUS_MISSING, "pi_auto_unbonused")}
    lost_gap = next(g for g in report["gaps"] if g["payment_intent"] == "pi_lost")
    assert lost_gap["org_id"] == org_id and lost_gap["amount_micro"] == 920_000_000


async def test_reconcile_ignores_a_paid_top_up_of_a_deleted_team(c, monkeypatch):
    org_id = await _org(c)
    assert (await _deliver(c, _pi(org_id, "pi_gone", 10_000))).status_code == 200
    from treg.domain.governance.teams import cascade_delete_org
    from treg.models import Org
    async with session_maker() as db:
        await cascade_delete_org(await db.get(Org, org_id), db)
        await db.commit()
    monkeypatch.setattr(billing, "_sdk", _stripe_lists([_pi(org_id, "pi_gone", 10_000)]))
    until = datetime.now(timezone.utc).replace(tzinfo=None)
    report = await billing.reconcile_topups(since=until - timedelta(hours=48), until=until)
    assert report["gaps"] == []


async def test_reconcile_flags_a_payment_credited_for_another_amount(c, monkeypatch):
    org_id = await _org(c)
    assert (await _deliver(c, _pi(org_id, "pi_amount", 5_000))).status_code == 200
    monkeypatch.setattr(billing, "_sdk", _stripe_lists([_pi(org_id, "pi_amount", 6_000)]))
    until = datetime.now(timezone.utc).replace(tzinfo=None)
    report = await billing.reconcile_topups(since=until - timedelta(hours=48), until=until)
    assert [g["gap"] for g in report["gaps"]] == [billing.GAP_CREDITED_DIFFERENTLY]


async def test_the_worker_alerts_slack_only_when_a_top_up_is_missing(c, monkeypatch, capsys):
    org_id = await _org(c)
    monkeypatch.setattr(get_settings(), "ops_slack_webhook_url", "https://hooks.slack.test/ops", raising=False)
    posted: list[tuple[str, str]] = []

    async def post(url, text, **kw):
        posted.append((url, text))

    monkeypatch.setattr(slack, "post", post)
    monkeypatch.setattr(infra_db, "verify_db", _schema_ok)
    args = argparse.Namespace(hours=48, grace_minutes=30)

    monkeypatch.setattr(billing, "_sdk", _stripe_lists([]))
    assert await worker._billing_reconcile(args) == 0
    assert posted == []

    monkeypatch.setattr(billing, "_sdk", _stripe_lists([_pi(org_id, "pi_missing", 92_000)]))
    assert await worker._billing_reconcile(args) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["gaps"][0]["payment_intent"] == "pi_missing"
    [(url, text)] = posted
    assert url == "https://hooks.slack.test/ops"
    assert f"not_credited: org {org_id}, $920.00, pi_missing" in text


async def test_the_worker_alerts_slack_when_the_check_itself_fails(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "ops_slack_webhook_url", "https://hooks.slack.test/ops", raising=False)
    posted: list[str] = []

    async def post(url, text, **kw):
        posted.append(text)

    class StripeDown(Exception):
        pass

    async def stripe_down(fn, /, **kw):
        raise StripeDown("Stripe is down")

    monkeypatch.setattr(slack, "post", post)
    monkeypatch.setattr(infra_db, "verify_db", _schema_ok)
    monkeypatch.setattr(billing, "_sdk", stripe_down)
    with pytest.raises(StripeDown):
        await worker._billing_reconcile(argparse.Namespace(hours=48, grace_minutes=30))
    assert posted == ["treg-worker billing reconcile failed: StripeDown: Stripe is down"]


async def test_slack_post_sends_the_text_as_an_incoming_webhook_message():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    await slack.post("https://hooks.slack.test/ops", "hello", transport=httpx.MockTransport(handler))
    assert json.loads(seen[0].content) == {"text": "hello"}
    with pytest.raises(httpx.HTTPStatusError):
        await slack.post("https://hooks.slack.test/ops", "hello",
                         transport=httpx.MockTransport(lambda r: httpx.Response(404)))
