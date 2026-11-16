"""DB-backed ephemeral state: OTP codes + auth rate-limit windows (backlog #3).

These used to be module-level dicts in `api.py` — correct only on ONE process. On a restart the
counters reset (an attacker just waits for a redeploy), and on two+ instances a per-IP/per-email cap
becomes N× weaker and an OTP written on one worker is invisible to another. Moving them to a single
`Ephemeral` table (keyed by namespace + key, opaque JSON value, `expires_at`) fixes both: the state
persists and is shared.

Two shapes live here:
  * **key/value with TTL** (`kv_put`/`kv_get`/`kv_pop`/`kv_take`) — the OTP code + its attempt counter.
  * **sliding-window rate limit** (`rate_check`) — the OTP-start + sandbox throttles; each key's row
    holds a list of recent hit timestamps (wall-clock epoch seconds, so they're comparable across
    processes — the old code used `time.monotonic()`, which is per-process and can't be shared).

Growth is bounded by `expires_at` + `sweep` (delete expired rows), so an unauthenticated caller can't
make the table grow without limit. Writes are per-request and low-volume (auth endpoints only).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from . import client_ip
from .models import Ephemeral


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def sweep(db: AsyncSession, ns: str | None = None) -> None:
    """Delete expired rows (optionally within one namespace) — the bound on table growth. Call it
    before adding to an unauthenticated-reachable namespace, mirroring the old `_rate_sweep`."""
    stmt = delete(Ephemeral).where(Ephemeral.expires_at < _utcnow_naive())
    if ns is not None:
        stmt = stmt.where(Ephemeral.ns == ns)
    await db.execute(stmt)


async def kv_get(db: AsyncSession, ns: str, k: str) -> dict | None:
    """The value for (ns, k), or None if missing or expired (an expired row is removed in passing)."""
    row = await db.get(Ephemeral, (ns, k))
    if row is None:
        return None
    if row.expires_at < _utcnow_naive():
        await db.delete(row)
        return None
    return row.v


async def kv_put(db: AsyncSession, ns: str, k: str, v: dict, ttl_s: float | None) -> None:
    """Upsert (ns, k) → `v`. `ttl_s` sets a fresh expiry; pass ttl_s=None to keep the existing expiry
    (used when only the payload changes, e.g. decrementing the OTP attempt counter without extending
    the code's lifetime)."""
    row = await db.get(Ephemeral, (ns, k))
    if row is None:
        # An INSERT that another process may be racing: two processes striking the same provider
        # write the same `capacity:lock` key within the same millisecond, and the loser of a plain
        # INSERT dies on `ephemeral_pkey`. ON CONFLICT makes the last writer win, which is what a
        # key/value put means.
        exp = _utcnow_naive() + timedelta(seconds=ttl_s or 0)
        await db.execute(_upsert(db.get_bind().dialect.name, ns=ns, k=k, v=v, expires_at=exp))
        return
    row.v = v  # reassign (not in-place) so SQLAlchemy marks the JSON column dirty
    if ttl_s is not None:
        row.expires_at = _utcnow_naive() + timedelta(seconds=ttl_s)
    db.add(row)


def _upsert(dialect: str, **values):
    """`INSERT ... ON CONFLICT (ns, k) DO UPDATE` for the dialect at hand - both backends spell it
    the same way, but SQLAlchemy exposes it per dialect."""
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    stmt = insert(Ephemeral).values(**values)
    return stmt.on_conflict_do_update(
        index_elements=[Ephemeral.ns, Ephemeral.k],
        set_={"v": stmt.excluded.v, "expires_at": stmt.excluded.expires_at})


async def kv_pop(db: AsyncSession, ns: str, k: str) -> dict | None:
    """Read-and-delete (ns, k) atomically-ish within this session. Returns the value if it was still
    live (not expired), else None. The row is always removed."""
    row = await db.get(Ephemeral, (ns, k))
    if row is None:
        return None
    v, exp = row.v, row.expires_at
    await db.delete(row)
    return v if exp >= _utcnow_naive() else None


async def kv_take(db: AsyncSession, ns: str, k: str) -> dict | None:
    """Delete (ns, k) in one statement and return its value if it was still live. Of concurrent
    callers exactly one gets the value, so it suits a single-use code."""
    row = (await db.execute(
        delete(Ephemeral).where(Ephemeral.ns == ns, Ephemeral.k == k)
        .returning(Ephemeral.v, Ephemeral.expires_at)
    )).first()
    if row is None or row.expires_at < _utcnow_naive():
        return None
    return row.v


async def kv_claim(db: AsyncSession, ns: str, k: str, ttl_s: float) -> bool:
    """Write (ns, k) only if no live row holds it: True for the one caller that claimed it. A
    single-use token's claim; an expired row is swept first so its key can be claimed again."""
    now = _utcnow_naive()
    await db.execute(delete(Ephemeral).where(
        Ephemeral.ns == ns, Ephemeral.k == k, Ephemeral.expires_at < now))
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    stmt = insert(Ephemeral).values(
        ns=ns, k=k, v={}, expires_at=now + timedelta(seconds=ttl_s)).on_conflict_do_nothing(
        index_elements=[Ephemeral.ns, Ephemeral.k])
    return (await db.execute(stmt)).rowcount == 1


def ip_limit(ip: str, max_hits: int, key: str | None = None) -> list[tuple[str, int]]:
    """The per-address entry of a `rate_check` limits list, keyed by `key` (default: the address
    itself). Empty for an address the deployment exempts, so the other entries still decide."""
    if client_ip.exempt(ip):
        return []
    return [(key or ip, max_hits)]


async def _locked_hits(db: AsyncSession, ns: str, k: str, ttl_s: float) -> list[float]:
    """Create (ns, k) if absent, or lock it, in one statement, and return its hits. The row stays
    locked until the transaction ends (PostgreSQL holds the conflicting row's lock on DO UPDATE)."""
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    stmt = insert(Ephemeral).values(
        ns=ns, k=k, v={}, expires_at=_utcnow_naive() + timedelta(seconds=ttl_s))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Ephemeral.ns, Ephemeral.k],
        set_={"expires_at": stmt.excluded.expires_at}).returning(Ephemeral.v)
    v = (await db.execute(stmt)).scalar_one()
    return list((v or {}).get("hits", []))


async def rate_check(
    db: AsyncSession, ns: str, limits: list[tuple[str, int]], window_s: float,
    *, atomic: bool = False,
) -> bool:
    """All-or-nothing sliding-window limiter over the (ns, key) rows. `limits` is a list of
    (key, max_hits): the request is allowed only if EVERY key has fewer than its cap of hits within
    the last `window_s`. On allow, one hit (now) is appended under every key and the rows are
    persisted with a fresh expiry; on reject, nothing is written. Each key's expired timestamps are
    pruned before the check. Wall-clock timestamps so the window is comparable across processes.

    `atomic` locks every key's row (in key order, so concurrent checks cannot deadlock) until the
    caller commits, so concurrent checks of one key count every hit; without it two concurrent
    checks may both read the same list and one hit is lost."""
    pruned: dict[str, list[float]] = {}
    if atomic:
        locked = {key: await _locked_hits(db, ns, key, window_s) for key, _ in sorted(limits)}
        now = time.time()
        for key, hits in locked.items():
            pruned[key] = [t for t in hits if now - t < window_s]
        ok = all(len(pruned[key]) < mx for key, mx in limits)
        if ok:
            for key, _ in limits:
                await db.execute(update(Ephemeral).where(Ephemeral.ns == ns, Ephemeral.k == key)
                                 .values(v={"hits": [*pruned[key], now]}, expires_at=_utcnow_naive()
                                         + timedelta(seconds=window_s)))
        return ok
    now = time.time()
    for key, _ in limits:
        row = await db.get(Ephemeral, (ns, key))
        hits = (row.v or {}).get("hits", []) if row is not None else []
        pruned[key] = [t for t in hits if now - t < window_s]
    ok = all(len(pruned[key]) < mx for key, mx in limits)
    if ok:
        for key, _ in limits:
            pruned[key].append(now)
            await kv_put(db, ns, key, {"hits": pruned[key]}, ttl_s=window_s)
    return ok
