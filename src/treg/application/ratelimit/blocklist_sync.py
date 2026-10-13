"""Loading the blocklist from Postgres, its only source, into `blocklist_state`.

Every `rate_limit_blocklist_refresh_s` each process reads the single `BlocklistVersion` row by
primary key on the dedicated `ratelimit` pool (one connection, statement and connect timeouts),
and the whole poll is abandoned past `rate_limit_blocklist_poll_timeout_s`. Only a changed version
reloads, and the reload reads the version, the mode and the active entries in ONE statement, so
the cache can never pair a new version with old entries. A reload with more entries than
`rate_limit_blocklist_max_entries` is refused whole: the last good list stays and the process
alerts. A failed poll keeps the cached list and logs and alerts once per distinct failure.

At startup `first_load` makes one attempt capped at `rate_limit_blocklist_first_load_s`, then the
worker serves either way: loaded, or failing open while the background loop retries and reports
`blocklist_cache_empty`. Health checks never wait on any of this.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import timezone

from sqlalchemy import and_, select

from ... import analytics
from ...config import get_settings
from ...domain.ratelimit import BlockEntry, Blocklist
from ...infra.db import ratelimit_session_maker
from ...models import BlocklistEntry, BlocklistVersion
from ...timeutil import utcnow_naive
from . import blocklist_state

log = logging.getLogger("treg.ratelimit")
_last_problem: str | None = None


class TooManyEntries(Exception):
    pass


def _epoch(t) -> float:
    return t.replace(tzinfo=timezone.utc).timestamp()


async def _read_version(db) -> tuple[int, str] | None:
    row = (await db.execute(select(BlocklistVersion.version, BlocklistVersion.mode)
                            .where(BlocklistVersion.id == 1))).first()
    return (int(row[0]), row[1]) if row else None


async def _read_snapshot(db) -> Blocklist:
    """Version, mode and active entries from one statement: one consistent snapshot."""
    limit = get_settings().rate_limit_blocklist_max_entries
    now = utcnow_naive()
    e = BlocklistEntry
    rows = (await db.execute(
        select(BlocklistVersion.version, BlocklistVersion.mode, e.kind, e.value, e.reason,
               e.expires_at, e.approved_by)
        .select_from(BlocklistVersion)
        .outerjoin(e, and_(e.revoked_at.is_(None), e.expires_at > now))
        .where(BlocklistVersion.id == 1)
        .limit(limit + 1)
    )).all()
    if not rows:
        raise LookupError("the blocklistversion row is missing (migration 0072 inserts it)")
    entries = [r for r in rows if r.kind is not None]
    if len(entries) > limit:
        raise TooManyEntries(f"more than {limit} active entries (rate_limit_blocklist_max_entries)")
    version, mode = int(rows[0].version), rows[0].mode
    return Blocklist.build(version, now.isoformat() + "Z", tuple(
        BlockEntry(r.kind, r.value, r.reason, _epoch(r.expires_at), r.approved_by)
        for r in entries if r.kind in ("key_hash", "org_id")), mode=mode)


async def _poll() -> None:
    async with ratelimit_session_maker() as db:
        head = await _read_version(db)
        held = blocklist_state.current()
        if head is not None and held is not None and (held.version, held.mode) == head:
            await db.commit()
            blocklist_state.touch()
            return
        snapshot = await _read_snapshot(db)
        await db.commit()
    blocklist_state.install(snapshot)


def _fail(exc: BaseException) -> None:
    global _last_problem
    blocklist_state.note_error()
    why = f"{type(exc).__name__}: {exc}"[:300]
    if why == _last_problem:
        return
    _last_problem = why
    held = blocklist_state.current()
    fields = {"event": "rate_limit_blocklist_load_failed", "reason": why,
              "kept_version": held.version if held else None}
    log.error("rate_limit_blocklist_load_failed %s", json.dumps(fields, separators=(",", ":")))
    analytics.capture(analytics.SERVER_DISTINCT_ID, "ratelimit_blocklist_load_failed", fields)


async def poll_once() -> bool:
    """One bounded poll. True when the cache now holds the current list (reloaded or unchanged)."""
    global _last_problem
    try:
        async with asyncio.timeout(get_settings().rate_limit_blocklist_poll_timeout_s):
            await _poll()
    except Exception as exc:  # noqa: BLE001 - includes the poll's TimeoutError; the cache is kept
        _fail(exc)
        return False
    _last_problem = None
    return True


def report_empty() -> None:
    """While no list has loaded: the `blocklist_cache_empty` gauge, once per poll."""
    if blocklist_state.current() is None:
        log.warning("blocklist_cache_empty %s", json.dumps({"event": "blocklist_cache_empty", "value": 1},
                                                           separators=(",", ":")))
        analytics.capture(analytics.SERVER_DISTINCT_ID, "blocklist_cache_empty", {"value": 1})


async def first_load() -> bool:
    """The startup attempt: one poll, capped. Never raises; the worker serves either way."""
    started = time.monotonic()
    budget = get_settings().rate_limit_blocklist_first_load_s
    try:
        async with asyncio.timeout(budget):
            ok = await poll_once()
    except TimeoutError:
        ok = False
        _fail(TimeoutError(f"first load exceeded {budget} s"))
    held = blocklist_state.current()
    fields = {"event": "rate_limit_blocklist_first_load", "pid": os.getpid(), "ok": ok,
              "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
              "version": held.version if held else None,
              "entries": held.active(now=time.time()) if held else None}
    (log.info if ok else log.warning)("rate_limit_blocklist_first_load %s",
                                      json.dumps(fields, separators=(",", ":")))
    if not ok:
        report_empty()
    return ok


def reset_for_tests() -> None:
    global _last_problem
    _last_problem = None
