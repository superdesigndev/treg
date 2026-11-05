"""Published Org admission snapshots, read lazily from the shared Redis store.

Pause, rate and concurrency are active. Redis failures allow new
work; malformed publications retain the last valid rule
until its original expiry. Accepted work never enters this guard again. The bounded worker cache
is not durable version history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import get_settings
from . import kv
from .structured_logging import get_logger

_MAX_ORGS = 4096
_MAX_BYTES = 32768
_MAX_INT = 2147483647
_LOG_INTERVAL_S = 5
_READ_TIMEOUT_S = 0.2
_clock = time.monotonic


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


log = get_logger("treg.org_flow_control")


class PauseRejected(Exception):
    def __init__(self, *, retry_after_s: int, version: int) -> None:
        self.status_code = 429
        self.retry_after_s = retry_after_s
        self.detail = {"error": "org_traffic_paused",
                       "message": "organization new requests are temporarily paused",
                       "retry_after_s": retry_after_s, "config_version": version}
        super().__init__(self.detail["error"])


@dataclass(frozen=True)
class RateLimit:
    rps: float
    burst: int


@dataclass(frozen=True)
class Snapshot:
    version: int
    pause_until: datetime | None
    canonical: str
    max_in_flight: int | None = None
    rate_limit: RateLimit | None = None


def _integer(value) -> bool:
    return type(value) is int and 0 < value <= _MAX_INT


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration field")
        result[key] = value
    return result


def _parse(raw: str, org_id: int) -> Snapshot:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("invalid configuration size")
    data = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(data, dict) or set(data) != {"org_id", "version", "config"}:
        raise ValueError("invalid envelope")
    if not _integer(data["org_id"]) or data["org_id"] != org_id or not _integer(data["version"]):
        raise ValueError("invalid identity or version")
    config = data["config"]
    if (not isinstance(config, dict)
            or set(config) != {"schema_version", "pause_until", "rate_limit", "concurrency_limit"}
            or type(config["schema_version"]) is not int or config["schema_version"] != 1):
        raise ValueError("invalid configuration")
    pause = config["pause_until"]
    if pause is not None:
        if not isinstance(pause, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", pause):
            raise ValueError("invalid UTC pause")
        if pause.startswith("0000-"):
            # The publication schema accepts Gregorian year zero; it is always expired here.
            datetime.fromisoformat(("0400" + pause[4:]).replace("Z", "+00:00"))
            pause = datetime.min.replace(tzinfo=timezone.utc)
        else:
            pause = datetime.fromisoformat(pause.replace("Z", "+00:00"))
    rate = config["rate_limit"]
    if rate is not None:
        if (not isinstance(rate, dict) or set(rate) != {"rps", "burst"}
                or type(rate["rps"]) not in (int, float)
                or not math.isfinite(rate["rps"]) or not 0 < rate["rps"] <= 9007199254740991
                or not _integer(rate["burst"])):
            raise ValueError("invalid rate limit")
    concurrency = config["concurrency_limit"]
    if concurrency is not None and (
            not isinstance(concurrency, dict) or set(concurrency) != {"max_in_flight"}
            or not _integer(concurrency["max_in_flight"])):
        raise ValueError("invalid concurrency limit")
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return Snapshot(data["version"], pause, canonical,
                    concurrency["max_in_flight"] if concurrency else None,
                    RateLimit(rate["rps"], rate["burst"]) if rate else None)


def _emit(event: str, org_id: int, call_ref: str | None, *,
          level: int = logging.INFO, **fields) -> None:
    try:
        log.log(level, "%s %s", event, json.dumps(
            {"worker_pid": os.getpid(), "org_id": org_id, "call_ref": call_ref,
             "capability": "pause", "capabilities": ["pause", "rate", "concurrency"], **fields},
            separators=(",", ":")))
    except Exception:  # noqa: BLE001 - observability cannot change an admission decision
        pass  # Observability cannot change an admission decision.


@dataclass
class Entry:
    snapshot: Snapshot | None = None
    available: bool = False
    users: int = 0
    next_refresh: float = 0.0
    next_pause_log: float = 0.0
    suppressed_pause_logs: int = 0
    next_failure_log: float = 0.0
    suppressed_failure_logs: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Reader:
    def __init__(self, *, max_entries: int = _MAX_ORGS) -> None:
        self.entries: OrderedDict[int, Entry] = OrderedDict()
        self.max_entries = max_entries
        self.next_capacity_warning = 0.0
        self.reads = asyncio.Semaphore(32)

    def _entry(self, org_id: int, call_ref: str | None) -> Entry | None:
        entry = self.entries.get(org_id)
        if entry is not None:
            self.entries.move_to_end(org_id)
            return entry
        if len(self.entries) >= self.max_entries:
            now = _utcnow()
            victim = next((key for key, row in self.entries.items()
                           if row.users == 0 and not row.lock.locked() and (
                               row.snapshot is None or (row.snapshot.max_in_flight is None
                                   and row.snapshot.rate_limit is None and (
                                   row.snapshot.pause_until is None
                                   or row.snapshot.pause_until <= now)))), None)
            if victim is None:
                if _clock() >= self.next_capacity_warning:
                    self.next_capacity_warning = _clock() + _LOG_INTERVAL_S
                    _emit("org_flow_control_config_unavailable", org_id, call_ref,
                          level=logging.WARNING, reason="cache_capacity", action="allow")
                return None
            del self.entries[victim]
        entry = self.entries[org_id] = Entry()
        return entry

    def _failed(self, entry: Entry, org_id: int, call_ref: str | None, reason: str) -> None:
        if _clock() < entry.next_failure_log:
            entry.suppressed_failure_logs += 1
            return
        _emit("org_flow_control_config_unavailable", org_id, call_ref, level=logging.WARNING,
              reason=reason, action="retain_previous" if entry.available and entry.snapshot else "allow",
              config_version=entry.snapshot.version if entry.snapshot else 0,
              suppressed_since_last_log=entry.suppressed_failure_logs)
        entry.suppressed_failure_logs = 0
        entry.next_failure_log = _clock() + _LOG_INTERVAL_S

    async def _refresh(self, entry: Entry, org_id: int, call_ref: str | None,
                       refresh_s: float) -> None:
        # Commit shared state only after the read. A cancelled owner leaves the snapshot and
        # refresh deadline untouched, so the next caller/waiter can retry immediately.
        was_available = entry.available
        try:
            async with asyncio.timeout(_READ_TIMEOUT_S):
                async with self.reads:
                    raw = await kv.store().read_org_flow_control(org_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any read fault reads as unavailable
            raw = kv.FlowControlReadFailure.UNAVAILABLE
        entry.next_refresh = _clock() + refresh_s
        if raw is kv.FlowControlReadFailure.UNAVAILABLE:
            entry.available = False
            self._failed(entry, org_id, call_ref, "redis_unavailable")
            return
        entry.available = True
        if raw is None:
            if entry.snapshot is not None or not was_available:
                _emit("org_flow_control_version_observed", org_id, call_ref, config_version=0,
                      pause_until=None, max_in_flight=None, rate_limit=None)
            entry.snapshot = None
            return
        try:
            snapshot = _parse(raw, org_id)
        except (ValueError, TypeError, RecursionError, OverflowError):
            self._failed(entry, org_id, call_ref, "invalid_config")
            return
        current = entry.snapshot
        if current is not None and (snapshot.version < current.version or (
                snapshot.version == current.version and snapshot.canonical != current.canonical)):
            self._failed(entry, org_id, call_ref, "version_conflict")
            return
        if current is None or snapshot.version > current.version:
            entry.snapshot = snapshot
        if current is None or snapshot.version > current.version or not was_available:
            _emit("org_flow_control_version_observed", org_id, call_ref,
                  config_version=snapshot.version,
                  pause_until=snapshot.pause_until.isoformat() if snapshot.pause_until else None,
                  max_in_flight=snapshot.max_in_flight,
                  rate_limit={"rps": snapshot.rate_limit.rps, "burst": snapshot.rate_limit.burst}
                  if snapshot.rate_limit else None)

    async def check(self, org_id: int, call_ref: str | None, refresh_s: float) -> Snapshot | None:
        entry = self._entry(org_id, call_ref)
        if entry is None:
            return None
        entry.users += 1
        try:
            if entry.lock.locked() or _clock() >= entry.next_refresh:
                # All concurrent cold/stale requests join the same bounded read.
                async with entry.lock:
                    if _clock() >= entry.next_refresh:
                        await self._refresh(entry, org_id, call_ref, refresh_s)
            if not entry.available:
                return None
            snapshot = entry.snapshot
            if snapshot is not None and snapshot.pause_until is not None:
                remaining = (snapshot.pause_until - _utcnow()).total_seconds()
                if remaining > 0:
                    retry = max(1, math.ceil(remaining))
                    if _clock() >= entry.next_pause_log:
                        _emit("org_flow_control_paused", org_id, call_ref, status=429,
                              config_version=snapshot.version, retry_after_s=retry,
                              suppressed_since_last_log=entry.suppressed_pause_logs)
                        entry.suppressed_pause_logs = 0
                        entry.next_pause_log = _clock() + _LOG_INTERVAL_S
                    else:
                        entry.suppressed_pause_logs += 1
                    raise PauseRejected(retry_after_s=retry, version=snapshot.version)
            return snapshot
        finally:
            entry.users -= 1


_reader = Reader()


def reset() -> None:
    """Drop snapshots only when no admissions are active, including between isolated tests."""
    global _reader
    _reader = Reader()


async def check(org_id: int, call_ref: str | None = None) -> Snapshot | None:
    """Called once with trusted Caller identity, before root financial/provider work."""
    settings = get_settings()
    if not settings.org_flow_control_enabled:
        return
    return await _reader.check(org_id, call_ref, settings.org_flow_control_refresh_s)
