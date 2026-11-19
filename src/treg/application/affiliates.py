"""The affiliate program on Dub Partners (docs/context/architecture/affiliates.md).

treg records which teams arrived through an affiliate's Dub link and sends Dub one lead per team.
Sales, refunds, commissions and payouts are Dub's: its Stripe app reads the top-ups of a customer
tagged with `dubCustomerExternalId`. The lead is durable (an `AffiliateLead` row written with the
team) and only the upload is asynchronous, so a Dub outage never touches a signup.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..config import get_settings
from ..domain.identity.access import _is_machine_email
from ..infra import dub
from ..infra.db import background_session_maker
from ..models import AffiliateLead, Org, User
from ..timeutil import utcnow_naive

log = logging.getLogger("treg")

STRIPE_EXTERNAL_ID_KEY = "dubCustomerExternalId"
DRAIN_INTERVAL_S = 30
_BATCH = 50
_RETRY_BASE_S = 60
_RETRY_CAP_S = 24 * 3600


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
    if not enabled() or org_id is None:
        return False
    # A dead-lettered lead (an unknown partner code) never reached Dub, so it attributes nothing.
    row = await db.execute(select(AffiliateLead.id).where(
        AffiliateLead.org_id == org_id, AffiliateLead.failed_at.is_(None)).limit(1))
    return row.first() is not None


async def stripe_metadata(db: AsyncSession, org_id: int | None) -> dict[str, str]:
    """What tells Dub's Stripe app whose top-ups these are. Empty for a team no affiliate brought."""
    if await attributed(db, org_id):
        return {STRIPE_EXTERNAL_ID_KEY: external_id(org_id)}
    return {}


def _retry_delay(attempts: int) -> timedelta:
    exponent = min(max(attempts - 1, 0), 12)
    return timedelta(seconds=min(_RETRY_BASE_S * (2 ** exponent), _RETRY_CAP_S))


async def _due(db: AsyncSession, now) -> list[tuple]:
    rows = (await db.execute(
        select(AffiliateLead.id, AffiliateLead.org_id, AffiliateLead.click_id, User.email,
               AffiliateLead.partner_code, AffiliateLead.country)
        .join(User, User.id == AffiliateLead.user_id, isouter=True)
        .where(AffiliateLead.uploaded_at.is_(None), AffiliateLead.failed_at.is_(None),
               or_(AffiliateLead.next_attempt_at.is_(None), AffiliateLead.next_attempt_at <= now))
        .order_by(AffiliateLead.created_at, AffiliateLead.id)
        .limit(_BATCH))).all()
    return [(r[0], r[1], r[2] or "", r[3] or "", r[4] or "", r[5] or "") for r in rows]


async def drain_once(client, *, session_factory) -> dict:
    """Upload the due leads. No database connection is held while Dub is being called."""
    if not enabled():
        return {"sent": 0, "reason": "disabled"}
    settings = get_settings()
    async with session_factory() as db:
        due = await _due(db, utcnow_naive())
    if not due:
        return {"sent": 0}
    outcomes: dict[int, dub.Outcome] = {}
    for row_id, org_id, click_id, email, partner_code, country in due:
        email = "" if _is_machine_email(email) else email
        if click_id:
            outcomes[row_id] = await dub.track_lead(
                client, api_key=settings.dub_api_key, click_id=click_id,
                customer_external_id=external_id(org_id), event_name=settings.dub_lead_event,
                customer_email=email)
        else:
            outcomes[row_id] = await dub.lead_from_code(
                client, api_key=settings.dub_api_key, link_domain=settings.dub_link_domain,
                code=partner_code, customer_external_id=external_id(org_id),
                country=country_of(country), customer_email=email)
    sent = retried = failed = 0
    now = utcnow_naive()
    async with session_factory() as db:
        rows = (await db.execute(
            select(AffiliateLead).where(AffiliateLead.id.in_(outcomes)))).scalars().all()
        for row in rows:
            outcome = outcomes[row.id]
            row.attempts += 1
            if outcome.ok:
                row.uploaded_at, row.next_attempt_at, row.error = now, None, ""
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
        await db.commit()
    return {"sent": sent, "retried": retried, "failed": failed}


async def worker(client) -> None:
    """Drain forever from `lifespan`; a failure here must never take the server down."""
    while True:
        try:
            result = await drain_once(client, session_factory=background_session_maker)
            if result.get("retried") or result.get("failed"):
                log.warning("affiliate lead drain incomplete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad batch must not kill the loop
            log.warning("affiliate lead drain failed: %s", exc)
        await asyncio.sleep(DRAIN_INTERVAL_S)
