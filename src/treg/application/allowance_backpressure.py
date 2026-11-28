"""Which teams the allowance fold is behind on, decided from what each fold pass already read.

A pass lists every team of its share with charges queued and when each team's oldest was written
(`allowance.queued_teams`), and counts the teams it could not take because another process held
their lease. From those alone, with no read of its own, the pass names the teams to throttle and
publishes them for its share (`infra.team_backpressure`):

- a team whose oldest queued charge has waited `money_backpressure_oldest_s` or more;
- a starved team (oldest at least `starved_after_s`, the fold's own threshold) that the fold found
  `lease_held` `money_backpressure_lease_held_skips` times in a row, with no fold of it between.

The pass read its share's whole queue, so its list replaces the share's: a team drops off as soon
as a pass no longer names it (an empty list deletes the key), and a list expires
`money_backpressure_mark_ttl_s` after the last pass that published it, so a fold that stops
throttles nobody.
"""
from __future__ import annotations

import time
from collections.abc import Iterable

from ..config import get_settings
from ..infra import team_backpressure

_lease_held: dict[int, int] = {}
# Teams this pass emptied the queue of: their wait, read before the fold, is no backlog now.
_drained: set[int] = set()
_LEASE_HELD_KEPT = 10_000
# What each share last published, and when: an unchanged list is published again only once a
# third of its lifetime has passed.
_published: dict[str, tuple[frozenset[int], float]] = {}


def note_lease_held(org_id: int) -> None:
    if get_settings().money_backpressure_lease_held_skips is None:
        return
    if org_id not in _lease_held and len(_lease_held) >= _LEASE_HELD_KEPT:
        _lease_held.clear()
    _lease_held[org_id] = _lease_held.get(org_id, 0) + 1


def note_folded(org_id: int) -> None:
    _lease_held.pop(org_id, None)


def begin_pass() -> None:
    _drained.clear()


def note_drained(org_id: int) -> None:
    _drained.add(org_id)


def _in_share(org_id: int, shard) -> bool:
    """`allowance.queued_teams`' rule: a share holds the teams whose number leaves its index."""
    return shard is None or shard[1] <= 1 or org_id % shard[1] == shard[0]


def choose(waits: Iterable[tuple[int, int]], *, starved_after_s: int, shard=None,
           complete: bool = False) -> frozenset[int]:
    """The teams to throttle among `waits`, (team, seconds its oldest queued charge waited), as
    one pass over `shard` read them. Only a `complete` read (every page of the share) shows that
    a team of the share it does not list has nothing queued, and only then is its count of
    `lease_held` skips dropped; a team of another share keeps its count."""
    settings = get_settings()
    oldest_s = settings.money_backpressure_oldest_s
    skips = settings.money_backpressure_lease_held_skips
    chosen, queued = set(), set()
    for org_id, waited_s in waits:
        if org_id in _drained:
            continue
        queued.add(org_id)
        if oldest_s is not None and waited_s >= oldest_s:
            chosen.add(org_id)
        elif (skips is not None and waited_s >= starved_after_s
              and _lease_held.get(org_id, 0) >= skips):
            chosen.add(org_id)
    if complete:
        for org_id in [team for team in _lease_held
                       if team not in queued and _in_share(team, shard)]:
            del _lease_held[org_id]  # nothing queued any more: the next backlog counts afresh
    return frozenset(chosen)


async def publish(shard, waits: Iterable[tuple[int, int]], *, starved_after_s: int,
                  complete: bool = False) -> None:
    """Publish one share's list after a pass that read its whole queue. Never raises."""
    try:
        settings = get_settings()
        if not team_backpressure.enabled(settings):
            return
        teams = choose(waits, starved_after_s=starved_after_s, shard=shard, complete=complete)
        key = team_backpressure.share_key(shard)
        last = _published.get(key)
        now = time.monotonic()
        ttl_s = settings.money_backpressure_mark_ttl_s
        if last is not None and last[0] == teams and now - last[1] < ttl_s / 3:
            return
        if await team_backpressure.publish(shard, teams, ttl_s):
            _published[key] = (teams, now)
    except Exception:  # noqa: BLE001 - throttling is optional; the fold goes on
        pass


def reset() -> None:
    _lease_held.clear()
    _drained.clear()
    _published.clear()
