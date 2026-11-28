"""The "own key out of credit" mark: a short-lived note in the shared store that a team's own key
answered one catalog endpoint with `own_key_credit_streak` out-of-credit answers in a row, so treg
answers that team's next calls to that endpoint on that key itself, without sending them, for
`own_key_credit_hold_s` seconds (proxy-model.md "An own key out of credit"). 0 = off.

The streak is counted per process, so a team that is not failing never costs a store round trip.
Only an answer the provider gave counts; a call this mark answers never reaches the provider, so it
cannot renew the mark, and the mark ends at its lifetime.

Optional work throughout: a store that is slow, down or wrong reads as no mark, and a write or a
clear that fails is left to the mark's own lifetime.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import NamedTuple

from ..config import get_settings
from . import kv

Key = tuple[int, str, tuple[int, ...]]

_SUSPECTS_KEPT = 10_000
_READ_WAIT_S = 0.15
_EXCERPT_CHARS = 300
_streaks: dict[Key, tuple[int, float]] = {}
_suspects: dict[Key, float] = {}
# Keys this process found held: the next out-of-credit answer on one is a failed probe and holds
# it again at once, whatever this process's own count.
_armed: dict[Key, float] = {}
_tasks: set[asyncio.Task] = set()


class Held(NamedTuple):
    """The mark: the provider's last answer and when the hold ends (epoch seconds)."""

    streak: int
    status: int
    excerpt: str
    answered_at: float
    until: float

    def retry_after_s(self, now: float | None = None) -> int:
        return max(1, int(self.until - (now if now is not None else time.time()) + 0.999))


def hold_s() -> int:
    return get_settings().own_key_credit_hold_s


def streak_needed() -> int:
    return get_settings().own_key_credit_streak


def key_for(org_id: int | None, endpoint_id: str, secret_ids) -> Key | None:
    """The team, endpoint and key a hold is about, or None when the feature is off or the call
    injects no stored credential of the team's."""
    ids = tuple(sorted(int(i) for i in secret_ids if i is not None))
    if hold_s() <= 0 or not org_id or not endpoint_id or not ids:
        return None
    return int(org_id), endpoint_id, ids


def _store_key(key: Key) -> str:
    org_id, endpoint_id, ids = key
    return f"treg:own-key-credit:v1:{org_id}:{endpoint_id}:{'.'.join(map(str, ids))}"


def _bounded(table: dict, now: float) -> None:
    if len(table) < _SUSPECTS_KEPT:
        return
    for stale in [k for k, v in table.items() if (v[1] if isinstance(v, tuple) else v) <= now]:
        del table[stale]
    if len(table) >= _SUSPECTS_KEPT:
        table.clear()


def _gap_s() -> float:
    return max(60.0, 2.0 * hold_s())


def excerpt(text: str) -> str:
    return " ".join(text.split())[:_EXCERPT_CHARS]


async def read(key: Key | None) -> Held | None:
    """The mark for `key`, when this process saw that key answered out of credit lately."""
    if key is None or _suspects.get(key, 0.0) <= time.monotonic():
        return None
    try:
        async with asyncio.timeout(_READ_WAIT_S):
            text = await kv.store().get_text(_store_key(key))
        if not text:
            return None
        data = json.loads(text)
        held = Held(int(data["streak"]), int(data["status"]), str(data["excerpt"]),
                    float(data["at"]), float(data["until"]))
    except Exception:  # noqa: BLE001 - an unread mark is a missing one
        return None
    if held.until <= time.time():
        return None
    now_m = time.monotonic()
    _bounded(_armed, now_m)
    _armed[key] = now_m + hold_s() + _gap_s()
    return held


async def observe(key: Key | None, *, out_of_credit: bool, status: int, answer_text: str) -> None:
    """After the provider answered a call on `key`: count an out-of-credit answer, and write the
    mark once the streak is long enough; any other answer ends the streak, and a success clears a
    mark this process may have seen. Store writes run in the background, so the answer never
    waits on the store. Never raises."""
    if key is None:
        return
    try:
        now_m = time.monotonic()
        if not out_of_credit:
            _streaks.pop(key, None)
            if status < 400:
                _armed.pop(key, None)
                if _suspects.pop(key, None) is not None:
                    _spawn(_clear(key))
            return
        count, last = _streaks.get(key, (0, 0.0))
        count = count + 1 if now_m - last <= _gap_s() else 1
        _bounded(_streaks, now_m - _gap_s())
        _streaks[key] = (count, now_m)
        _bounded(_suspects, now_m)
        _suspects[key] = now_m + _gap_s()
        if count < streak_needed() and _armed.get(key, 0.0) <= now_m:
            return
        now = time.time()
        value = json.dumps({"streak": count, "status": int(status), "excerpt": excerpt(answer_text),
                            "at": now, "until": now + hold_s()})
        _spawn(_write(key, value, hold_s()))
    except Exception:  # noqa: BLE001 - the next out-of-credit answer writes it again
        pass


async def _write(key: Key, value: str, ttl: int) -> None:
    try:
        await kv.store().set_text(_store_key(key), value, ttl)
    except Exception:  # noqa: BLE001 - the next out-of-credit answer writes it again
        pass


async def _clear(key: Key) -> None:
    try:
        await kv.store().clear_flag(_store_key(key))
    except Exception:  # noqa: BLE001 - the mark expires on its own
        pass


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro, name="treg-own-key-credit")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def refusal(held: Held, *, provider: str, endpoint_id: str) -> dict:
    """treg's own answer for a held call: what the provider said last, and when calls go out again."""
    wait = held.retry_after_s()
    return {
        "error": "own_key_out_of_credit",
        "provider": provider,
        "endpoint_id": endpoint_id,
        "message": (
            f"your own {provider} account is out of credit: its last {held.streak} answers to "
            f"{endpoint_id} on this key said so, so treg did not send this call (not sent, not "
            f"charged). Top up the {provider} account or connect another key; treg sends calls "
            f"on this key again in {wait} s."),
        "provider_last_answer": {
            "status": held.status,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(held.answered_at)),
            "body": held.excerpt,
        },
        "retry_after_s": wait,
    }
