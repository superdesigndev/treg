"""Capacity marks written FROM the call path — the one sanctioned dataplane write of this domain.

A confirmed balance/quota signature on treg's own key (plan §4.1) marks the provider exhausted in
ratestore (`capacity:state:<provider>`, the same key the sweep publishes) so the NEXT call is refused
before a hold exists, without waiting for a sweep tick. Ratestore is the shared `Ephemeral` table:
this is a DB write, listed in `tests/test_call_architecture.py`'s dataplane allowlist as
`capacity_exhausted_mark`. It runs on its own short session AFTER the settle — never while an
upstream request is in flight — and never raises: a failed mark costs one more relayed 402, not
the call. The tables (`capacitypolicy`, `capacitysnapshot`) are worker-only; nothing here touches them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ... import ratestore
from ...infra.db import session_maker
from ...timeutil import utcnow_naive
from .policy import LatestState
from .sweep import STATE_NS, STATE_TTL_S

DEFAULT_EXHAUSTED_FOR = timedelta(hours=1)
"""A balance signature carries no reset time: hold the mark this long, then let a call probe again
(the sweep, hourly, refreshes it from the account API in between)."""

log = logging.getLogger("treg.capacity")


async def mark_exhausted(provider: str, *, until: datetime | None, note: str = "",
                         now: datetime | None = None) -> LatestState | None:
    now = now or utcnow_naive()
    until = until or (now + DEFAULT_EXHAUSTED_FOR)
    state = LatestState(provider, 0.0, "", now, "exact", exhausted_until=until, health="exhausted",
                        note=(note or "signature on the call path")[:200])
    try:
        async with session_maker() as db:
            prev = await ratestore.kv_get(db, STATE_NS, provider)
            if prev:  # the sweep's rate limit must survive a mark
                state.rate_limit = prev.get("rate_limit")
            await ratestore.kv_put(db, STATE_NS, provider, state.to_json(), ttl_s=STATE_TTL_S)
            await db.commit()
    except Exception:  # noqa: BLE001 — a mark is a hint for the next call, never this call's fate
        log.warning("capacity mark for %s not written", provider, exc_info=True)
        return None
    return state
