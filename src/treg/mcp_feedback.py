"""Optional MCP feedback hints, controlled by cached PostHog configuration."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import math
import time

import httpx

from .config import get_settings

FLAG = "mcp-feedback-hint"
DISTINCT_ID = "treg-mcp-feedback-hint"
HINT = (
    "Proactively share friction using treg's feedback tool. Small annoyances count: "
    "confusing results or charges, unclear instructions, or a limitation you worked around, "
    "even if your task succeeded. Describe what you observed; you do not need to prove a bug. "
    "Pass related call IDs in call_ids when available. Omit private data and report each "
    "issue once. Feedback helps the treg team improve the product; keep going and finish "
    "the user's task afterward."
)
REFRESH_SECONDS = 60
MAX_AGE_SECONDS = 90
_rate = 0.0
_updated_at = 0.0
_users = 0
_task: asyncio.Task | None = None


def _parse_rate(doc: dict) -> float:
    if doc.get("errorsWhileComputingFlags") or doc.get("quotaLimited"):
        return 0.0
    flag = doc.get("flags", {}).get(FLAG, {})
    if flag.get("enabled") is not True or flag.get("variant"):
        return 0.0
    payload = flag.get("metadata", {}).get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    rate = payload.get("sample_rate") if isinstance(payload, dict) else None
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 1:
        return 0.0
    return float(rate)


async def refresh(client: httpx.AsyncClient) -> None:
    """Failure disables hints; no customer identity or request data leaves through this read."""
    global _rate, _updated_at
    rate = 0.0
    settings = get_settings()
    if settings.posthog_key:
        try:
            response = await client.post(
                f"{settings.posthog_host.rstrip('/')}/flags?v=2",
                json={"api_key": settings.posthog_key, "distinct_id": DISTINCT_ID},
                timeout=3.0,
            )
            response.raise_for_status()
            rate = _parse_rate(response.json())
        except Exception:
            pass  # Optional product hint: neither log credentials nor fail a call.
    _rate, _updated_at = rate, time.monotonic()


def sampled(sample_id: str) -> bool:
    """Local, stable sampling; a stale cache or unconfigured deployment is off."""
    if not get_settings().posthog_key or time.monotonic() - _updated_at > MAX_AGE_SECONDS:
        return False
    bucket = int.from_bytes(hashlib.sha256(f"{FLAG}:{sample_id}".encode()).digest()[:8], "big")
    return bucket < _rate * 2**64


async def _poll() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            await refresh(client)
            await asyncio.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan():
    """Both MCP surfaces share one poller; the final owner cancels and awaits it."""
    global _users, _task, _rate, _updated_at
    _users += 1
    if _users == 1 and get_settings().posthog_key:
        _task = asyncio.create_task(_poll())
    try:
        yield
    finally:
        _users -= 1
        if _users == 0:
            task, _task = _task, None
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            _rate, _updated_at = 0.0, 0.0
