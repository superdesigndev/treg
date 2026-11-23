"""The partner offer: bonus credit on a paid top-up of a team a partner brought, read from Dub.

Covered: a top-up of a partner-brought team is staged and nothing else is; a fixed offer pays once
per team when paid top-ups reach the minimum, and once per card; a percentage offer pays on each
top-up at or above the minimum for its duration; one grant is capped; no offer, a friend-referral
team, a refunded payment and a dead-lettered lead earn nothing; a lead not verified yet and a Dub
outage retry; and a refund after the grant revokes it like the top-up bonus.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream, verified_signup
from test_referrals import WHSEC, _checkout_event, _deliver, _fake_sdk

from treg.api import app
from treg.application import affiliate_bonus, affiliates, billing
from treg.config import get_settings
from treg.infra import dub
from treg.infra.db import reset_db, session_maker
from treg.models import AffiliateBonus, AffiliateLead, CreditBlock, LedgerEntry, Referral
from treg.timeutil import utcnow_naive

FLAT_5_ONCE = {"id": "disc_1", "type": "flat", "amount": 500, "maxDuration": 0}


@pytest.fixture
async def c(monkeypatch):
    await reset_db()
    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", "sk_test_suite", raising=False)
    monkeypatch.setattr(s, "stripe_webhook_secret", WHSEC, raising=False)
    monkeypatch.setattr(s, "dub_api_key", "dub_test_suite", raising=False)
    monkeypatch.setattr(s, "topup_bonus_tiers", {}, raising=False)
    monkeypatch.setattr(affiliates, "_http_client", lambda: httpx.AsyncClient(transport=_unreachable))
    billing._locks.clear()
    billing._scheduled.clear()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no Dub in the test suite", request=request)


_unreachable = httpx.MockTransport(_refuse)


def _dub(discount: dict | None = None, *, status: int = 200, seen: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": "down"})
        return httpx.Response(200, json=[{"externalId": "x", "partner": {"id": "pn_1"}, "discount": discount}])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _team(c: AsyncClient, email: str, *, lead: str = "verified", headers: dict | None = None) -> int:
    r = await verified_signup(c, json={"email": email}, headers=headers)
    assert r.status_code == 200, r.text
    org_id = r.json()["org_id"]
    if lead != "none":
        now = utcnow_naive()
        async with session_maker() as db:
            db.add(AffiliateLead(org_id=org_id, user_id=r.json()["id"],
                                 partner_code="jane", country="US", partner_id="pn_1",
                                 uploaded_at=now if lead != "pending" else None,
                                 verified_at=now if lead == "verified" else None,
                                 failed_at=now if lead == "failed" else None,
                                 error="self_referral" if lead == "failed" else ""))
            await db.commit()
    return org_id


async def _topup(c: AsyncClient, monkeypatch, org_id: int, pi: str, cents: int = 1000,
                 fingerprint: str | None = "fp_card_A"):
    monkeypatch.setattr(billing, "_sdk", _fake_sdk(fingerprint))
    r = await _deliver(c, _checkout_event(org_id, pi=pi, cents=cents))
    assert r.status_code == 200, r.text


async def _decide(discount: dict | None = FLAT_5_ONCE, *, card: str | None = "fp_card_A",
                  stripe_down: bool = False, **kw) -> dict:
    async def stripe_call(op, **params):
        if stripe_down:
            raise RuntimeError("stripe down")
        return {"id": params.get("id"), "payment_method": {"id": "pm_1", "card": {"fingerprint": card}}}
    async with _dub(discount, **kw) as client:
        return await affiliate_bonus.drain_once(client, session_factory=session_maker, stripe_call=stripe_call)


async def _rows() -> list[AffiliateBonus]:
    async with session_maker() as db:
        return list((await db.execute(select(AffiliateBonus).order_by(AffiliateBonus.id))).scalars().all())


async def _bonus_blocks(org_id: int) -> list[CreditBlock]:
    async with session_maker() as db:
        return list((await db.execute(select(CreditBlock).where(
            CreditBlock.org_id == org_id, CreditBlock.kind == "bonus"))).scalars().all())


def test_offers_are_read_only_when_well_formed():
    assert dub._discount(FLAT_5_ONCE) == dub.Discount("disc_1", "flat", 500.0, 0)
    assert dub._discount({"type": "fixed", "amount": 5, "maxDuration": None}).kind == "flat"
    assert dub._discount({"type": "percentage", "amount": 20, "maxDuration": 6}).max_duration == 6
    for bad in (None, {}, {"type": "bogo", "amount": 5}, {"type": "flat", "amount": 0},
                {"type": "flat", "amount": "5"}, {"type": "flat", "amount": True},
                {"type": "percentage", "amount": 20, "maxDuration": -1}):
        assert dub._discount(bad) is None


async def test_a_fixed_offer_pays_once_on_the_first_ten_dollars(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _decide() == {"bonus_granted": 1}
    row = (await _rows())[0]
    assert (row.outcome, row.offer, row.bonus_micro) == ("granted", "flat:500:0", 5_000_000)
    blocks = await _bonus_blocks(org_id)
    assert [b.amount_micro for b in blocks] == [5_000_000] and blocks[0].id == row.block_id
    await _topup(c, monkeypatch, org_id, "pi_2")
    assert await _decide() == {"bonus_already_granted": 1}
    assert len(await _bonus_blocks(org_id)) == 1


async def test_a_fixed_offer_counts_small_top_ups_together(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1", cents=500)
    assert await _decide() == {"bonus_below_minimum": 1}
    await _topup(c, monkeypatch, org_id, "pi_2", cents=500)
    assert await _decide() == {"bonus_granted": 1}


async def test_one_card_earns_a_fixed_offer_once(c, monkeypatch):
    first = await _team(c, "bob@superdesign.dev")
    second = await _team(c, "carl@superdesign.dev")
    await _topup(c, monkeypatch, first, "pi_1")
    await _topup(c, monkeypatch, second, "pi_2")
    assert await _decide() == {"bonus_granted": 1, "bonus_card_already_used": 1}


async def test_a_once_percentage_offer_pays_the_first_qualifying_top_up(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    offer = {"id": "disc_2", "type": "percentage", "amount": 20, "maxDuration": 0}
    await _topup(c, monkeypatch, org_id, "pi_small", cents=500)
    await _topup(c, monkeypatch, org_id, "pi_1", cents=5000)
    assert await _decide(offer) == {"bonus_below_minimum": 1, "bonus_granted": 1}
    assert [b.amount_micro for b in await _bonus_blocks(org_id)] == [10_000_000]
    await _topup(c, monkeypatch, org_id, "pi_2", cents=5000)
    assert await _decide(offer) == {"bonus_offer_ended": 1}


async def test_a_percentage_offer_pays_every_top_up_for_its_months(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    offer = {"id": "disc_3", "type": "percentage", "amount": 20, "maxDuration": 1}
    await _topup(c, monkeypatch, org_id, "pi_1")
    await _topup(c, monkeypatch, org_id, "pi_2")
    assert await _decide(offer) == {"bonus_granted": 2}
    async with session_maker() as db:
        first = (await db.execute(select(CreditBlock).where(CreditBlock.org_id == org_id,
                                                            CreditBlock.kind == "purchased"))).scalars().first()
        first.created_at -= timedelta(days=40)
        db.add(first)
        await db.commit()
    await _topup(c, monkeypatch, org_id, "pi_3")
    assert await _decide(offer) == {"bonus_offer_ended": 1}


async def test_one_grant_is_capped(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _decide({"id": "typo", "type": "flat", "amount": 50000, "maxDuration": 0}) == {"bonus_granted": 1}
    assert [b.amount_micro for b in await _bonus_blocks(org_id)] == [get_settings().affiliate_bonus_max_micro]


async def test_no_offer_in_dub_pays_nothing(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _decide(None) == {"bonus_no_offer": 1}
    assert await _bonus_blocks(org_id) == []


async def test_a_team_no_partner_brought_stages_nothing(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev", lead="none")
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _rows() == []


async def test_nothing_is_staged_while_dub_is_off(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    monkeypatch.setattr(get_settings(), "dub_api_key", "", raising=False)
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _rows() == []


async def test_a_self_referral_found_later_pays_nothing(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev", lead="pending")
    await _topup(c, monkeypatch, org_id, "pi_1")
    async with session_maker() as db:
        lead = (await db.execute(select(AffiliateLead))).scalars().one()
        lead.failed_at, lead.error = utcnow_naive(), "self_referral"
        db.add(lead)
        await db.commit()
    assert await _decide() == {"bonus_not_attributed": 1}


async def test_a_lead_not_verified_yet_waits_then_pays(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev", lead="pending")
    await _topup(c, monkeypatch, org_id, "pi_1")
    seen: list = []
    assert await _decide(seen=seen) == {"bonus_retried": 1}
    assert seen == [], "Dub is asked only for a verified team"
    async with session_maker() as db:
        lead = (await db.execute(select(AffiliateLead))).scalars().one()
        lead.verified_at = utcnow_naive()
        db.add(lead)
        row = (await db.execute(select(AffiliateBonus))).scalars().one()
        row.next_attempt_at = None
        db.add(row)
        await db.commit()
    assert await _decide() == {"bonus_granted": 1}


async def test_a_dub_outage_retries_and_a_week_later_gives_up(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _decide(status=503) == {"bonus_retried": 1}
    row = (await _rows())[0]
    assert row.decided_at is None and row.next_attempt_at is not None
    async with session_maker() as db:
        row = await db.get(AffiliateBonus, row.id)
        row.next_attempt_at, row.created_at = None, row.created_at - timedelta(days=8)
        db.add(row)
        await db.commit()
    assert await _decide(status=503) == {"bonus_expired": 1}


async def test_a_team_the_friend_referral_rewards_gets_no_partner_bonus(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    async with session_maker() as db:
        db.add(Referral(code="ann1", referrer_user_id=1, referred_user_id=1, referred_org_id=org_id,
                        status="paid"))
        await db.commit()
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _decide() == {"bonus_friend_referral": 1}


async def test_a_payment_refunded_before_the_decision_earns_nothing(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")
    async with session_maker() as db:
        await billing.revoke_refunded(db, "pi_1", 10_000_000, source="test")
    assert await _decide() == {"bonus_refunded": 1}


async def test_a_refund_after_the_grant_revokes_the_bonus(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _decide() == {"bonus_granted": 1}
    async with session_maker() as db:
        await billing.revoke_refunded(db, "pi_1", 10_000_000, source="test")
    async with session_maker() as db:
        revoked = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.kind == "revoke", LedgerEntry.block_id == (await _bonus_blocks(org_id))[0].id))).scalars().all()
    assert revoked, "the refund takes the bonus back with the payment"
    assert (await _bonus_blocks(org_id))[0].remaining_micro == 0


async def test_two_drains_never_decide_one_payment_twice(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert await _decide() == {"bonus_granted": 1}
    assert await _decide() == {}
    assert len(await _bonus_blocks(org_id)) == 1


async def test_the_affiliate_drain_decides_the_offer(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")

    async def stripe_call(op, **kw):
        return {}
    async with _dub(FLAT_5_ONCE) as client:
        result = await affiliates.drain_once(client, session_factory=session_maker, stripe_call=stripe_call)
    assert result["bonus_granted"] == 1


async def test_the_bonus_row_commits_with_the_credit_or_not_at_all(c, monkeypatch):
    """The outbox row is staged in the credit's own transaction: a staging failure leaves no credit
    either, so Stripe's redelivery credits and stages both, and no paid top-up loses its bonus."""
    org_id = await _team(c, "bob@superdesign.dev")
    real_queue = affiliate_bonus.queue

    async def broken(*a, **kw):
        raise RuntimeError("database hiccup")
    monkeypatch.setattr(affiliate_bonus, "queue", broken)
    monkeypatch.setattr(billing, "_sdk", _fake_sdk("fp_card_A"))
    r = await _deliver(c, _checkout_event(org_id, pi="pi_1"))
    assert r.status_code >= 500, "a failed delivery makes Stripe redeliver"
    async with session_maker() as db:
        assert (await db.execute(select(CreditBlock).where(CreditBlock.org_id == org_id,
                                                           CreditBlock.kind == "purchased"))).first() is None
    assert await _rows() == []
    monkeypatch.setattr(affiliate_bonus, "queue", real_queue)
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert [r.payment_intent for r in await _rows()] == ["pi_1"]
    await _topup(c, monkeypatch, org_id, "pi_1")
    assert len(await _rows()) == 1, "a redelivery stages nothing more"


async def test_a_payment_staged_without_its_card_reads_it_from_stripe(c, monkeypatch):
    """`payment_intent.succeeded` can credit before the Checkout completion, with no card: the
    per-card rule still holds, because the worker reads the card from Stripe."""
    first = await _team(c, "bob@superdesign.dev")
    second = await _team(c, "carl@superdesign.dev")
    await _topup(c, monkeypatch, first, "pi_1", fingerprint=None)
    await _topup(c, monkeypatch, second, "pi_2", fingerprint=None)
    assert await _decide(card="fp_card_A") == {"bonus_granted": 1, "bonus_card_already_used": 1}
    assert [r.fingerprint for r in await _rows()] == ["fp_card_A", "fp_card_A"]


async def test_a_card_stripe_cannot_read_retries(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1", fingerprint=None)
    assert await _decide(stripe_down=True) == {"bonus_retried": 1}
    assert await _bonus_blocks(org_id) == []


async def test_a_refunded_purchase_never_counts_toward_the_minimum(c, monkeypatch):
    org_id = await _team(c, "bob@superdesign.dev")
    await _topup(c, monkeypatch, org_id, "pi_1")
    async with session_maker() as db:
        await billing.revoke_refunded(db, "pi_1", 10_000_000, source="test")
    await _topup(c, monkeypatch, org_id, "pi_2", cents=500)
    assert await _decide() == {"bonus_refunded": 1, "bonus_below_minimum": 1}
    assert await _bonus_blocks(org_id) == []
