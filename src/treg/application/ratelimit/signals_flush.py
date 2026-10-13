"""Writing surge signals: the background half of `signals`, on the dedicated `ratelimit` pool.

Once per `surge_flush_s` it takes the closed window, looks up each org's `created_at` and paid
total, and inserts one `SurgeSignal` row per subject. Rows are per process; a reader sums them by
(window_start, scope, ip, org_id). `distinct_*` are per-process counts and do not sum exactly.
Rows older than `surge_retention_days` are deleted, at most hourly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func
from sqlmodel import select

from ...config import get_settings
from ...infra.db import ratelimit_session_maker
from ...infra.money_timing import PROCESS_INSTANCE
from ...models import CreditBlock, Org, SurgeSignal
from . import signals
from .signals import _now

log = logging.getLogger("treg.ratelimit")
_last_prune: datetime | None = None


async def _org_facts(db, org_ids: set[int]) -> dict[int, tuple]:
    if not org_ids:
        return {}
    orgs = (await db.execute(select(Org.id, Org.slug, Org.created_at).where(Org.id.in_(org_ids)))).all()
    paid = dict((await db.execute(
        select(CreditBlock.org_id, func.coalesce(func.sum(CreditBlock.amount_micro), 0))
        .where(CreditBlock.org_id.in_(org_ids), CreditBlock.kind == "purchased",
               CreditBlock.stripe_payment_intent.is_not(None))
        .group_by(CreditBlock.org_id))).all())
    return {oid: (slug, created, int(paid.get(oid, 0))) for oid, slug, created in orgs}


async def flush() -> int:
    """Write the closed window. Returns the rows written; a failed write is logged and dropped,
    because signals are evidence for a human, never an input to a refusal."""
    window = signals.swap()
    if not window.rows:
        return 0
    try:
        async with ratelimit_session_maker() as db:
            facts = await _org_facts(db, {k[2] for k in window.rows if k[2] is not None})
            for (scope, ip, org_id), a in window.rows.items():
                slug, created, paid = facts.get(org_id, (None, None, None)) if org_id else (None, None, None)
                db.add(SurgeSignal(
                    window_start=window.opened, process_instance=PROCESS_INSTANCE, scope=scope,
                    ip=ip, org_id=org_id, org_slug=slug, org_created_at=created, net_paid_micro=paid,
                    requests=a.requests, call_requests=a.call_requests,
                    unknown_key_requests=a.unknown_key_requests, status_401=a.status_401,
                    status_403=a.status_403, status_429=a.status_429, status_5xx=a.status_5xx,
                    blocklist_matches=a.blocklist_matches, distinct_orgs=len(a.orgs),
                    distinct_keys=len(a.keys), max_in_flight=a.max_in_flight,
                ))
            await db.commit()
    except Exception:  # noqa: BLE001 - a lost minute of evidence must not stop the loop
        log.exception("surge signal flush failed; %d rows dropped", len(window.rows))
        return 0
    if window.dropped:
        log.warning("surge signal window dropped %d subjects past surge_max_subjects", window.dropped)
    await prune()
    return len(window.rows)


async def prune() -> bool:
    """At most hourly, in its own transaction after the insert has committed. The attempt time is
    recorded BEFORE the DELETE, so a prune that times out is retried next hour, not every minute,
    and can never take an insert batch with it."""
    global _last_prune
    now = _now()
    if _last_prune is not None and now - _last_prune < timedelta(hours=1):
        return False
    _last_prune = now
    cutoff = now - timedelta(days=get_settings().surge_retention_days)
    try:
        async with ratelimit_session_maker() as db:
            await db.execute(delete(SurgeSignal).where(SurgeSignal.window_start < cutoff))
            await db.commit()
    except Exception:  # noqa: BLE001 - pruning is housekeeping; the next attempt is in an hour
        log.exception("surge signal prune failed")
        return False
    return True



def reset_for_tests() -> None:
    global _last_prune
    _last_prune = None
