"""The "balance empty" mark: a short-lived per-team note in the shared store that a reserve of a
known amount was just refused because the team's money, wherever it sits, does not reach it.

Optional work throughout: a store that is slow, down or wrong reads as no mark, and a write or a
clear that fails is left to the mark's own lifetime (`money_empty_mark_ttl_s`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import NamedTuple

from sqlalchemy import event

from ..config import get_settings
from . import kv

# How long a process keeps asking the store about a team after it last saw that team refused.
_SUSPECT_S = 60.0
_SUSPECTS_KEPT = 10_000
_SESSION_KEY = "treg_empty_balance_credited"
_suspects: dict[int, float] = {}
_clears: set[asyncio.Task] = set()


class Mark(NamedTuple):
    """A reserve of `required_micro` was refused; the refusal showed `balance_micro`."""

    required_micro: int
    balance_micro: int


def ttl_s() -> int:
    return get_settings().money_empty_mark_ttl_s


def _key(org_id: int) -> str:
    return f"treg:balance-empty:v1:{org_id}"


def suspect(org_id: int) -> None:
    """This process saw the team refused for its balance: its next calls ask the store first."""
    if ttl_s() <= 0:
        return
    now = time.monotonic()
    if org_id not in _suspects and len(_suspects) >= _SUSPECTS_KEPT:
        for stale in [team for team, until in _suspects.items() if until <= now]:
            del _suspects[stale]
        if len(_suspects) >= _SUSPECTS_KEPT:
            _suspects.clear()
    _suspects[org_id] = now + _SUSPECT_S


async def read(org_id: int) -> Mark | None:
    """The team's mark, or None: switched off, a team this process has not seen refused lately
    (the store is not asked), no mark, or a store that cannot answer."""
    if ttl_s() <= 0 or _suspects.get(org_id, 0.0) <= time.monotonic():
        return None
    try:
        text = await kv.store().get_text(_key(org_id))
        if not text:
            return None
        required, balance = (int(part) for part in text.split(":"))
    except Exception:  # noqa: BLE001 - an unread mark is a missing one
        return None
    return Mark(required, balance) if required > 0 else None


async def remember(org_id: int, required_micro: int, balance_micro: int) -> None:
    """Write the team's mark for `money_empty_mark_ttl_s`. Only a refusal the database decided
    writes it, so a mark is never renewed by the refusals it answers."""
    ttl = ttl_s()
    if ttl <= 0 or required_micro <= 0:
        return
    try:
        await kv.store().set_text(_key(org_id), f"{int(required_micro)}:{int(balance_micro)}", ttl)
    except Exception:  # noqa: BLE001 - the call is refused as it was; the next refusal writes it
        pass


async def clear(org_ids: Iterable[int]) -> None:
    for org_id in org_ids:
        try:
            await kv.store().clear_flag(_key(org_id))
        except Exception:  # noqa: BLE001 - the mark expires on its own
            pass


def credited(db, org_id: int) -> None:
    """A balance is going up in this transaction: clear the team's mark once it commits. Never
    raises and does no I/O here; the clear runs in a task of its own after the commit."""
    try:
        if ttl_s() <= 0:
            return
        sync = getattr(db, "sync_session", None)
        if sync is None:
            return
        orgs = sync.info.get(_SESSION_KEY)
        if orgs is None:
            orgs = sync.info[_SESSION_KEY] = set()
            event.listen(sync, "after_commit", _committed)
            event.listen(sync, "after_rollback", _rolled_back)
        orgs.add(org_id)
    except Exception:  # noqa: BLE001 - a missed clear costs the mark's lifetime, never the write
        pass


def _committed(session) -> None:
    try:
        orgs = session.info.get(_SESSION_KEY)
        if not orgs:
            return
        credited_orgs = tuple(orgs)
        orgs.clear()
        task = asyncio.get_running_loop().create_task(
            clear(credited_orgs), name="treg-empty-balance-clear")
        _clears.add(task)
        task.add_done_callback(_clears.discard)
    except Exception:  # noqa: BLE001
        pass


def _rolled_back(session) -> None:
    orgs = session.info.get(_SESSION_KEY)
    if orgs:
        orgs.clear()
