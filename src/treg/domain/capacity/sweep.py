"""`treg-worker capacity sweep` — one pass over every treg-owned account.

collect (free provider calls, in parallel) → one `CapacitySnapshot` row each (a failure is a row) →
publish the latest state per provider to ratestore (`capacity:state:<provider>`), which the
dataplane's `view` reads on a TTL. Worker profile only: needs the platform keys in the env and makes
outbound calls, so it is never lifespan work of the server (refactor plan §2.2).

Observe-only in step B: no alerts, no marks the call path acts on. Money is never touched here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ... import ratestore
from ...config import get_settings, platform_setting_name
from ...models import CapacityPolicy, CapacitySnapshot
from ...timeutil import utcnow_naive
from . import collectors
from .policy import LatestState, ensure_policies, latest_state

STATE_NS = "capacity:state"
STATE_TTL_S = 24 * 3600  # a published state outlives many sweeps; `latest_state` marks it stale itself

log = logging.getLogger("treg.capacity")

_FORBIDDEN_IN_NOTE = ("key", "token", "secret", "bearer", "sk_", "password")


def _has_key(provider: str) -> bool:
    if provider.startswith("overflow:"):
        return bool(getattr(get_settings(), f"overflow_key_{provider.split(':', 1)[1]}", ""))
    return bool(getattr(get_settings(), platform_setting_name(provider), "") or "")


def snapshot_from(provider: str, row: dict, observed_at=None) -> CapacitySnapshot:
    """A collector's dict → the row. Never carries a credential: the note is clipped and checked."""
    note = str(row.get("note") or "")[:300]
    error = ""
    if row.get("value") is None:
        if row.get("no_api"):
            error = "no_balance_api"
        elif row.get("no_key"):
            error = "no_key"
        else:
            error = note or "collector returned no value"
            note = ""
    if any(word in note.lower() for word in _FORBIDDEN_IN_NOTE):
        note = "(note withheld: looked like a credential)"
    value = row.get("value")
    return CapacitySnapshot(
        provider=provider, observed_at=observed_at or utcnow_naive(),
        remaining=float(value) if isinstance(value, (int, float)) else None,
        unit=str(row.get("unit") or ""), source="api", confidence="exact" if error == "" else "stale",
        note=note, error=error,
    )


@dataclass
class SweepResult:
    providers: list[str]
    unknown_policies: list[str]
    snapshots: list[CapacitySnapshot]
    states: dict[str, LatestState]


async def _collect_all(providers: list[str], client: httpx.AsyncClient | None) -> dict[str, dict]:
    async def one(p: str) -> tuple[str, dict]:
        if p.startswith("overflow:"):
            return p, {"provider": p, "value": None, "unit": "", "no_api": True,
                       "note": "aggregator publishes no balance endpoint; source=manual"}
        return p, await collectors.provider_balance(p, client)
    rows = await asyncio.gather(*(one(p) for p in providers))
    return dict(rows)


async def run_sweep(db: AsyncSession, *, client: httpx.AsyncClient | None = None,
                    only: set[str] | None = None) -> SweepResult:
    """One sweep on an application-owned session. Commits once at the end (a partial sweep
    is not worth persisting: the published state must match the rows)."""
    unknown = await ensure_policies(db, has_key=_has_key)
    await db.flush()
    policies = {p.provider: p for p in (await db.execute(select(CapacityPolicy))).scalars()}
    providers = [p for p in sorted(policies) if p in only] if only else sorted(policies)
    # DB idle during the outbound calls: nothing above is left un-flushed, nothing below reads
    # while the network is in flight (the same discipline as the call path, on a smaller stage).
    rows = await _collect_all(providers, client)
    now = utcnow_naive()
    snaps: list[CapacitySnapshot] = []
    states: dict[str, LatestState] = {}
    for provider in providers:
        snap = snapshot_from(provider, rows[provider], now)
        db.add(snap)
        snaps.append(snap)
        state = latest_state(policies[provider], snap, now)
        states[provider] = state
        await ratestore.kv_put(db, STATE_NS, provider, state.to_json(), ttl_s=STATE_TTL_S)
    await db.commit()
    for p in unknown:
        log.warning("capacity policy for %s imported as unknown — classify it", p)
    return SweepResult(providers, unknown, snaps, states)
