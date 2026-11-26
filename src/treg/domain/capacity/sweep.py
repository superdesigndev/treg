"""`treg-worker capacity sweep` — one pass over every treg-owned account.

collect (free provider calls, in parallel) → one `CapacitySnapshot` row each (a failure is a row) →
publish the latest state per provider to ratestore (`capacity:state:<provider>`), which the
dataplane's `view` reads on a TTL. Worker profile only: needs the platform keys in the env and makes
outbound calls, so it is never lifespan work of the server (refactor plan §2.2).

Publishes `capacity:state:*` only; the call path's locks (`marks.py`, `capacity:lock:*`) are never
written here. Money is never touched here.
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
_CREDENTIAL_IN_URL = ("api_key=", "apikey=", "token=")


def state_key(provider: str) -> str:
    """The `capacity:state` key for an account in either spelling: the provider's Settings slot
    (`fiber-ai` -> `fiber_ai`, `fiber-ai#b` -> `fiber_ai#b`), the one the sweep publishes under."""
    base, label = collectors.split_account(provider)
    return collectors.account_key(platform_setting_name(base).removeprefix("platform_key_"), label)


def _has_key(provider: str) -> bool:
    if provider.startswith("overflow:"):
        return bool(getattr(get_settings(), f"overflow_key_{provider.split(':', 1)[1]}", ""))
    base = collectors.split_account(provider)[0]
    return bool(collectors.account_credential(provider)(platform_setting_name(base)))


def snapshot_from(provider: str, row: dict, observed_at=None) -> CapacitySnapshot:
    """A collector's dict → the row. Never carries a credential: the note is clipped and checked."""
    note = str(row.get("note") or "")[:300]
    error = ""
    if row.get("value") is None and not row.get("informational"):
        if row.get("no_api"):
            error = "no_balance_api"
        elif row.get("no_key"):
            error = "no_key"
        else:
            error = note or "collector returned no value"
            note = ""
    if any(word in note.lower() for word in _FORBIDDEN_IN_NOTE):
        note = "(note withheld: looked like a credential)"
    lowered = error.lower()
    if (any(mark in lowered for mark in _CREDENTIAL_IN_URL)
            or any(value in error for value in collectors.credential_values(provider))):
        error = "collector failed (detail withheld: looked like a credential)"
    value = row.get("value")
    return CapacitySnapshot(
        provider=provider, observed_at=observed_at or utcnow_naive(),
        remaining=float(value) if isinstance(value, (int, float)) else None,
        unit=str(row.get("unit") or ""), source="api", confidence=("informational" if row.get("informational") else "exact" if error == "" else "stale"),
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
    providers = sorted(p for p in policies
                       if not only or p in only or collectors.split_account(p)[0] in only)
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
        await ratestore.kv_put(db, STATE_NS, state_key(provider), state.to_json(), ttl_s=STATE_TTL_S)
    await db.commit()
    for p in unknown:
        log.warning("capacity policy for %s imported as unknown — classify it", p)
    return SweepResult(providers, unknown, snaps, states)
