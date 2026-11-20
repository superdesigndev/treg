"""The breaker's operator events: one `capacity {json}` line each, the shape `request` lines have.

An alert reads these, so each event means one thing: `lock_opened` is written once, when the
call path locks treg's own account, never again for a signature that meets the lock already in
place; `lock_cleared` once, when a probe's 2xx lifts it. Fields are scalars treg decided; a
vendor's body never appears (an error often echoes the request back), only the tripwire's phrase.
"""
from __future__ import annotations

import json

from ...infra.structured_logging import get_logger

log = get_logger("treg.capacity_events")


def emit(event: str, **fields) -> None:
    """Never raises: an event is for the operator, never this call's fate."""
    try:
        log.info("capacity %s", json.dumps({"event": event, **fields}, separators=(",", ":"),
                                            default=str))
    except Exception:  # noqa: BLE001
        pass
