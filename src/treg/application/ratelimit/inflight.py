"""The process's in-flight request count: two integer updates per request, nothing else.

Counted for every HTTP request whether or not the overload cap is on, so the cap can be sized from
measured data. `background.inflight_loop` samples it with the DB pools' checked-out counts.
"""

from __future__ import annotations

_current = 0
_peak = 0


def enter() -> None:
    global _current, _peak
    _current += 1
    if _current > _peak:
        _peak = _current


def leave() -> None:
    global _current
    _current = max(0, _current - 1)


def current() -> int:
    return _current


def take_peak() -> int:
    """The highest count since the last call, then start a new window at the current count."""
    global _peak
    peak, _peak = _peak, _current
    return peak


def reset_for_tests() -> None:
    global _current, _peak
    _current = _peak = 0
