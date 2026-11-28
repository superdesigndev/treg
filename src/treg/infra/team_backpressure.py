"""The teams whose queued charges the allowance fold is behind on, and their in-flight places.

The fold publishes, per share of the teams it works on, the list of teams to throttle under one
self-expiring key (`publish`); request processes read every share's key in one round trip, at
most once per `money_backpressure_refresh_s` per process (`throttled`). A listed team's metered
call takes one of `money_backpressure_max_in_flight` places shared by every process (`admit`), on
the provider concurrency lease. Optional work throughout: a store that is unset, slow, down or
wrong reads as no throttled team and no place to take, so every call is allowed.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Iterable

from ..config import get_settings
from . import kv, provider_concurrency

log = logging.getLogger("treg.team_backpressure")
_KEY = "treg:team-backpressure:v1"
_SLOTS = "treg:team-backpressure-slots:v1"
# Below the store's own limit for optional work, so the call knows the store did not answer.
_READ_WAIT_S = 0.15
_MAX_TEAMS = 1000
_LOG_EVERY_S = 5.0


class Throttled(Exception):
    """The team is listed and every one of its places is taken."""

    def __init__(self, *, max_in_flight: int, retry_after_s: int) -> None:
        self.max_in_flight = max_in_flight
        self.retry_after_s = retry_after_s
        super().__init__("team_backpressure")


def enabled(settings=None) -> bool:
    settings = settings or get_settings()
    return kv.configured() and (settings.money_backpressure_oldest_s is not None
                                or settings.money_backpressure_lease_held_skips is not None)


def share_key(shard: tuple[int, int] | None) -> str:
    """One key per share of the fold (`allowance_sweep.pass_key`'s shares); `all` for a pass over
    every team."""
    if shard is None:
        return f"{_KEY}:all"
    index, count = shard
    return f"{_KEY}:{index}/{count}"


def _share_keys(count: int) -> list[str]:
    return [share_key((index, count)) for index in range(count)] + [share_key(None)]


async def publish(shard: tuple[int, int] | None, org_ids: Iterable[int], ttl_s: int) -> bool:
    """Replace one share's list. An empty list deletes the key, so a team lifts at the next read
    rather than at the key's expiry. False when the store did not take it."""
    ids = sorted({int(org_id) for org_id in org_ids})[:_MAX_TEAMS]
    key = share_key(shard)
    try:
        if not ids:
            await kv.store().clear_flag(key)
            return True
        return await kv.store().set_text(key, ",".join(map(str, ids)), ttl_s)
    except Exception:  # noqa: BLE001 - an unpublished list expires on its own
        return False


def _parse(text: str | None) -> set[int]:
    if not text:
        return set()
    out = set()
    for part in text.split(",")[:_MAX_TEAMS]:
        if part.isdigit():
            out.add(int(part))
    return out


class _Reader:
    def __init__(self) -> None:
        self.teams: frozenset[int] = frozenset()
        self.next_refresh = 0.0
        self.lock = asyncio.Lock()

    async def throttled(self, settings) -> frozenset[int]:
        if time.monotonic() < self.next_refresh:
            return self.teams
        async with self.lock:
            if time.monotonic() < self.next_refresh:
                return self.teams
            try:
                async with asyncio.timeout(_READ_WAIT_S):
                    texts = await kv.store().get_texts(
                        _share_keys(settings.money_allowance_fold_shards))
                teams: set[int] = set()
                for text in texts:
                    teams |= _parse(text)
                self.teams = frozenset(teams)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - an unread list throttles nobody
                self.teams = frozenset()
            self.next_refresh = time.monotonic() + settings.money_backpressure_refresh_s
            return self.teams


_reader = _Reader()
_next_log = 0.0
_suppressed = 0


def reset() -> None:
    global _reader
    _reader = _Reader()


async def throttled(org_id: int) -> bool:
    settings = get_settings()
    if not enabled(settings):
        return False
    return org_id in await _reader.throttled(settings)


def _rejected(org_id: int, limit: int) -> None:
    global _next_log, _suppressed
    now = time.monotonic()
    if now < _next_log:
        _suppressed += 1
        return
    try:
        log.warning("team_backpressure rejected org_id=%s max_in_flight=%s status=429 "
                    "suppressed_since_last_log=%s", org_id, limit, _suppressed)
    except Exception:  # noqa: BLE001 - logging never replaces the refusal
        pass
    _suppressed = 0
    _next_log = now + _LOG_EVERY_S


async def admit(org_id: int) -> provider_concurrency.Lease | None:
    """A place for one metered call of a throttled team, released with `Lease.release()`; None
    for a team that is not throttled, or when the store does not answer. Raises `Throttled` when
    every place of the team is taken."""
    if not await throttled(org_id):
        return None
    settings = get_settings()
    limit = settings.money_backpressure_max_in_flight
    lease = await provider_concurrency.try_acquire_slot(f"{_SLOTS}:{org_id}", limit)
    if lease == provider_concurrency.BUSY:
        _rejected(org_id, limit)
        base = settings.money_admission_retry_after_s
        raise Throttled(max_in_flight=limit, retry_after_s=random.randint(base, 2 * base))
    return lease
