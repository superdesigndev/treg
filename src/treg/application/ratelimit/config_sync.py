"""Loading the admission policy from Postgres, its only source, into `config_state`.

Every `rate_limit_config_refresh_s` each process reads the newest `RateLimitConfigVersion` row (by
primary key order, one row) on the dedicated `ratelimit` pool, bounded by
`rate_limit_blocklist_poll_timeout_s`. Workers never read the Redis copy, so a stale writer that
lands in Redis after a restart or a race is never served: the version a worker runs is always
one Postgres had.
"""

from __future__ import annotations

import asyncio

from sqlmodel import select

from ...config import get_settings
from ...infra.db import ratelimit_session_maker
from ...models import RateLimitConfigVersion
from . import config_state


async def _read() -> tuple[int, dict] | None:
    async with ratelimit_session_maker() as db:
        row = (await db.execute(select(RateLimitConfigVersion.id, RateLimitConfigVersion.config)
                                .order_by(RateLimitConfigVersion.id.desc()).limit(1))).first()
        await db.commit()
    return (int(row[0]), row[1]) if row else None


async def poll_once() -> config_state.ConfigState:
    try:
        async with asyncio.timeout(get_settings().rate_limit_blocklist_poll_timeout_s):
            head = await _read()
    except Exception as exc:  # noqa: BLE001 - includes the timeout; the last good policy stays
        return config_state.fail(f"policy read failed: {type(exc).__name__}: {exc}"[:300])
    if head is None:
        return config_state.no_policy()
    return config_state.install(*head)
