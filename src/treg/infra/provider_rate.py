"""Calls admitted on one treg-owned provider bucket inside its declared window, across every process.

One sorted set per smoothing bucket in the shared store (`infra/kv.py`), on the slot script org
concurrency uses: each admitted call is a member that expires `window_s` after it was admitted, by
the store's clock, so fewer than `limit` live members means the window has room. A refused call
adds no member. No shared store (`TREG_KV_URL` unset) or a store fault reads as `None`.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from . import kv

_DEADLINE_S = 0.2


async def admit(bucket: str, limit: int, window_s: float) -> Literal["admit", "over"] | None:
    """Whether the bucket's window has room for one more call; room is taken when it does."""
    if not kv.configured() or limit <= 0 or window_s <= 0:
        return None
    try:
        store = kv.store()
        async with asyncio.timeout(_DEADLINE_S):
            outcome = await store.acquire_slot(
                f"treg:provider-rate:v1:{bucket}", uuid.uuid4().hex,
                max(1, round(window_s * 1000)), limit)
    except Exception:  # noqa: BLE001 - a store fault is an unknown verdict, never a call fault
        return None
    return {"acquired": "admit", "busy": "over"}.get(outcome)
