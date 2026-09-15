"""MCP stream lifecycle limits — prevent OOM from long-lived connections.

Production OOM pattern: long-lived GET /mcp/ connections (Cursor, claude-code, opencode) hold
SSE streams open for minutes to hours. Each holds request/response buffers that are only freed
when the client disconnects. On a 499 (client disconnect), cleanup may not be prompt.

This module wraps the MCP ASGI app with:
1. Connection lifetime cap — force-close after max_lifetime_s
2. Idle timeout — close after no activity for idle_timeout_s
3. Per-instance concurrent stream limit — reject new connections when at capacity
4. Per-org concurrent stream limit — fair sharing across teams
5. Prompt cleanup on disconnect — ensure buffers are freed
6. Metrics/logging for monitoring

The design is an ASGI middleware that:
- Tracks active connections in process-local state
- Uses anyio cancel scopes for timeout enforcement
- Logs connection lifecycle events for observability
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakSet

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from anyio.abc import CancelScope

logger = logging.getLogger("treg.mcp_limits")

# Defaults tuned for the incident: 4 GiB instance, ~1 GiB baseline, leaves ~3 GiB for MCP
# With ~10 MiB worst-case per connection (large SSE buffers), 200 connections = 2 GiB headroom
DEFAULT_MAX_CONNECTIONS: Final = 200
DEFAULT_MAX_CONNECTIONS_PER_ORG: Final = 20
DEFAULT_MAX_LIFETIME_S: Final = 1800.0  # 30 minutes
DEFAULT_IDLE_TIMEOUT_S: Final = 300.0   # 5 minutes of no activity


@dataclass
class ConnectionInfo:
    """Metadata for one active MCP connection."""
    conn_id: str
    org_slug: str | None
    started_at: float
    last_activity: float
    method: str
    path: str
    cancel_scope: CancelScope | None = None

    def age_s(self) -> float:
        return time.monotonic() - self.started_at

    def idle_s(self) -> float:
        return time.monotonic() - self.last_activity


@dataclass
class ConnectionTracker:
    """Process-local tracking of active MCP connections.

    Thread-safe through asyncio's single-threaded event loop model. All access
    is from coroutines on the same loop.
    """
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    max_connections_per_org: int = DEFAULT_MAX_CONNECTIONS_PER_ORG
    max_lifetime_s: float = DEFAULT_MAX_LIFETIME_S
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S

    _connections: dict[str, ConnectionInfo] = field(default_factory=dict)
    _by_org: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _counter: int = 0

    # Metrics counters (reset on read for gauge-style reporting)
    _total_connections: int = 0
    _rejected_capacity: int = 0
    _rejected_org_limit: int = 0
    _closed_lifetime: int = 0
    _closed_idle: int = 0
    _closed_disconnect: int = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"mcp-{self._counter}"

    def can_accept(self, org_slug: str | None) -> tuple[bool, str]:
        """Check if a new connection can be accepted. Returns (allowed, reason)."""
        if len(self._connections) >= self.max_connections:
            return False, "instance_capacity"
        if org_slug and len(self._by_org.get(org_slug, set())) >= self.max_connections_per_org:
            return False, "org_capacity"
        return True, ""

    def register(self, org_slug: str | None, method: str, path: str) -> ConnectionInfo:
        """Register a new connection. Caller must check can_accept first."""
        conn_id = self._next_id()
        now = time.monotonic()
        info = ConnectionInfo(
            conn_id=conn_id,
            org_slug=org_slug,
            started_at=now,
            last_activity=now,
            method=method,
            path=path,
        )
        self._connections[conn_id] = info
        if org_slug:
            self._by_org[org_slug].add(conn_id)
        self._total_connections += 1
        return info

    def unregister(self, conn_id: str) -> ConnectionInfo | None:
        """Remove a connection from tracking."""
        info = self._connections.pop(conn_id, None)
        if info and info.org_slug:
            self._by_org[info.org_slug].discard(conn_id)
            if not self._by_org[info.org_slug]:
                del self._by_org[info.org_slug]
        return info

    def touch(self, conn_id: str) -> None:
        """Update last activity time for idle timeout tracking."""
        if info := self._connections.get(conn_id):
            info.last_activity = time.monotonic()

    def record_rejection(self, reason: str) -> None:
        if reason == "instance_capacity":
            self._rejected_capacity += 1
        elif reason == "org_capacity":
            self._rejected_org_limit += 1

    def record_close(self, reason: str) -> None:
        if reason == "lifetime":
            self._closed_lifetime += 1
        elif reason == "idle":
            self._closed_idle += 1
        elif reason == "disconnect":
            self._closed_disconnect += 1

    def snapshot(self) -> dict[str, Any]:
        """Current state for logging/metrics. Resets rejection counters."""
        by_method = defaultdict(int)
        by_org = defaultdict(int)
        ages = []
        idles = []
        for info in self._connections.values():
            by_method[info.method] += 1
            if info.org_slug:
                by_org[info.org_slug] += 1
            ages.append(info.age_s())
            idles.append(info.idle_s())

        result = {
            "active_connections": len(self._connections),
            "total_connections": self._total_connections,
            "by_method": dict(by_method),
            "org_count": len(self._by_org),
            "rejected_capacity": self._rejected_capacity,
            "rejected_org_limit": self._rejected_org_limit,
            "closed_lifetime": self._closed_lifetime,
            "closed_idle": self._closed_idle,
            "closed_disconnect": self._closed_disconnect,
        }
        if ages:
            result["age_max_s"] = max(ages)
            result["age_avg_s"] = sum(ages) / len(ages)
            result["idle_max_s"] = max(idles)
            result["idle_avg_s"] = sum(idles) / len(idles)

        # Reset counters after snapshot (gauge-style)
        self._rejected_capacity = 0
        self._rejected_org_limit = 0
        self._closed_lifetime = 0
        self._closed_idle = 0
        self._closed_disconnect = 0
        return result


# Process-global tracker instance
_tracker: ConnectionTracker | None = None


def get_tracker() -> ConnectionTracker:
    """Get or create the process-global connection tracker."""
    global _tracker
    if _tracker is None:
        from .config import get_settings
        settings = get_settings()
        _tracker = ConnectionTracker(
            max_connections=getattr(settings, 'mcp_max_connections', DEFAULT_MAX_CONNECTIONS),
            max_connections_per_org=getattr(settings, 'mcp_max_connections_per_org', DEFAULT_MAX_CONNECTIONS_PER_ORG),
            max_lifetime_s=getattr(settings, 'mcp_max_lifetime_s', DEFAULT_MAX_LIFETIME_S),
            idle_timeout_s=getattr(settings, 'mcp_idle_timeout_s', DEFAULT_IDLE_TIMEOUT_S),
        )
    return _tracker


def reset_tracker() -> None:
    """Reset the tracker (for testing)."""
    global _tracker
    _tracker = None


def _extract_org_from_headers(scope: Scope) -> str | None:
    """Extract org slug from request headers if available.

    MCP requests may carry X-Treg-Org or have it set by RequireAuthForProtectedTools
    after OAuth validation.
    """
    for key, value in scope.get("headers", []):
        if key.lower() == b"x-treg-org":
            return value.decode("latin-1", errors="replace")
    return None


class MCPStreamLimitsMiddleware:
    """ASGI middleware enforcing MCP connection limits and timeouts.

    Wraps the MCP app to:
    1. Reject connections when at capacity (503)
    2. Track active connections per instance and per org
    3. Enforce max lifetime with cancel scope
    4. Enforce idle timeout with periodic checks
    5. Log connection lifecycle events
    6. Ensure cleanup on any exit path
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        tracker: ConnectionTracker | None = None,
    ):
        self.app = app
        self._tracker = tracker

    @property
    def tracker(self) -> ConnectionTracker:
        return self._tracker or get_tracker()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        method = scope.get("method", "")
        path = scope.get("path", "")

        # Only apply limits to MCP streaming endpoints
        # POST requests are short-lived tool calls; GET requests hold SSE streams
        if method not in ("GET", "POST"):
            return await self.app(scope, receive, send)

        org_slug = _extract_org_from_headers(scope)
        tracker = self.tracker

        # Check capacity
        allowed, reason = tracker.can_accept(org_slug)
        if not allowed:
            tracker.record_rejection(reason)
            logger.warning(
                "mcp_connection_rejected",
                extra={
                    "reason": reason,
                    "org": org_slug,
                    "method": method,
                    "path": path,
                    "active": len(tracker._connections),
                },
            )
            response = Response(
                '{"error": "Service temporarily at capacity"}',
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "5"},
            )
            return await response(scope, receive, send)

        # Register connection
        conn_info = tracker.register(org_slug, method, path)
        logger.debug(
            "mcp_connection_start",
            extra={
                "conn_id": conn_info.conn_id,
                "org": org_slug,
                "method": method,
                "path": path,
                "active": len(tracker._connections),
            },
        )

        close_reason = "completed"
        try:
            # Create a cancel scope for lifetime limit
            async with anyio.create_task_group() as tg:
                conn_info.cancel_scope = tg.cancel_scope

                # Set lifetime deadline
                lifetime_deadline = time.monotonic() + tracker.max_lifetime_s
                tg.cancel_scope.deadline = lifetime_deadline

                # Wrap receive to track activity and detect disconnect
                disconnected = False

                async def tracked_receive() -> Message:
                    nonlocal disconnected, close_reason
                    try:
                        msg = await receive()
                        tracker.touch(conn_info.conn_id)

                        # Check for client disconnect
                        if msg.get("type") == "http.disconnect":
                            disconnected = True
                            close_reason = "disconnect"
                            tracker.record_close("disconnect")
                            # Cancel the scope to stop the app
                            tg.cancel_scope.cancel()

                        return msg
                    except Exception:
                        disconnected = True
                        close_reason = "disconnect"
                        tracker.record_close("disconnect")
                        raise

                # Wrap send to track activity
                async def tracked_send(message: Message) -> None:
                    tracker.touch(conn_info.conn_id)
                    await send(message)

                # Start idle timeout checker for long-lived connections (GET)
                if method == "GET":
                    async def idle_checker():
                        while True:
                            await anyio.sleep(min(30.0, tracker.idle_timeout_s / 2))
                            if disconnected:
                                return
                            idle = conn_info.idle_s()
                            if idle >= tracker.idle_timeout_s:
                                nonlocal close_reason
                                close_reason = "idle"
                                tracker.record_close("idle")
                                logger.info(
                                    "mcp_connection_idle_timeout",
                                    extra={
                                        "conn_id": conn_info.conn_id,
                                        "idle_s": idle,
                                        "age_s": conn_info.age_s(),
                                    },
                                )
                                tg.cancel_scope.cancel()
                                return

                    tg.start_soon(idle_checker)

                try:
                    await self.app(scope, tracked_receive, tracked_send)
                except anyio.get_cancelled_exc_class():
                    # Check which limit triggered cancellation
                    if tg.cancel_scope.cancel_called:
                        if close_reason == "idle":
                            pass  # Already logged
                        elif close_reason == "disconnect":
                            pass  # Already logged
                        elif time.monotonic() >= lifetime_deadline:
                            close_reason = "lifetime"
                            tracker.record_close("lifetime")
                            logger.info(
                                "mcp_connection_lifetime_exceeded",
                                extra={
                                    "conn_id": conn_info.conn_id,
                                    "age_s": conn_info.age_s(),
                                    "max_lifetime_s": tracker.max_lifetime_s,
                                },
                            )
                    raise

        except anyio.get_cancelled_exc_class():
            # Cancellation from our limits is expected, don't propagate
            if close_reason in ("lifetime", "idle", "disconnect"):
                pass
            else:
                raise
        except Exception:
            close_reason = "error"
            logger.exception(
                "mcp_connection_error",
                extra={"conn_id": conn_info.conn_id},
            )
            raise
        finally:
            # Always unregister and log
            tracker.unregister(conn_info.conn_id)
            logger.debug(
                "mcp_connection_end",
                extra={
                    "conn_id": conn_info.conn_id,
                    "reason": close_reason,
                    "age_s": round(conn_info.age_s(), 2),
                    "active": len(tracker._connections),
                },
            )


def wrap_mcp_app(app: ASGIApp, *, tracker: ConnectionTracker | None = None) -> ASGIApp:
    """Wrap an MCP ASGI app with stream limits middleware."""
    return MCPStreamLimitsMiddleware(app, tracker=tracker)


async def log_mcp_metrics() -> None:
    """Log current MCP connection metrics. Call periodically from a background task."""
    tracker = get_tracker()
    snapshot = tracker.snapshot()
    if snapshot["active_connections"] > 0 or snapshot["rejected_capacity"] > 0:
        logger.info("mcp_stream_metrics", extra=snapshot)


async def metrics_worker(interval_s: float = 60.0) -> None:
    """Background task that logs MCP metrics periodically."""
    while True:
        try:
            await log_mcp_metrics()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mcp_metrics_worker error")
        await anyio.sleep(interval_s)
