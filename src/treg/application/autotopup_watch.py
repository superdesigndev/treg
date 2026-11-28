"""Paying teams whose own auto top-up settings are refusing their calls, for an operator alert.

A team with auto top-up runs out of balance only when one of its own limits holds the refill: the
monthly cap, the charges-per-hour wait, the wait after a failed charge, the failure limit, a missing
card, or auto top-up switched off. The 402 the agent sees names the setting, but nobody at treg
does. `find_blocked` reads, for each team with a card or a mandate whose balance is below its
threshold and that has bought credit, the refusals of its last two hours and the limit
`attempt_auto_topup` would stop at now. It reads the replica when one is configured, one short
transaction per team, writes nothing and moves no money.

Out of scope: a team above its threshold whose balance is still below one call's price is refused
with no refill (`above_threshold`) and is not reported.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import logging

from sqlalchemy import case, exists, func, literal_column, or_, text
from sqlmodel import select

from ..config import get_settings
from ..infra.db import read_session_maker
from ..models import CallRecord, LedgerEntry, Org
from . import billing

log = logging.getLogger(__name__)

_TOPUP = literal_column("'topup'")
_STATEMENT_TIMEOUT_MS = 20_000
_SETTLE = literal_column("'settle'")

LIMIT_OFF = "off"
LIMIT_NO_CONSENT = "no_consent"
LIMIT_NO_CARD = "no_card"
LIMIT_FAILURES = "card_failures"
LIMIT_RETRY_WAIT = "retry_wait"
LIMIT_PER_HOUR = "per_hour"
LIMIT_MONTHLY_CAP = "monthly_cap"
LIMIT_REFILL_DUE = "refill_due"


@dataclass
class Blocked:
    org_id: int
    name: str
    owner_email: str
    limit: str
    detail: str
    refused_last_hour: int
    refused_hour_before: int
    spent_last_hour_micro: int
    settled_last_hour: int
    balance_micro: int
    min_refusals: int = 1

    @property
    def new(self) -> bool:
        """It was not reported the hour before: that hour stayed below the reporting floor."""
        return self.refused_hour_before < self.min_refusals

    def turned_away_micro(self) -> int:
        """Refusals times the team's mean settled price in the last hour: an upper bound on the spend
        refused, since an agent retries a refused call."""
        if not self.settled_last_hour:
            return 0
        return self.refused_last_hour * self.spent_last_hour_micro // self.settled_last_hour


def _usd(micro: int) -> str:
    return f"${micro / 1_000_000:,.2f}"


def blocking_limit(org: Org, *, now: datetime, month_spend_micro: int) -> tuple[str, str]:
    """The guard of `billing.attempt_auto_topup` that holds this team's refill now, in its order,
    with the team's own numbers."""
    prefs = billing.autotopup_prefs(org)
    if not org.autotopup_enabled:
        return LIMIT_OFF, f"auto top-up is off ({org.autotopup_disabled_reason or 'turned off by the team'})"
    if not org.autotopup_consented_at:
        return LIMIT_NO_CONSENT, "no recorded consent for automatic charges"
    if not (org.stripe_customer_id and org.stripe_default_pm):
        return LIMIT_NO_CARD, "no saved card"
    failures = int(org.autotopup_failures or 0)
    if failures >= get_settings().autotopup_max_attempts:
        return LIMIT_FAILURES, f"{failures} failed charges in a row"
    last = org.autotopup_last_attempt_at
    wait = billing.autotopup_wait_s(org)
    if last is not None and (now - last).total_seconds() < wait:
        next_at = (last + timedelta(seconds=wait)).strftime("%H:%M")
        if failures:
            return LIMIT_RETRY_WAIT, f"waiting after {failures} failed charge(s); next try {next_at} UTC"
        per_hour = prefs["per_hour"]
        return LIMIT_PER_HOUR, (f"at most {per_hour} charge(s) per hour of {_usd(prefs['amount_micro'])} "
                                f"(up to {_usd(per_hour * prefs['amount_micro'])} per hour); "
                                f"next {next_at} UTC")
    if billing.autotopup_cap_reached(prefs, month_spend_micro):
        return LIMIT_MONTHLY_CAP, (f"monthly cap: {_usd(month_spend_micro)} of "
                                   f"{_usd(prefs['monthly_cap_micro'])} auto top-up used this month")
    return LIMIT_REFILL_DUE, "a refill is due; no limit holds it"


async def _bounded(db) -> None:
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))


async def find_blocked(*, now: datetime | None = None, min_refusals: int = 20,
                       max_teams: int = 5_000) -> list[Blocked]:
    """Every team with auto top-up configured, below its threshold and refused for balance at least
    `min_refusals` times in the last hour, worst first."""
    now = now or billing._now()
    s = get_settings()
    hour, two_hours = now - timedelta(hours=1), now - timedelta(hours=2)
    threshold = case((Org.autotopup_threshold_micro > 0, Org.autotopup_threshold_micro),
                     else_=billing.usd_to_micro(s.autotopup_default_threshold_usd))
    bought = exists().where(LedgerEntry.org_id == Org.id, LedgerEntry.kind == _TOPUP)
    found: list[Blocked] = []
    async with read_session_maker() as db:
        await _bounded(db)
        orgs = (await db.execute(
            select(Org).where(
                or_(Org.autotopup_consented_at.is_not(None), Org.stripe_default_pm.is_not(None)),
                Org.balance_micro < threshold, bought,
            ).order_by(Org.id).limit(max_teams)
        )).scalars().all()
        await db.commit()
        if len(orgs) >= max_teams:
            log.warning("autotopup watch: candidate list cut at %d teams; later teams were not read", max_teams)
        for org in orgs:
            await _bounded(db)
            last_hour, hour_before = (await db.execute(
                select(func.count().filter(CallRecord.created_at >= hour),
                       func.count().filter(CallRecord.created_at < hour))
                .where(CallRecord.org_id == org.id, CallRecord.created_at >= two_hours,
                       CallRecord.status_code == 402, CallRecord.refused_by == "balance")
            )).one()
            if int(last_hour or 0) < min_refusals:
                await db.commit()
                continue
            spent, settled = (await db.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_micro), 0), func.count())
                .where(LedgerEntry.org_id == org.id, LedgerEntry.kind == _SETTLE,
                       LedgerEntry.created_at >= hour)
            )).one()
            month = await billing.monthly_autotopup_spend(db, org.id)
            limit, detail = blocking_limit(org, now=now, month_spend_micro=month)
            found.append(Blocked(
                org_id=org.id, name=org.name or org.slug or "", owner_email=await billing.owner_email(db, org.id),
                limit=limit, detail=detail, refused_last_hour=int(last_hour), refused_hour_before=int(hour_before or 0),
                spent_last_hour_micro=-int(spent or 0), settled_last_hour=int(settled or 0),
                balance_micro=int(org.balance_micro or 0), min_refusals=min_refusals))
            await db.commit()
    return sorted(found, key=lambda b: -b.refused_last_hour)


def to_json(blocked: list[Blocked]) -> list[dict]:
    return [{**asdict(b), "new": b.new, "turned_away_micro": b.turned_away_micro()} for b in blocked]


def alert(blocked: list[Blocked]) -> str:
    """The operator message: one line per team, the limit first, so whoever reads it can write to
    the owner about that setting."""
    lines = ["Paying teams refused for balance by their own auto top-up settings (last hour, UTC):"]
    for b in blocked:
        since = "new this hour" if b.new else f"{b.refused_hour_before:,} the hour before"
        lines.append(
            f"- {b.name} (team {b.org_id}, owner {b.owner_email or 'unknown'}): {b.limit}: {b.detail}. "
            f"{b.refused_last_hour:,} refusals ({since}); spent {_usd(b.spent_last_hour_micro)} in "
            f"{b.settled_last_hour:,} calls; up to {_usd(b.turned_away_micro())} refused "
            f"(refusals include retries); balance {_usd(b.balance_micro)}")
    lines.append("Reach out to the owner about the named setting; treg changes none of them.")
    return "\n".join(lines)
