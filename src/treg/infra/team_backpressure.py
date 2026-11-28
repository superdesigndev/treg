"""The teams whose money work treg is behind on, and their in-flight places.

The fold publishes, per share of the teams it works on, the list of teams to throttle under one
self-expiring key (`publish`). A team whose own calls keep timing out in the money admission
queue is marked under a key of its own by the process that saw the timeouts
(`note_admission_timeout`). Request processes read every share's key, and the marks of the teams
they served since the last read, in one round trip, at most once per
`money_backpressure_refresh_s` per process (`throttled`). A throttled team's metered
call takes one of `money_backpressure_max_in_flight` places shared by every process (`admit`), on
the provider concurrency lease. Optional work throughout: a store that is unset, slow, down or
wrong reads as no throttled team and no place to take, so every call is allowed.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from collections.abc import Iterable

from ..config import get_settings
from . import kv, provider_concurrency

log = logging.getLogger("treg.team_backpressure")
_KEY = "treg:team-backpressure:v1"
_SLOTS = "treg:team-backpressure-slots:v1"
_TIMEOUTS = "treg:team-backpressure-admission-timeouts:v1"
_MARK = "treg:team-backpressure-admission:v1"
# Below the store's own limit for optional work, so the call knows the store did not answer.
_READ_WAIT_S = 0.15
_MAX_TEAMS = 1000
# Teams kept waiting for a look-up at most, so a burst of new teams cannot grow the map unbounded.
_ASKED_KEPT = 4 * _MAX_TEAMS
# Timeout counts being written at once at most; past it a count is dropped, never queued.
_PENDING_MAX = 256
_pending: set[asyncio.Task] = set()
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
                                or settings.money_backpressure_lease_held_skips is not None
                                or settings.money_backpressure_admission_timeouts is not None)


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


def mark_key(org_id: int) -> str:
    return f"{_MARK}:{org_id}"


def note_admission_timeout(org_id: int) -> None:
    """Count one of the team's reserves the money admission queue turned away; at
    `money_backpressure_admission_timeouts` inside `money_backpressure_admission_window_s`, mark
    the team for `money_backpressure_mark_ttl_s`, renewed by every timeout past the count.

    The count is written in the background, at most `_PENDING_MAX` at once: the refusal it
    follows is returned, and a refill's money released, without waiting on the store, and a
    count that would exceed the bound is dropped rather than queued. Never raises."""
    settings = get_settings()
    count = settings.money_backpressure_admission_timeouts
    if count is None or not kv.configured() or len(_pending) >= _PENDING_MAX:
        return
    try:
        task = asyncio.get_running_loop().create_task(_count_timeout(org_id, count, settings))
    except RuntimeError:  # no running loop: nothing to count on
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _count_timeout(org_id: int, count: int, settings) -> None:
    """A store that cannot count may mark the team, as `take` spends a budget it cannot check."""
    try:
        async with asyncio.timeout(_READ_WAIT_S):
            store = kv.store()
            if await store.take(f"{_TIMEOUTS}:{org_id}", count - 1,
                                settings.money_backpressure_admission_window_s):
                return
            await store.set_text(mark_key(org_id), "1", settings.money_backpressure_mark_ttl_s)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - an unmarked team is only not throttled
        pass


async def drain() -> None:
    """Wait for the counts in flight (tests, shutdown)."""
    while running := [task for task in _pending if not task.done()]:
        await asyncio.gather(*running, return_exceptions=True)


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
        # The teams a fold list names: they keep `money_backpressure_max_in_flight` even when also
        # marked by the admission trigger.
        self.listed: frozenset[int] = frozenset()
        self.next_refresh = 0.0
        self.lock = asyncio.Lock()
        # The teams whose mark the last read found: every read looks them up again, whether or
        # not they asked since, until their mark is gone.
        self.marked: frozenset[int] = frozenset()
        # Teams whose calls asked since they were last looked up, oldest first: a read that
        # cannot take them all keeps the rest at the front for the next one.
        self.asked: dict[int, None] = {}

    def _batch(self, org_id: int) -> list[int]:
        """The marks one read looks up: the asking team, then every team marked at the last
        read, then the teams that asked, oldest first, up to `_MAX_TEAMS`."""
        batch, seen = [org_id], {org_id}
        for team in itertools.chain(sorted(self.marked), self.asked):
            if len(batch) >= _MAX_TEAMS:
                break
            if team not in seen:
                batch.append(team)
                seen.add(team)
        self.asked = {team: None for team in self.asked if team not in seen}
        return batch

    async def throttled(self, settings, org_id: int) -> frozenset[int]:
        watching = settings.money_backpressure_admission_timeouts is not None
        if watching and len(self.asked) < _ASKED_KEPT:
            self.asked.setdefault(org_id)
        if time.monotonic() < self.next_refresh:
            return self.teams
        async with self.lock:
            if time.monotonic() < self.next_refresh:
                return self.teams
            batch = self._batch(org_id) if watching else []
            try:
                share_keys = _share_keys(settings.money_allowance_fold_shards)
                async with asyncio.timeout(_READ_WAIT_S):
                    texts = await kv.store().get_texts(
                        share_keys + [mark_key(team) for team in batch])
                teams: set[int] = set()
                for text in texts[:len(share_keys)]:
                    teams |= _parse(text)
                self.listed = frozenset(teams)
                self.marked = frozenset(
                    team for team, text in zip(batch, texts[len(share_keys):]) if text)
                self.teams = frozenset(teams | self.marked)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - an unread list throttles nobody
                self.teams = self.listed = frozenset()
            self.next_refresh = time.monotonic() + settings.money_backpressure_refresh_s
            return self.teams


_reader = _Reader()
_next_log = 0.0
_suppressed = 0


def reset() -> None:
    global _reader
    _reader = _Reader()
    _pending.clear()


async def throttled(org_id: int) -> bool:
    settings = get_settings()
    if not enabled(settings):
        return False
    return org_id in await _reader.throttled(settings, org_id)


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
    if (org_id not in _reader.listed
            and settings.money_backpressure_admission_max_in_flight is not None):
        limit = settings.money_backpressure_admission_max_in_flight
    lease = await provider_concurrency.try_acquire_slot(f"{_SLOTS}:{org_id}", limit)
    if lease == provider_concurrency.BUSY:
        _rejected(org_id, limit)
        base = settings.money_admission_retry_after_s
        raise Throttled(max_in_flight=limit, retry_after_s=random.randint(base, 2 * base))
    return lease
