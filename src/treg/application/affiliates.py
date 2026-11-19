"""The affiliate program on Dub Partners (docs/context/architecture/affiliates.md).

treg records which teams arrived through an affiliate's Dub link and sends Dub one lead per team.
Sales, refunds, commissions and payouts are Dub's: its Stripe app reads the top-ups of a customer
tagged with `dubCustomerExternalId`. The lead is durable (an `AffiliateLead` row written with the
team) and only the upload is asynchronous, so a Dub outage never touches a signup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import or_, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..config import get_settings
from ..domain.identity.access import _is_machine_email
from ..domain.identity.promotions import promo_inbox
from ..infra import dub
from ..infra import stripe as stripe_adapter
from ..infra.db import background_session_maker
from ..models import AffiliateLead, Membership, Org, User
from ..timeutil import utcnow_naive

log = logging.getLogger("treg")

STRIPE_EXTERNAL_ID_KEY = "dubCustomerExternalId"
DRAIN_INTERVAL_S = 30
_BATCH = 50
_RETRY_BASE_S = 60
_RETRY_CAP_S = 24 * 3600
_CLAIM_S = 300
CHECK_NOW_S = 8.0
SELF_REFERRAL = "self_referral"
_http_client = httpx.AsyncClient  # the payment-time check's client; tests swap in a fake Dub


def enabled() -> bool:
    return bool(get_settings().dub_api_key)


def external_id(org_id: int) -> str:
    return str(org_id)


def country_of(raw: str) -> str:
    """The country Dub records for a code-only lead: the request's, else the configured fallback."""
    return dub.valid_country(raw) or dub.valid_country(get_settings().dub_default_country) or "US"


def stage_lead(db: AsyncSession, *, org: Org, user: User, click_id: str = "",
               partner_code: str = "", country: str = "") -> bool:
    """Add the team's lead to the caller's transaction. False when there is nothing to record.
    A link click wins over a typed partner code."""
    click_id = dub.valid_click_id(click_id)
    partner_code = "" if click_id else dub.valid_partner_code(partner_code)
    if not enabled() or not (click_id or partner_code) or org.id is None or org.demo or org.public_demo:
        return False
    db.add(AffiliateLead(org_id=org.id, user_id=user.id, click_id=click_id or None,
                         partner_code=partner_code or None,
                         country=country_of(country) if partner_code else ""))
    return True


async def attributed(db: AsyncSession, org_id: int | None) -> bool:
    """A verified lead: Dub recorded it and its partner is not the team's own owner. A lead still in
    flight, dead-lettered, or found to be a self-referral attributes nothing."""
    if not enabled() or org_id is None:
        return False
    row = await db.execute(select(AffiliateLead.id).where(
        AffiliateLead.org_id == org_id, AffiliateLead.verified_at.is_not(None),
        AffiliateLead.failed_at.is_(None)).limit(1))
    return row.first() is not None


async def brought_before(db: AsyncSession, user_id: int | None) -> bool:
    """A partner already brought this person: a partner earns on the person they brought, so a
    later team the same person makes is not a second lead."""
    if user_id is None:
        return False
    row = await db.execute(select(AffiliateLead.id).where(AffiliateLead.user_id == user_id).limit(1))
    return row.first() is not None


async def stripe_metadata(db: AsyncSession, org_id: int | None) -> dict[str, str]:
    """What tells Dub's Stripe app whose top-ups these are. Empty for a team no verified affiliate
    brought. Dub credits only a payment that carries the tag when it is made, so a lead still waiting
    for its check is checked here first, before the caller creates the Stripe object. Reads and
    writes on the caller's session and ends its transaction before Dub is called; the caller ends
    it again before it calls Stripe."""
    if not enabled() or org_id is None:
        return {}
    if not await attributed(db, org_id):
        await _check_now(db, org_id)
    if await attributed(db, org_id):
        return {STRIPE_EXTERNAL_ID_KEY: external_id(org_id)}
    return {}


def _retry_delay(attempts: int) -> timedelta:
    exponent = min(max(attempts - 1, 0), 12)
    return timedelta(seconds=min(_RETRY_BASE_S * (2 ** exponent), _RETRY_CAP_S))


@dataclass(frozen=True)
class _Due:
    id: int
    org_id: int
    click_id: str
    email: str
    partner_code: str
    country: str
    recorded: bool
    partner_id: str


def _open(now):
    return (AffiliateLead.verified_at.is_(None), AffiliateLead.failed_at.is_(None),
            or_(AffiliateLead.next_attempt_at.is_(None), AffiliateLead.next_attempt_at <= now))


async def _owner_inboxes(db: AsyncSession, org_ids: set[int]) -> dict[int, set[str]]:
    rows = (await db.execute(
        select(Membership.org_id, User.email).join(User, User.id == Membership.user_id)
        .where(Membership.org_id.in_(org_ids), Membership.role == "owner"))).all()
    inboxes: dict[int, set[str]] = {}
    for org_id, email in rows:
        if email:
            inboxes.setdefault(org_id, set()).add(promo_inbox(email))
    return inboxes


async def _due(db: AsyncSession, now, *, org_id: int | None = None
               ) -> tuple[list[_Due], dict[int, set[str]]]:
    """Leads Dub has not recorded yet, and recorded leads whose partner check has not resolved,
    each claimed for this caller, with their teams' owner inboxes. Every control process runs a
    worker and a checkout may check its own team's lead: a lead is worked by the one whose
    conditional update moved its `next_attempt_at` past the claim window. Commits."""
    query = (select(AffiliateLead.id, AffiliateLead.org_id, AffiliateLead.click_id, User.email,
                    AffiliateLead.partner_code, AffiliateLead.country, AffiliateLead.uploaded_at,
                    AffiliateLead.partner_id)
             .join(User, User.id == AffiliateLead.user_id, isouter=True)
             .where(*_open(now))
             .order_by(AffiliateLead.created_at, AffiliateLead.id)
             .limit(_BATCH))
    if org_id is not None:
        query = query.where(AffiliateLead.org_id == org_id)
    rows = (await db.execute(query)).all()
    claimed: list = []
    for r in rows:
        won = await db.execute(update(AffiliateLead)
                               .where(AffiliateLead.id == r[0], *_open(now))
                               .values(next_attempt_at=now + timedelta(seconds=_CLAIM_S)))
        if won.rowcount == 1:
            claimed.append(r)
    owners = await _owner_inboxes(db, {r[1] for r in claimed}) if claimed else {}
    await db.commit()
    return [_Due(r[0], r[1], r[2] or "", r[3] or "", r[4] or "", r[5] or "", r[6] is not None,
                 r[7] or "") for r in claimed], owners


def _is_self(partner: dub.Outcome, inboxes: set[str]) -> bool:
    """The partner's own inbox owns or created the team: Gmail dots and `+tag` aliases count as one
    inbox, the same rule as the one-per-inbox signup credit."""
    return bool(partner.ok and partner.email and promo_inbox(partner.email) in inboxes)


@dataclass(frozen=True)
class _Result:
    """One lead's Dub round: `recorded` once Dub holds the lead, `verified` once its partner is known
    not to be the team's owner, `self_referral` when it is."""
    outcome: dub.Outcome
    recorded: bool = False
    partner_id: str = ""
    verified: bool = False
    self_referral: bool = False


def _pending_check(partner: dub.Outcome) -> dub.Outcome:
    return dub.Outcome(ok=False, retry=True, status=partner.status,
                       detail=f"partner lookup: {partner.detail}"[:300])


async def _check(client, key: str, partner_id: str, inboxes: set[str], *,
                 recorded: bool) -> _Result:
    partner = await dub.partner_email(client, api_key=key, partner_id=partner_id)
    if not partner.ok:
        return _Result(_pending_check(partner), recorded=recorded, partner_id=partner_id)
    if _is_self(partner, inboxes):
        return _Result(partner, recorded=recorded, partner_id=partner_id, self_referral=True)
    return _Result(partner, recorded=recorded, partner_id=partner_id, verified=True)


async def _check_recorded_click(client, key: str, lead: _Due, inboxes: set[str]) -> _Result:
    """A click lead Dub holds but whose partner it did not name: a deduplicated repeat, such as the
    retry after a reply that timed out. Dub's customer record names the partner it credited."""
    found = await dub.customer_partner(client, api_key=key,
                                       customer_external_id=external_id(lead.org_id))
    if not found.ok:
        return _Result(_pending_check(found), recorded=True)
    if not found.partner_id:
        return _Result(found, recorded=True, verified=True)
    return await _check(client, key, found.partner_id, inboxes, recorded=True)


async def _send(client, settings, lead: _Due, *, email: str, inboxes: set[str]) -> _Result:
    """One lead's Dub calls. A recorded lead whose partner check failed is only re-checked."""
    key = settings.dub_api_key
    if lead.recorded:
        if lead.partner_id:
            return await _check(client, key, lead.partner_id, inboxes, recorded=True)
        return await _check_recorded_click(client, key, lead, inboxes)
    if lead.click_id:
        sent = await dub.track_lead(
            client, api_key=key, click_id=lead.click_id,
            customer_external_id=external_id(lead.org_id), event_name=settings.dub_lead_event,
            customer_email=email)
        if not sent.ok:
            return _Result(sent)
        if not sent.partner_id:
            return await _check_recorded_click(client, key, lead, inboxes)
        return await _check(client, key, sent.partner_id, inboxes, recorded=True)
    found = await dub.partner_for_code(client, api_key=key, link_domain=settings.dub_link_domain,
                                       code=lead.partner_code)
    if not found.ok:
        return _Result(found)
    checked = await _check(client, key, found.partner_id, inboxes, recorded=False)
    if not checked.verified:
        return checked
    made = await dub.lead_for_partner(
        client, api_key=key, partner_id=found.partner_id,
        customer_external_id=external_id(lead.org_id), country=country_of(lead.country),
        customer_email=email)
    return _Result(made, recorded=made.ok, partner_id=found.partner_id, verified=made.ok)


async def _send_all(client, due: list[_Due], owners: dict[int, set[str]]) -> dict[int, _Result]:
    settings = get_settings()
    results: dict[int, _Result] = {}
    for lead in due:
        inboxes = set(owners.get(lead.org_id, set()))
        if lead.email:
            inboxes.add(promo_inbox(lead.email))
        email = "" if _is_machine_email(lead.email) else lead.email
        results[lead.id] = await _send(client, settings, lead, email=email, inboxes=inboxes)
    return results


async def _record(db: AsyncSession, results: dict[int, _Result], now) -> dict[str, int]:
    """Write each lead's Dub round into its row. The caller commits."""
    sent = retried = failed = 0
    rows = (await db.execute(
        select(AffiliateLead).where(AffiliateLead.id.in_(results)))).scalars().all()
    for row in rows:
        result = results[row.id]
        outcome = result.outcome
        row.attempts += 1
        if result.recorded and row.uploaded_at is None:
            row.uploaded_at = now
        if result.partner_id:
            row.partner_id = result.partner_id
        if result.self_referral:
            # A rejection outranks any verification and asks `_sync_stripe` to untag again.
            row.failed_at, row.next_attempt_at, row.error = now, None, SELF_REFERRAL
            row.verified_at, row.stripe_synced_at = None, None
            failed += 1
        elif row.error == SELF_REFERRAL:
            failed += 1
        elif result.verified:
            row.verified_at, row.next_attempt_at, row.error = now, None, ""
            sent += 1
        elif outcome.retry:
            row.next_attempt_at = now + _retry_delay(row.attempts)
            row.error = f"{outcome.status}: {outcome.detail}"[:300]
            retried += 1
        else:
            row.failed_at, row.next_attempt_at = now, None
            row.error = f"{outcome.status}: {outcome.detail}"[:300]
            failed += 1
        db.add(row)
    return {"sent": sent, "retried": retried, "failed": failed}


async def _check_now(db: AsyncSession, org_id: int) -> None:
    """Resolve the team's own pending lead inside a payment request, bounded by `CHECK_NOW_S`. A lead
    another process holds, one waiting out a retry, or a Dub that does not answer in time is left
    to the drain; that payment then goes untagged. No connection is held while Dub is called."""
    due, owners = await _due(db, utcnow_naive(), org_id=org_id)
    if not due:
        return
    try:
        async with _http_client() as client:
            results = await asyncio.wait_for(_send_all(client, due, owners), CHECK_NOW_S)
    except (asyncio.TimeoutError, httpx.HTTPError) as exc:
        log.warning("affiliate lead check for org %s left to the drain: %s", org_id, exc)
        return
    await _record(db, results, utcnow_naive())
    await db.commit()


def _sync_due(now):
    return (AffiliateLead.stripe_synced_at.is_(None),
            or_(AffiliateLead.verified_at.is_not(None), AffiliateLead.error == SELF_REFERRAL),
            or_(AffiliateLead.next_attempt_at.is_(None), AffiliateLead.next_attempt_at <= now))


async def _sync_stripe(*, session_factory, stripe_call) -> dict:
    """Bring each resolved lead's existing Stripe customer in line with it: tag it when the lead is
    verified, untag it on a self-referral. A lead whose team has no customer yet stays unsynced, so
    a customer billing creates while the lead is being verified is still picked up on a later drain.
    A Stripe refusal of the request itself (a deleted customer) settles the row; any other failure
    waits out a growing delay, so a few broken customers never hold up the rest.
    No database connection is held during the Stripe calls."""
    key = get_settings().stripe_secret_key
    if not key:
        return {"synced": 0}
    now = utcnow_naive()
    async with session_factory() as db:
        rows = (await db.execute(
            select(AffiliateLead.id, AffiliateLead.org_id, AffiliateLead.verified_at,
                   Org.stripe_customer_id)
            .join(Org, Org.id == AffiliateLead.org_id)
            .where(Org.stripe_customer_id.is_not(None), *_sync_due(now))
            .order_by(AffiliateLead.id)
            .limit(_BATCH))).all()
    if not rows:
        return {"synced": 0}
    done: list[int] = []
    refused: dict[int, str] = {}
    later: list[int] = []
    for row_id, org_id, verified_at, customer_id in rows:
        value = external_id(org_id) if verified_at is not None else ""
        try:
            await stripe_call(stripe_adapter.CUSTOMER_MODIFY, api_key=key, id=customer_id,
                              metadata={STRIPE_EXTERNAL_ID_KEY: value})
        except stripe_adapter.InvalidRequestError as exc:
            log.warning("affiliate stripe sync refused for org %s: %s", org_id, exc)
            refused[row_id] = f"stripe: {exc}"[:300]
            continue
        except Exception as exc:  # noqa: BLE001 - retried after a delay
            log.warning("affiliate stripe sync failed for org %s: %s", org_id, exc)
            later.append(row_id)
            continue
        done.append(row_id)
    now = utcnow_naive()
    async with session_factory() as db:
        for row in (await db.execute(select(AffiliateLead).where(
                AffiliateLead.id.in_(done + list(refused) + later)))).scalars().all():
            if row.id in later:
                row.attempts += 1
                row.next_attempt_at = now + _retry_delay(row.attempts)
            else:
                row.stripe_synced_at = now
                if row.id in refused and row.error != SELF_REFERRAL:
                    row.error = refused[row.id]
            db.add(row)
        await db.commit()
    return {"synced": len(done), "sync_refused": len(refused), "sync_later": len(later)}


async def drain_once(client, *, session_factory, stripe_call=stripe_adapter.call) -> dict:
    """Upload the due leads and resolve their partner check, then sync Stripe. No database connection
    is held while Dub or Stripe is being called.

    A lead whose partner is the team's own creator or owner is dead-lettered as `self_referral`, so
    the team's Stripe customer is never tagged (and untagged if it ever was). A recorded lead whose
    partner lookup fails stays pending and is re-checked with backoff, never acknowledged."""
    if not enabled():
        return {"sent": 0, "reason": "disabled"}
    async with session_factory() as db:
        due, owners = await _due(db, utcnow_naive())
    counts = {"sent": 0, "retried": 0, "failed": 0}
    if due:
        results = await _send_all(client, due, owners)
        async with session_factory() as db:
            counts = await _record(db, results, utcnow_naive())
            await db.commit()
    synced = await _sync_stripe(session_factory=session_factory, stripe_call=stripe_call)
    return {**counts, **synced}


async def worker(client) -> None:
    """Drain forever from `lifespan`; a failure here must never take the server down."""
    while True:
        try:
            result = await drain_once(client, session_factory=background_session_maker)
            if any(result.get(k) for k in ("retried", "failed", "sync_refused", "sync_later")):
                log.warning("affiliate lead drain incomplete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad batch must not kill the loop
            log.warning("affiliate lead drain failed: %s", exc)
        await asyncio.sleep(DRAIN_INTERVAL_S)
