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
from sqlalchemy.orm import Session

from ..config import get_settings
from . import kv

# How long a process keeps asking the store about a team after it last saw that team refused.
_SUSPECT_S = 60.0
_SUSPECTS_KEPT = 10_000
_SESSION_KEY = "treg_empty_balance_credited"
# Below the store's own limit for optional work, so this limit is the one that ends a read of a
# store that hangs, and the call knows the store did not answer.
_READ_WAIT_S = 0.15
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


class Read(NamedTuple):
    """`read`'s answer. `answered` is False only when the store was asked and did not answer in
    time or failed: the call then leaves the store alone for the rest of its reserve."""

    mark: Mark | None
    answered: bool = True


async def read(org_id: int) -> Read:
    """The team's mark, if any: none when switched off, for a team this process has not seen
    refused lately (the store is not asked), with no mark, or with a store that cannot answer."""
    if ttl_s() <= 0 or _suspects.get(org_id, 0.0) <= time.monotonic():
        return Read(None)
    try:
        async with asyncio.timeout(_READ_WAIT_S):
            text = await kv.store().get_text(_key(org_id))
        if not text:
            return Read(None)
        required, balance = (int(part) for part in text.split(":"))
    except Exception:  # noqa: BLE001 - an unread mark is a missing one
        return Read(None, answered=False)
    return Read(Mark(required, balance) if required > 0 else None)


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
        if sync is not None:
            sync.info.setdefault(_SESSION_KEY, set()).add(org_id)
    except Exception:  # noqa: BLE001 - a missed clear costs the mark's lifetime, never the write
        pass


def _committed(session) -> None:
    """The credited teams' marks are deleted in the background, and this process stops asking
    the store about them at once: a reserve that follows the credit in the same request (a
    routed call's next child after a released one) must not meet a mark whose delete is still
    on its way."""
    try:
        orgs = session.info.get(_SESSION_KEY)
        if not orgs:
            return
        credited_orgs = tuple(orgs)
        orgs.clear()
        for org_id in credited_orgs:
            _suspects.pop(org_id, None)
        task = asyncio.get_running_loop().create_task(
            clear(credited_orgs), name="treg-empty-balance-clear")
        _clears.add(task)
        task.add_done_callback(_clears.discard)
    except Exception:  # noqa: BLE001
        pass


def _rolled_back(session) -> None:
    try:
        orgs = session.info.get(_SESSION_KEY)
        if orgs:
            orgs.clear()
    except Exception:  # noqa: BLE001
        pass


# One listener for every session, registered once: a session that credited nothing costs one
# dictionary lookup at its commit.
event.listen(Session, "after_commit", _committed)
event.listen(Session, "after_rollback", _rolled_back)
