"""The partner offer: bonus credit on a paid top-up of a team a partner brought
(docs/context/architecture/affiliates.md "Partner offer").

The offer itself lives in Dub, set by the program owner with the Custom discount provider on the
program's group, a group, one partner or one link; Dub resolves which applies to a customer. treg
pays it as a `bonus` credit block, never as a Stripe price change. A payment is staged with its
credit (`queue`) and decided later by the affiliate worker (`drain_once`), which reads the offer from
Dub with no connection held, so the webhook never waits on Dub and a Dub outage only delays a bonus.

The rules treg adds to whatever Dub says:
- a fixed offer is paid once per team, when its paid top-ups first reach the friend referral's
  minimum (`referral_min_topup_micro`), and once per card;
- a percentage offer is paid on each top-up of at least that minimum, for the offer's duration in
  months from the team's first paid top-up (0 = the first such top-up only, none = every one);
- one grant never exceeds `affiliate_bonus_max_micro`;
- a team the friend referral already rewards gets nothing more here, and a refunded payment earns
  nothing.
The grant names its payment, so a refund later revokes it in proportion like the top-up bonus
(`billing.revoke_refunded`).
"""

from __future__ import annotations

import calendar
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..config import get_settings
from ..domain import money as ledger
from ..infra import dub
from ..models import AffiliateBonus, AffiliateLead, CreditBlock, LedgerEntry, Referral
from ..timeutil import utcnow_naive

log = logging.getLogger("treg")

GRANTED = "granted"
_BATCH = 50
_CLAIM_S = 120
_RETRY_BASE_S = 60
_RETRY_CAP_S = 24 * 3600
# How long a payment waits for its team's lead to be verified, or for Dub to answer, before it is
# decided `expired`. The lead drain retries for longer, but a bonus that lands a week late is not
# the offer anyone saw.
GIVE_UP_AFTER = timedelta(days=7)
# The friend referral states that reward the team; a team in one of them gets no partner bonus.
_FRIEND_REWARDED = ("pending", "qualified", "paid")


def enabled() -> bool:
    return bool(get_settings().dub_api_key)


async def queue(db: AsyncSession, *, org_id: int, payment_intent: str, amount_micro: int,
                fingerprint: str | None = None) -> bool:
    """Stage a paid top-up for a partner-brought team (a lead not dead-lettered). The caller commits.
    False when there is nothing to stage."""
    if not enabled() or not payment_intent or amount_micro <= 0:
        return False
    lead = (await db.execute(select(AffiliateLead.id).where(
        AffiliateLead.org_id == org_id, AffiliateLead.failed_at.is_(None)).limit(1))).first()
    if lead is None:
        return False
    db.add(AffiliateBonus(org_id=org_id, payment_intent=payment_intent, topup_micro=int(amount_micro),
                          fingerprint=fingerprint or None))
    return True


@dataclass(frozen=True)
class _Due:
    id: int
    org_id: int
    created_at: datetime
    verified: bool
    attributed: bool  # a lead exists and is not dead-lettered


def _open(now):
    return (AffiliateBonus.decided_at.is_(None),
            or_(AffiliateBonus.next_attempt_at.is_(None), AffiliateBonus.next_attempt_at <= now))


def _retry_delay(attempts: int) -> timedelta:
    exponent = min(max(attempts - 1, 0), 12)
    return timedelta(seconds=min(_RETRY_BASE_S * (2 ** exponent), _RETRY_CAP_S))


async def _claim(db: AsyncSession, now) -> list[_Due]:
    """Due payments, each claimed for this worker by a conditional update, as the lead drain does.
    Commits."""
    rows = (await db.execute(
        select(AffiliateBonus.id, AffiliateBonus.org_id, AffiliateBonus.created_at,
               AffiliateLead.verified_at, AffiliateLead.failed_at, AffiliateLead.id)
        .join(AffiliateLead, AffiliateLead.org_id == AffiliateBonus.org_id, isouter=True)
        .where(*_open(now))
        .order_by(AffiliateBonus.id)
        .limit(_BATCH))).all()
    claimed = []
    for bonus_id, org_id, created_at, verified_at, failed_at, lead_id in rows:
        won = await db.execute(update(AffiliateBonus).where(AffiliateBonus.id == bonus_id, *_open(now))
                               .values(next_attempt_at=now + timedelta(seconds=_CLAIM_S)))
        if won.rowcount == 1:
            claimed.append(_Due(bonus_id, org_id, created_at, verified_at is not None and failed_at is None,
                                lead_id is not None and failed_at is None))
    await db.commit()
    return claimed


async def drain_once(client, *, session_factory) -> dict:
    """Decide the due payments: read each verified team's offer from Dub, then grant or record why
    not. No database connection is held while Dub is called."""
    if not enabled():
        return {}
    async with session_factory() as db:
        due = await _claim(db, utcnow_naive())
    if not due:
        return {}
    key = get_settings().dub_api_key
    offers: dict[int, dub.Outcome] = {}
    for d in due:
        if d.verified and d.org_id not in offers:
            offers[d.org_id] = await dub.customer_partner(client, api_key=key,
                                                          customer_external_id=str(d.org_id))
    counts: dict[str, int] = {}
    for d in due:
        async with session_factory() as db:
            try:
                result = await _settle(db, d, offers.get(d.org_id), utcnow_naive())
            except Exception as exc:  # noqa: BLE001 - one bad payment must not stop the rest
                await db.rollback()
                log.warning("affiliate bonus for payment row %s failed: %s", d.id, exc)
                result = "bonus_error"
        counts[result] = counts.get(result, 0) + 1
    return counts


async def _settle(db: AsyncSession, d: _Due, offer: dub.Outcome | None, now) -> str:
    """Decide one payment and record it; grants in the same transaction. Commits. Returns a count key."""
    if not d.attributed:
        return await _decide(db, d.id, "not_attributed", now)
    waited_out = now - d.created_at > GIVE_UP_AFTER
    if not d.verified:
        if waited_out:
            return await _decide(db, d.id, "expired", now, error="lead never verified")
        return await _later(db, d.id, now, "lead not verified yet")
    if offer is None or not offer.ok:
        detail = f"{offer.status}: {offer.detail}" if offer else "no answer"
        if waited_out or (offer is not None and not offer.retry):
            return await _decide(db, d.id, "expired", now, error=detail)
        return await _later(db, d.id, now, detail)
    await _lock(db, f"affiliate-bonus-org:{d.org_id}")
    row = (await db.execute(select(AffiliateBonus).where(AffiliateBonus.id == d.id)
                            .execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None or row.decided_at is not None:
        await db.commit()
        return "bonus_skipped"
    if row.fingerprint:
        await _lock(db, f"affiliate-bonus-card:{row.fingerprint}")
    discount = offer.discount
    outcome, bonus = await _evaluate(db, row, discount)
    row.offer = _offer_text(discount)
    if bonus > 0:
        block = await ledger.grant(db, row.org_id, amount_micro=bonus, kind="bonus", once=False,
                                   meta={"source": "affiliate_bonus", "payment_intent": row.payment_intent,
                                         "offer": row.offer, "discount_id": discount.id if discount else ""})
        row.block_id = block.id if block is not None else None
        row.bonus_micro = block.amount_micro if block is not None else 0
    row.outcome, row.decided_at, row.next_attempt_at, row.error = outcome, now, None, ""
    row.attempts += 1
    db.add(row)
    await db.commit()
    return "bonus_granted" if outcome == GRANTED else "bonus_" + outcome


async def _evaluate(db: AsyncSession, row: AffiliateBonus, discount: dub.Discount | None) -> tuple[str, int]:
    s = get_settings()
    minimum = int(s.referral_min_topup_micro)
    if discount is None:
        return "no_offer", 0
    block = await ledger.block_for_payment(db, row.payment_intent)
    if block is None or block.kind != "purchased":
        return "no_payment", 0
    if await _refunded(db, block.id):
        return "refunded", 0
    friend = (await db.execute(select(Referral.id).where(
        Referral.referred_org_id == row.org_id, Referral.status.in_(_FRIEND_REWARDED)).limit(1))).first()
    if friend is not None:
        return "friend_referral", 0
    if discount.kind == "flat":
        if await _granted(db, org_id=row.org_id, kind="flat"):
            return "already_granted", 0
        paid = (await db.execute(select(func.coalesce(func.sum(CreditBlock.amount_micro), 0)).where(
            CreditBlock.org_id == row.org_id, CreditBlock.kind == "purchased"))).scalar_one()
        if int(paid) < minimum:
            return "below_minimum", 0
        if row.fingerprint and await _card_used(db, row.fingerprint, row.org_id):
            return "card_already_used", 0
        bonus = int(Decimal(str(discount.amount)) * 10_000)  # cents to micro-USD
    else:
        if row.topup_micro < minimum:
            return "below_minimum", 0
        if discount.max_duration == 0 and await _granted(db, org_id=row.org_id, kind="percentage"):
            return "offer_ended", 0
        if discount.max_duration:
            first = (await db.execute(select(func.min(CreditBlock.created_at)).where(
                CreditBlock.org_id == row.org_id, CreditBlock.kind == "purchased"))).scalar_one()
            if first is not None and block.created_at >= _add_months(first, discount.max_duration):
                return "offer_ended", 0
        bonus = int(Decimal(row.topup_micro) * Decimal(str(discount.amount)) / 100)
    bonus = min(bonus, int(s.affiliate_bonus_max_micro))
    return (GRANTED, bonus) if bonus > 0 else ("no_offer", 0)


async def _decide(db: AsyncSession, bonus_id: int, outcome: str, now, *, error: str = "") -> str:
    await db.execute(update(AffiliateBonus).where(AffiliateBonus.id == bonus_id, AffiliateBonus.decided_at.is_(None))
                     .values(decided_at=now, outcome=outcome, next_attempt_at=None, error=error[:300],
                             attempts=AffiliateBonus.attempts + 1))
    await db.commit()
    return "bonus_" + outcome


async def _later(db: AsyncSession, bonus_id: int, now, error: str) -> str:
    row = await db.get(AffiliateBonus, bonus_id)
    if row is None or row.decided_at is not None:
        await db.commit()
        return "bonus_skipped"
    row.attempts += 1
    row.next_attempt_at = now + _retry_delay(row.attempts)
    row.error = error[:300]
    db.add(row)
    await db.commit()
    return "bonus_retried"


async def _lock(db: AsyncSession, name: str) -> None:
    """A transaction-scoped Postgres advisory lock, so two workers never decide one team's or one
    card's payments at once (a fixed offer is once per team and per card)."""
    if db.bind.dialect.name != "postgresql":
        return
    key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
    await db.execute(select(func.pg_advisory_xact_lock(key)))


async def _granted(db: AsyncSession, *, org_id: int, kind: str) -> bool:
    row = (await db.execute(select(AffiliateBonus.id).where(
        AffiliateBonus.org_id == org_id, AffiliateBonus.outcome == GRANTED,
        AffiliateBonus.offer.startswith(kind + ":")).limit(1))).first()
    return row is not None


async def _card_used(db: AsyncSession, fingerprint: str, org_id: int) -> bool:
    row = (await db.execute(select(AffiliateBonus.id).where(
        AffiliateBonus.fingerprint == fingerprint, AffiliateBonus.org_id != org_id,
        AffiliateBonus.outcome == GRANTED, AffiliateBonus.offer.startswith("flat:")).limit(1))).first()
    return row is not None


async def _refunded(db: AsyncSession, block_id: str) -> bool:
    row = (await db.execute(select(LedgerEntry.id).where(
        LedgerEntry.kind == "revoke", LedgerEntry.block_id == block_id).limit(1))).first()
    return row is not None


def _offer_text(discount: dub.Discount | None) -> str:
    """`flat:500:0` (cents, months) or `percentage:20:-` (no duration = every payment)."""
    if discount is None:
        return ""
    duration = "-" if discount.max_duration is None else str(discount.max_duration)
    return f"{discount.kind}:{discount.amount:g}:{duration}"


def _add_months(when: datetime, months: int) -> datetime:
    month = when.month - 1 + months
    year, month = when.year + month // 12, month % 12 + 1
    return when.replace(year=year, month=month, day=min(when.day, calendar.monthrange(year, month)[1]))
