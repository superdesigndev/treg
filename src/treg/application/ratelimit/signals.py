"""Surge signals: record-only request aggregates per IP, per org and per (IP, org).

The request path only touches process memory (`enter`, `leave`); nothing here can refuse a
request, and this module never imports the database. `signals_flush.flush` takes the window once
per `surge_flush_s` in the background (`swap`).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...config import get_settings

_CALL_PREFIXES = ("/call/", "/catalog/call/", "/table/", "/mcp")
_lock = threading.Lock()


@dataclass
class _Agg:
    requests: int = 0
    call_requests: int = 0
    unknown_key_requests: int = 0
    status_401: int = 0
    status_403: int = 0
    status_429: int = 0
    status_5xx: int = 0
    blocklist_matches: int = 0
    max_in_flight: int = 0
    orgs: set = field(default_factory=set)
    keys: set = field(default_factory=set)


@dataclass
class _Window:
    opened: datetime
    rows: dict = field(default_factory=dict)  # (scope, ip, org_id) -> _Agg
    dropped: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


_window = _Window(_minute(_now()))
_in_flight: dict[tuple, int] = {}


@dataclass
class Visit:
    ip: str
    org_id: int | None
    key_id: str | None
    path: str
    unknown: bool
    subjects: tuple = ()


def _agg(subject: tuple, *, count_drop: bool = True) -> _Agg | None:
    row = _window.rows.get(subject)
    if row is None:
        if len(_window.rows) >= get_settings().surge_max_subjects:
            _window.dropped += count_drop
            return None
        row = _window.rows[subject] = _Agg()
    return row


def _subjects(ip: str, org_id: int | None) -> tuple:
    if org_id is None:
        return (("ip", ip, None),)
    return (("ip", ip, None), ("org", None, org_id), ("ip_org", ip, org_id))


def enter(ip: str, org_id: int | None, key_id: str | None, path: str, *, unknown: bool) -> Visit | None:
    if not get_settings().surge_signals_enabled:
        return None
    visit = Visit(ip, org_id, key_id, path, unknown, _subjects(ip, org_id))
    with _lock:
        for subject in visit.subjects:
            _in_flight[subject] = n = _in_flight.get(subject, 0) + 1
            row = _agg(subject)
            if row is not None:
                row.max_in_flight = max(row.max_in_flight, n)
    return visit


def leave(visit: Visit | None, *, status: int, org_id: int | None, blocked: bool) -> None:
    if visit is None:
        return
    with _lock:
        for subject in visit.subjects:
            n = _in_flight.get(subject, 1) - 1
            if n > 0:
                _in_flight[subject] = n
            else:
                _in_flight.pop(subject, None)
        # Auth may have named the org only now (a key's first request in this process).
        subjects = visit.subjects if visit.org_id is not None or org_id is None else _subjects(visit.ip, org_id)
        org = visit.org_id or org_id
        for subject in subjects:
            row = _agg(subject, count_drop=subject not in visit.subjects)
            if row is None:
                continue
            row.requests += 1
            row.call_requests += visit.path.startswith(_CALL_PREFIXES)
            row.unknown_key_requests += visit.unknown and org is None
            row.status_401 += status == 401
            row.status_403 += status == 403
            row.status_429 += status == 429
            row.status_5xx += status >= 500
            row.blocklist_matches += blocked
            if org is not None:
                row.orgs.add(org)
            if visit.key_id:
                row.keys.add(visit.key_id[:16])


def swap() -> _Window:
    global _window
    with _lock:
        out, _window = _window, _Window(_minute(_now()))
    return out


def reset_for_tests() -> None:
    global _window
    with _lock:
        _window = _Window(_minute(_now()))
        _in_flight.clear()
