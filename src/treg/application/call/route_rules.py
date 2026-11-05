"""The published route rule set, read lazily from the shared Redis store into one worker snapshot.

A request after the refresh interval performs one bounded read, joined by concurrent requests.
Every failure keeps the last valid set: transport failure, invalid data, a rule naming what the
catalog does not serve, a lower version or different content at the same version. A healthy
missing key means no published rules. Rules only reorder candidates, so a stale set costs
ordering, never correctness.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from ...config import get_settings
from ...domain.catalog import store as catalog_store
from ...domain.catalog.routing import rules as route_rules
from ...infra import kv

_READ_TIMEOUT_S = 0.2
_LOG_INTERVAL_S = 60
_clock = time.monotonic
log = logging.getLogger("treg.route_rules")


def _emit(event: str, *, level: int = logging.INFO, **fields) -> None:
    try:
        log.log(level, "%s %s", event, json.dumps({"worker_pid": os.getpid(), **fields},
                                                 separators=(",", ":")))
    except Exception:  # noqa: BLE001 - observability cannot change a routing decision
        pass


def _store():
    return kv.store()


def _providers_of(capability: str) -> set[str]:
    return {e["provider"] for e in catalog_store.load().for_capability(capability)}


class Reader:
    def __init__(self) -> None:
        self.snapshot: route_rules.RuleSet | None = None
        self.next_refresh = 0.0
        self.next_failure_log = 0.0
        self.suppressed_failure_logs = 0
        self.lock = asyncio.Lock()

    def _failed(self, reason: str) -> None:
        if _clock() < self.next_failure_log:
            self.suppressed_failure_logs += 1
            return
        _emit("route_rules_config_unavailable", level=logging.WARNING, reason=reason,
              action="retain_previous" if self.snapshot else "yaml_order",
              rules_version=self.snapshot.version if self.snapshot else 0,
              suppressed_since_last_log=self.suppressed_failure_logs)
        self.suppressed_failure_logs = 0
        self.next_failure_log = _clock() + _LOG_INTERVAL_S

    async def _refresh(self, refresh_s: float) -> None:
        try:
            async with asyncio.timeout(_READ_TIMEOUT_S):
                raw = await _store().read_route_rules()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any read fault reads as unavailable
            raw = kv.FlowControlReadFailure.UNAVAILABLE
        self.next_refresh = _clock() + refresh_s
        if raw is kv.FlowControlReadFailure.UNAVAILABLE:
            self._failed("redis_unavailable")
            return
        if raw is kv.FlowControlReadFailure.INVALID:
            self._failed("invalid_config")
            return
        if raw is None:
            if self.snapshot is not None:
                _emit("route_rules_version_observed", rules_version=0)
            self.snapshot = None
            return
        try:
            parsed = route_rules.parse(raw)
            route_rules.check_against(parsed, catalog_store.load().contracts, _providers_of)
        except (ValueError, TypeError, RecursionError, OverflowError):
            self._failed("invalid_config")
            return
        current = self.snapshot
        if current is not None and (parsed.version < current.version or (
                parsed.version == current.version and parsed.canonical != current.canonical)):
            self._failed("version_conflict")
            return
        if current is None or parsed.version > current.version:
            self.snapshot = parsed
            _emit("route_rules_version_observed", rules_version=parsed.version,
                  enabled=parsed.enabled, rules=len(parsed.rules))

    async def current(self, refresh_s: float) -> route_rules.RuleSet | None:
        if self.lock.locked() or _clock() >= self.next_refresh:
            async with self.lock:
                if _clock() >= self.next_refresh:
                    await self._refresh(refresh_s)
        return self.snapshot


_reader = Reader()


def reset() -> None:
    global _reader
    _reader = Reader()


async def rule_for(capability: str, segment: dict) -> tuple[int, route_rules.Rule] | None:
    """(rule set version, rule) for this routed call, or None: no rule, or the feature is off."""
    settings = get_settings()
    if not settings.route_rules_enabled:
        return None
    try:
        snapshot = await _reader.current(settings.route_rules_refresh_s)
    except asyncio.CancelledError:
        raise
    except Exception:  # rules only reorder; the YAML order stands
        log.warning("route rules unavailable", exc_info=True)
        return None
    rule = route_rules.match(snapshot, capability, segment)
    return (snapshot.version, rule) if rule is not None and snapshot is not None else None
