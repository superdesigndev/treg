"""The published route rule set, read from the shared Redis store into one worker snapshot.

A plan never waits for Redis: it uses the snapshot the worker holds, and one past the refresh
interval starts a single background refresh. A cold worker plans in the YAML order until its first
read lands. Every failure keeps the last valid set: transport failure, invalid data, a rule that
cannot take effect, a lower version or different content at the same version. A healthy missing
key means no published rules. An unchanged value is not parsed or checked again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass

from ...config import get_settings
from ...domain.catalog import store as catalog_store
from ...domain.catalog.routing import rules as route_rules
from ...domain.catalog.routing.contracts import adapter_accepts, canonical_identity
from ...infra import kv
from .. import call_events

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


def _servers_of(capability: str, input_type: str | None) -> set[str]:
    """Providers a routed plan for this capability can ask: its routed endpoint's children, and of
    those, for an identity variant, the ones whose adapter accepts some call recorded as it (the
    widest such call, after the contract's derivations)."""
    cat = catalog_store.load()
    contract = cat.contracts.get(capability)
    identity = None
    if input_type is not None:
        fields = call_events.recorded_variants(contract).get(input_type) if contract else None
        if fields is None:
            return set()
        identity, _ = canonical_identity(contract, fields)
    out = set()
    for e in cat.for_capability(capability):
        if e.get("kind") != "routed":
            continue
        for child in e.get("routed_children") or ():
            ad = cat.adapters.get(child)
            if child not in cat.by_id or ad is None:
                continue
            if identity is None or adapter_accepts(ad, identity) is not None:
                out.add(cat.by_id[child]["provider"])
    return out


@dataclass(frozen=True)
class Decision:
    """What the published set says about one call: the version this worker holds (0: none
    published) and the rule that matched, if any."""
    version: int
    rule: route_rules.Rule | None


class Reader:
    def __init__(self) -> None:
        self.snapshot: route_rules.RuleSet | None = None
        self.observed = False   # a read has landed: no snapshot then means none published
        self.last_raw: str | None = None
        self.last_catalog: object | None = None
        self.next_refresh = 0.0
        self.task: asyncio.Task | None = None
        self.next_failure_log = 0.0
        self.suppressed_failure_logs = 0

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

    async def _refresh(self) -> None:
        try:
            async with asyncio.timeout(_READ_TIMEOUT_S):
                raw = await _store().read_route_rules()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any read fault reads as unavailable
            raw = kv.FlowControlReadFailure.UNAVAILABLE
        if raw is kv.FlowControlReadFailure.UNAVAILABLE:
            self._failed("redis_unavailable")
            return
        if raw is kv.FlowControlReadFailure.INVALID:
            self._failed("invalid_config")
            return
        if raw is None:
            if self.snapshot is not None or not self.observed:
                _emit("route_rules_version_observed", rules_version=0)
            self.snapshot, self.observed, self.last_raw = None, True, None
            return
        cat = catalog_store.load()
        if raw == self.last_raw and cat is self.last_catalog:
            return
        try:
            parsed = route_rules.parse(raw)
            route_rules.check_against(parsed, cat.contracts, _servers_of, call_events.segment_checks)
        except (ValueError, TypeError, RecursionError, OverflowError):
            self._failed("invalid_config")
            return
        current = self.snapshot
        if current is not None and (parsed.version < current.version or (
                parsed.version == current.version and parsed.canonical != current.canonical)):
            self._failed("version_conflict")
            return
        self.last_raw, self.last_catalog, self.observed = raw, cat, True
        if current is None or parsed.version > current.version:
            self.snapshot = parsed
            _emit("route_rules_version_observed", rules_version=parsed.version,
                  enabled=parsed.enabled, rules=len(parsed.rules))

    def current(self, refresh_s: float) -> tuple[bool, route_rules.RuleSet | None]:
        """(a read has landed, the snapshot), at once; a stale snapshot starts one refresh."""
        if _clock() >= self.next_refresh and (self.task is None or self.task.done()):
            self.next_refresh = _clock() + refresh_s
            self.task = asyncio.get_running_loop().create_task(self._refresh())
        return self.observed, self.snapshot


_reader = Reader()


def reset() -> None:
    global _reader
    if _reader.task is not None and not _reader.task.done():
        try:
            _reader.task.cancel()
        except RuntimeError:  # its loop already closed (between isolated tests)
            pass
    _reader = Reader()


def enabled() -> bool:
    return get_settings().route_rules_enabled


def decide(capability: str, segment: dict) -> Decision | None:
    """The published set's say on this routed call; None when the feature is off or this worker
    has not read the set yet."""
    settings = get_settings()
    if not settings.route_rules_enabled:
        return None
    try:
        observed, snapshot = _reader.current(settings.route_rules_refresh_s)
    except Exception:  # rules only reorder; the YAML order stands
        log.warning("route rules unavailable", exc_info=True)
        return None
    if not observed:
        return None
    return Decision(snapshot.version if snapshot else 0, route_rules.match(snapshot, capability, segment))
