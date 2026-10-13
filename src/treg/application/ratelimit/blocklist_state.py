"""The process's blocklist: one in-memory `Blocklist`, replaced only by `blocklist_sync`.

The request path reads `current()` and nothing else. Before a first successful load the process
has no list (`loaded` is false): it fails open and `blocklist_cache_empty` is reported until the
background poll succeeds. A failed poll never replaces or empties a loaded list.
"""

from __future__ import annotations

import json
import logging
import time

from ...domain.ratelimit import Blocklist

log = logging.getLogger("treg.ratelimit")
_clock = time.monotonic

_list: Blocklist | None = None
_loaded_at = 0.0
_counts = {"blocklist_load_errors": 0, "blocklist_reloads": 0}


def current() -> Blocklist | None:
    return _list


def counters() -> dict:
    return dict(_counts)


def status(*, now: float) -> dict:
    return {"blocklist_loaded": _list is not None, "blocklist_cache_empty": _list is None,
            "blocklist_version": _list.version if _list else None,
            "blocklist_entries": _list.active(now=now) if _list else None,
            "blocklist_age_s": round(_clock() - _loaded_at) if _list else None}


def install(new: Blocklist) -> None:
    global _list, _loaded_at
    if _list is None or new.version != _list.version:
        log.info("rate_limit_blocklist_changed %s", json.dumps({
            "event": "rate_limit_blocklist_changed", "version": new.version,
            "previous_version": _list.version if _list else None,
            "entries": new.active(now=time.time()),
        }, separators=(",", ":")))
        _counts["blocklist_reloads"] += 1
    _list, _loaded_at = new, _clock()


def touch() -> None:
    """The version is unchanged: the cached list is current as of now."""
    global _loaded_at
    if _list is not None:
        _loaded_at = _clock()


def note_error() -> None:
    _counts["blocklist_load_errors"] += 1


def reset_for_tests() -> None:
    global _list, _loaded_at
    _list, _loaded_at = None, 0.0
    for k in _counts:
        _counts[k] = 0
