"""Calls admitted on one treg-owned provider bucket inside its declared window, across every process.

One sorted set per smoothing bucket and declared rate in the shared store (`infra/kv.py`): each
admitted call is a member that expires `window_s` after it was admitted, by the store's clock, so
fewer than `limit` live members means the window has room. A refused call adds no member and learns
when the oldest one expires. A changed rate starts its own window. No shared store
(`TREG_KV_URL` unset) or a store fault reads as `None`; after a fault the store is not asked for
`COOLDOWN_S`, then one call probes it while the others still skip it.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from . import kv

COOLDOWN_S = 5.0
log = logging.getLogger("treg.provider_rate")
_down_until = 0.0
_probing = False
_pending: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Admission:
    verdict: Literal["admit", "over"]
    key: str
    token: str
    retry_after_s: int = 0


def _key(bucket: str, limit: int, window_s: float) -> tuple[str, int]:
    window_ms = max(1, round(window_s * 1000))
    return f"treg:provider-rate:v2:{bucket}:{limit}/{window_ms}", window_ms


async def admit(bucket: str, limit: int, window_s: float) -> Admission | None:
    """Whether the bucket's window has room for one more call; room is taken when it does."""
    global _down_until, _probing
    if not kv.configured() or limit <= 0 or window_s <= 0:
        return None
    now = time.monotonic()
    if now < _down_until or _probing:
        return None
    probe = _down_until > 0.0
    _probing = probe
    key, window_ms = _key(bucket, limit, window_s)
    token = uuid.uuid4().hex
    try:
        store = kv.store()
        result = await store.rate_window(key, token, window_ms, limit)
    except Exception:  # noqa: BLE001 - a store that cannot initialize reads as unavailable
        result = kv.RateWindowResult("unavailable")
    finally:
        if probe:
            _probing = False
    if result.outcome == "unavailable":
        if _down_until <= time.monotonic():
            log.warning("provider_rate store_unavailable; skipping it for %ss", COOLDOWN_S)
        _down_until = time.monotonic() + COOLDOWN_S
        return None
    _down_until = 0.0
    return Admission(result.outcome, key, token, max(1, math.ceil(result.retry_after_ms / 1000))
                     if result.outcome == "over" else 0)


def release(admission: Admission | None) -> None:
    """Give an admitted call's member back (the call was refused before it was sent). Sync."""
    if admission is None or admission.verdict != "admit":
        return
    task = asyncio.get_running_loop().create_task(_release(admission))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _release(admission: Admission) -> None:
    try:
        store = kv.store()
        await store.release_slot(admission.key, admission.token)
    except Exception:  # noqa: BLE001 - an unreleased member expires with its window
        pass


def reset() -> None:
    global _down_until, _probing
    _down_until, _probing = 0.0, False
