"""Best-effort, process-local root-call logs. No request content or external I/O.

Arrivals belong to their start window; statuses/latencies to their completion window.
See docs/context/architecture/org-traffic.md for boundaries and bounded-loss semantics.
All mutations are synchronous on the worker's event loop (including the minute timer).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from heapq import nlargest

from ..call_surface import split_call_path
from .structured_logging import get_logger

WINDOW_S = 60
MAX_ORGS = 4096
# Upper bounds 1, 2, ... 2**31 milliseconds, then an overflow bucket.
BUCKETS = 33
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT",
                           "OPTIONS", "TRACE", "PATCH"})
log = get_logger("treg.org_traffic")
_adapter: ContextVar[dict | None] = ContextVar("org_traffic_adapter", default=None)


@dataclass
class Window:
    live: int = 0
    requests: int = 0
    completed: int = 0
    in_flight_max: int = 0
    status_429: int = 0
    status_503: int = 0
    latency: list[int] = field(default_factory=lambda: [0] * BUCKETS)

    def p95(self) -> int | None:
        rank = (95 * self.completed + 99) // 100
        if not rank:
            return None
        count = 0
        for index, samples in enumerate(self.latency):
            count += samples
            if count >= rank:
                return (1 << index) if index < BUCKETS - 1 else None
        return None


class Counters:
    def __init__(self) -> None:
        self.orgs: dict[int, Window] = {}
        self.overflow_requests = 0
        self.overflow_in_flight = 0
        self.overflow_completed = 0

    def begin(self, org_id: int) -> bool:
        row = self.orgs.get(org_id)
        if row is None:
            if len(self.orgs) >= MAX_ORGS:
                self.overflow_requests += 1
                self.overflow_in_flight += 1
                return False
            row = self.orgs[org_id] = Window()
        row.requests += 1
        row.live += 1
        row.in_flight_max = max(row.in_flight_max, row.live)
        return True

    def finish(self, org_id: int, status: int | None, elapsed_ms: float, *, tracked=True) -> None:
        if not tracked:
            self.overflow_in_flight -= 1
            self.overflow_completed += 1
            return
        row = self.orgs[org_id]
        row.live -= 1
        row.completed += 1
        row.status_429 += status == 429
        row.status_503 += status == 503
        bucket = min(BUCKETS - 1, max(0, math.ceil(elapsed_ms) - 1).bit_length())
        row.latency[bucket] += 1

    def snapshot(self) -> dict:
        top = nlargest(10, self.orgs.items(), key=lambda item: (item[1].requests, -item[0]))
        result = {"window_s": WINDOW_S, "overflow_requests": self.overflow_requests,
                  "overflow_in_flight": self.overflow_in_flight,
                  "overflow_completed": self.overflow_completed,
                  "top": [{"org_id": org_id, "requests": row.requests,
                           "completed": row.completed, "in_flight_max": row.in_flight_max,
                           "status_429": row.status_429, "status_503": row.status_503,
                           "p95_ms": row.p95()} for org_id, row in top]}
        # Live work outlives the arrival window. Never reset its gauge or evict its identity.
        self.orgs = {org_id: Window(live=row.live, in_flight_max=row.live)
                     for org_id, row in self.orgs.items() if row.live}
        self.overflow_requests = 0
        self.overflow_completed = 0
        return result


counters = Counters()
_timer: asyncio.TimerHandle | None = None
_users = 0


def _emit(event: str, fields: dict) -> None:
    try:
        log.info("%s %s", event, json.dumps({"worker_pid": os.getpid(), **fields},
                                          separators=(",", ":")))
    except Exception:  # noqa: BLE001 - telemetry must never fail a request
        pass


def emit_snapshot() -> None:
    try:
        _emit("org_traffic_top", {**counters.snapshot(), "window_end_unix_s": time.time()})
    except Exception:  # noqa: BLE001 - telemetry must never fail a request
        pass


def _tick() -> None:
    global _timer
    emit_snapshot()
    _timer = asyncio.get_running_loop().call_later(WINDOW_S, _tick)


def start_reporting() -> bool:
    """One timer per worker even if multiple app lifespans overlap; no per-request tasks."""
    global _timer, _users
    try:
        if _users == 0:
            _timer = asyncio.get_running_loop().call_later(WINDOW_S, _tick)
        _users += 1
        return True
    except Exception:  # noqa: BLE001 - telemetry must never fail a request
        return False


def stop_reporting() -> None:
    global _timer, _users
    _users -= 1
    if _users == 0 and _timer is not None:
        _timer.cancel()
        _timer = None


def identify(scope: dict, org_id: int, *, enter: bool = False,
             api_key_id: int | None = None) -> None:
    """Called only with trusted Caller IDs. Never derive identity from a header or URL."""
    try:
        observation = scope.get("state", {}).get("org_traffic")
        if observation is not None:
            observation.org_id = org_id
            observation.api_key_id = api_key_id
            if enter and observation.started is None:
                observation.started = time.perf_counter()
                observation.tracked = observation.counters.begin(org_id)
    except Exception:  # noqa: BLE001 - telemetry must never fail a request
        pass


def describe(scope: dict, *, tool_name: str | None, endpoint_id: str | None,
             replay_of: str | None, provider: str | None = None,
             credential_tier: str | None = None, refused_by: str | None = None) -> None:
    """Copy already-resolved application metadata without inspecting request bytes or ORM objects."""
    try:
        observation = scope.get("state", {}).get("org_traffic")
        if observation is not None:
            observation.tool_name = tool_name
            observation.endpoint_id = endpoint_id
            observation.replay_of = replay_of
            observation.provider = provider
            observation.credential_tier = credential_tier
            observation.refused_by = refused_by
    except Exception:  # noqa: BLE001 - telemetry must never fail a request
        pass


@dataclass
class Observation:
    surface: str
    adapter: str | None = None
    org_id: int | None = None
    api_key_id: int | None = None
    started: float | None = None
    tracked: bool | None = None
    counters: Counters = field(default_factory=lambda: counters)
    method: str | None = None
    tool_name: str | None = None
    endpoint_id: str | None = None
    replay_of: str | None = None
    provider: str | None = None
    credential_tier: str | None = None
    refused_by: str | None = None

    def finish(self, status: int | None, outcome: str, *, call_ref: str | None = None,
               failure_kind: str | None = None) -> None:
        elapsed = (time.perf_counter() - self.started) * 1000 if self.started is not None else None
        try:
            if self.tracked is not None:
                tracked, self.tracked = self.tracked, None
                self.counters.finish(self.org_id, status, elapsed, tracked=tracked)
        except Exception:  # noqa: BLE001 - telemetry must never fail a request
            pass
        _emit("request", {"surface": self.surface, "adapter": self.adapter,
                          "org_id": self.org_id, "api_key_id": self.api_key_id,
                          "call_ref": call_ref, "status": status, "outcome": outcome,
                          "failure_kind": failure_kind,
                          "method": self.method, "tool_name": self.tool_name,
                          "endpoint_id": self.endpoint_id, "replay_of": self.replay_of,
                          "provider": self.provider, "credential_tier": self.credential_tier,
                          "refused_by": self.refused_by,
                          "handler_ms": round(elapsed, 3) if elapsed is not None else None})


class RequestLogMiddleware:
    """Observe the existing ASGI lifecycle without buffering, swallowing, or adding awaits."""
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        call_path = split_call_path(scope.get("path", "")) if scope["type"] == "http" else None
        mcp_transport = scope["type"] == "http" and scope.get("path") in (
            "/mcp", "/mcp/", "/mcp/v2", "/mcp/v2/")
        if call_path is None and not mcp_transport:
            return await self.app(scope, receive, send)
        try:
            state = scope.setdefault("state", {})
            if "org_traffic" in state:
                observation = None  # A nested wrapper on the same root cannot count it twice.
            else:
                adapter = _adapter.get()
                method = scope.get("method")
                observation = Observation(call_path[0] if call_path else "mcp_transport",
                                          adapter["name"] if adapter else None,
                                          method=method if method in _HTTP_METHODS else None)
                state["org_traffic"] = observation
                if adapter is not None and call_path is not None:
                    adapter["forwarded"] = True
        except Exception:  # noqa: BLE001 - telemetry must never fail a request
            observation = None
        if observation is None:
            return await self.app(scope, receive, send)
        status = None
        outcome = "completed"

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            return await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            status = status if status is not None else 500
            raise
        finally:
            try:
                # Successful MCP envelopes are not another call. An eager auth challenge has no
                # adapter/root invocation, but still needs an explicitly unresolved identity.
                if not mcp_transport or status == 401:
                    observation.finish(status, outcome, call_ref=state.get("call_ref"),
                                       failure_kind=state.get("call_failure_kind"))
            except Exception:  # noqa: BLE001 - telemetry must never fail a request
                pass


def mcp_adapter(name: str):
    """One log for an adapter's root HTTP call, or an unattributed preflight exit."""
    def decorate(call):
        @wraps(call)
        async def observed(*args, **kwargs):
            marker = {"name": name, "forwarded": False}
            token = _adapter.set(marker)
            outcome = "preflight"
            try:
                return await call(*args, **kwargs)
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception:
                outcome = "error"
                raise
            finally:
                _adapter.reset(token)
                if not marker["forwarded"]:
                    _emit("request", {"surface": "mcp", "adapter": name, "org_id": None,
                                      "api_key_id": None, "call_ref": None,
                                      "method": None, "tool_name": None, "endpoint_id": None,
                                      "replay_of": None, "provider": None,
                                      "credential_tier": None, "refused_by": None,
                                      "status": None, "outcome": outcome, "handler_ms": None})
        return observed
    return decorate
