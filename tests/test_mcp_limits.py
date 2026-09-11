"""Tests for MCP stream lifecycle limits (mcp_limits.py).

Verifies that the OOM-prevention mechanisms work:
1. Connection capacity limits (per-instance and per-org)
2. Lifetime timeout (force close after max time)
3. Idle timeout (close after no activity)
4. Cleanup on client disconnect
5. Metrics tracking
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from starlette.responses import Response
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from treg.mcp_limits import (
    ConnectionInfo,
    ConnectionTracker,
    MCPStreamLimitsMiddleware,
    get_tracker,
    reset_tracker,
    wrap_mcp_app,
)


@pytest.fixture(autouse=True)
def clean_tracker():
    """Reset the global tracker before each test."""
    reset_tracker()
    yield
    reset_tracker()


class TestConnectionTracker:
    """Unit tests for ConnectionTracker."""

    def test_can_accept_within_limits(self):
        tracker = ConnectionTracker(max_connections=10, max_connections_per_org=5)
        allowed, reason = tracker.can_accept("org-1")
        assert allowed
        assert reason == ""

    def test_can_accept_rejects_at_instance_capacity(self):
        tracker = ConnectionTracker(max_connections=2, max_connections_per_org=10)
        tracker.register("org-1", "GET", "/mcp/")
        tracker.register("org-2", "GET", "/mcp/")

        allowed, reason = tracker.can_accept("org-3")
        assert not allowed
        assert reason == "instance_capacity"

    def test_can_accept_rejects_at_org_capacity(self):
        tracker = ConnectionTracker(max_connections=100, max_connections_per_org=2)
        tracker.register("org-1", "GET", "/mcp/")
        tracker.register("org-1", "GET", "/mcp/")

        allowed, reason = tracker.can_accept("org-1")
        assert not allowed
        assert reason == "org_capacity"

        # Other orgs can still connect
        allowed, reason = tracker.can_accept("org-2")
        assert allowed

    def test_register_and_unregister(self):
        tracker = ConnectionTracker()
        info = tracker.register("org-1", "GET", "/mcp/")

        assert info.conn_id in tracker._connections
        assert info.conn_id in tracker._by_org["org-1"]
        assert len(tracker._connections) == 1

        tracker.unregister(info.conn_id)
        assert info.conn_id not in tracker._connections
        assert "org-1" not in tracker._by_org  # Empty set is removed
        assert len(tracker._connections) == 0

    def test_touch_updates_activity(self):
        tracker = ConnectionTracker()
        info = tracker.register("org-1", "GET", "/mcp/")
        original_activity = info.last_activity

        time.sleep(0.01)
        tracker.touch(info.conn_id)

        assert info.last_activity > original_activity

    def test_age_and_idle_calculations(self):
        tracker = ConnectionTracker()
        info = tracker.register("org-1", "GET", "/mcp/")

        time.sleep(0.02)
        assert info.age_s() >= 0.02
        assert info.idle_s() >= 0.02

        tracker.touch(info.conn_id)
        time.sleep(0.01)
        assert info.age_s() >= 0.03
        assert info.idle_s() >= 0.01  # Reset by touch

    def test_snapshot_returns_metrics(self):
        tracker = ConnectionTracker()
        tracker.register("org-1", "GET", "/mcp/")
        tracker.register("org-1", "POST", "/mcp/")
        tracker.register("org-2", "GET", "/mcp/")
        tracker.record_rejection("instance_capacity")
        tracker.record_close("lifetime")

        snapshot = tracker.snapshot()

        assert snapshot["active_connections"] == 3
        assert snapshot["total_connections"] == 3
        assert snapshot["by_method"] == {"GET": 2, "POST": 1}
        assert snapshot["org_count"] == 2
        assert snapshot["rejected_capacity"] == 1
        assert snapshot["closed_lifetime"] == 1
        assert "age_max_s" in snapshot
        assert "idle_max_s" in snapshot

    def test_snapshot_resets_counters(self):
        tracker = ConnectionTracker()
        tracker.record_rejection("instance_capacity")

        snapshot1 = tracker.snapshot()
        assert snapshot1["rejected_capacity"] == 1

        snapshot2 = tracker.snapshot()
        assert snapshot2["rejected_capacity"] == 0  # Reset after first read

    def test_null_org_is_allowed(self):
        tracker = ConnectionTracker(max_connections_per_org=1)
        tracker.register(None, "GET", "/mcp/")
        tracker.register(None, "GET", "/mcp/")

        # Null org should not be subject to per-org limit
        allowed, reason = tracker.can_accept(None)
        assert allowed


class TestMCPStreamLimitsMiddleware:
    """Integration tests for the ASGI middleware."""

    @staticmethod
    def make_app(
        *,
        response_delay: float = 0,
        disconnect_after: float | None = None,
    ) -> ASGIApp:
        """Create a test ASGI app that simulates MCP behavior."""

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                return

            # Simulate processing
            if response_delay > 0:
                await anyio.sleep(response_delay)

            # Send response
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"ok": true}',
            })

        return app

    @staticmethod
    def make_scope(
        method: str = "POST",
        path: str = "/mcp/",
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> Scope:
        return {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers or [],
        }

    @staticmethod
    async def call_app(
        app: ASGIApp,
        scope: Scope,
        *,
        disconnect_after: float | None = None,
    ) -> tuple[int, bytes]:
        """Call an ASGI app and return (status, body)."""
        response_started = False
        status = 0
        body_parts = []

        async def receive() -> Message:
            if disconnect_after is not None:
                await anyio.sleep(disconnect_after)
                return {"type": "http.disconnect"}
            await anyio.sleep(10)  # Wait indefinitely
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            nonlocal response_started, status
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        try:
            await app(scope, receive, send)
        except anyio.get_cancelled_exc_class():
            pass

        return status, b"".join(body_parts)

    @pytest.mark.asyncio
    async def test_allows_requests_within_capacity(self):
        tracker = ConnectionTracker(max_connections=10)
        app = self.make_app()
        middleware = MCPStreamLimitsMiddleware(app, tracker=tracker)
        scope = self.make_scope()

        status, body = await self.call_app(middleware, scope, disconnect_after=0.01)

        assert status == 200
        assert tracker._total_connections == 1
        assert len(tracker._connections) == 0  # Cleaned up after completion

    @pytest.mark.asyncio
    async def test_rejects_at_capacity(self):
        tracker = ConnectionTracker(max_connections=1)
        # Pre-register a connection
        tracker.register("org-1", "GET", "/mcp/")

        app = self.make_app()
        middleware = MCPStreamLimitsMiddleware(app, tracker=tracker)
        scope = self.make_scope()

        status, body = await self.call_app(middleware, scope, disconnect_after=0.01)

        assert status == 503
        assert b"capacity" in body
        assert tracker._rejected_capacity == 1

    @pytest.mark.asyncio
    async def test_rejects_at_org_capacity(self):
        tracker = ConnectionTracker(max_connections=100, max_connections_per_org=1)
        # Pre-register a connection for org-1
        tracker.register("org-1", "GET", "/mcp/")

        app = self.make_app()
        middleware = MCPStreamLimitsMiddleware(app, tracker=tracker)
        scope = self.make_scope(headers=[(b"x-treg-org", b"org-1")])

        status, body = await self.call_app(middleware, scope, disconnect_after=0.01)

        assert status == 503
        assert tracker._rejected_org_limit == 1

    @pytest.mark.asyncio
    async def test_extracts_org_from_headers(self):
        tracker = ConnectionTracker()
        app = self.make_app()
        middleware = MCPStreamLimitsMiddleware(app, tracker=tracker)
        scope = self.make_scope(headers=[(b"x-treg-org", b"my-org")])

        await self.call_app(middleware, scope, disconnect_after=0.01)

        # Check that the connection was registered with the org
        snapshot = tracker.snapshot()
        assert snapshot["total_connections"] == 1

    @pytest.mark.asyncio
    async def test_lifetime_timeout_closes_connection(self):
        tracker = ConnectionTracker(max_lifetime_s=0.05)

        async def slow_app(scope: Scope, receive: Receive, send: Send) -> None:
            # This app takes longer than the lifetime limit
            await anyio.sleep(0.2)
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b"done"})

        middleware = MCPStreamLimitsMiddleware(slow_app, tracker=tracker)
        scope = self.make_scope()

        # The middleware should cancel the app after lifetime expires
        status, body = await self.call_app(middleware, scope, disconnect_after=0.3)

        # Connection should have been closed due to lifetime
        assert tracker._closed_lifetime == 1
        assert len(tracker._connections) == 0

    @pytest.mark.asyncio
    async def test_idle_timeout_closes_get_connection(self):
        tracker = ConnectionTracker(idle_timeout_s=0.05, max_lifetime_s=10.0)

        async def idle_app(scope: Scope, receive: Receive, send: Send) -> None:
            # This app does nothing for a while (simulating idle SSE stream)
            await anyio.sleep(0.3)

        middleware = MCPStreamLimitsMiddleware(idle_app, tracker=tracker)
        scope = self.make_scope(method="GET")

        await self.call_app(middleware, scope, disconnect_after=0.5)

        # Connection should have been closed due to idle timeout
        assert tracker._closed_idle == 1

    @pytest.mark.asyncio
    async def test_client_disconnect_triggers_cleanup(self):
        tracker = ConnectionTracker()
        app_started = asyncio.Event()

        async def waiting_app(scope: Scope, receive: Receive, send: Send) -> None:
            app_started.set()
            # Wait for receive (which will return disconnect)
            msg = await receive()
            assert msg["type"] == "http.disconnect"

        middleware = MCPStreamLimitsMiddleware(waiting_app, tracker=tracker)
        scope = self.make_scope(method="GET")

        # Disconnect quickly
        await self.call_app(middleware, scope, disconnect_after=0.02)

        # Connection should have been cleaned up
        assert tracker._closed_disconnect == 1
        assert len(tracker._connections) == 0

    @pytest.mark.asyncio
    async def test_post_requests_not_subject_to_idle_timeout(self):
        # POST requests are short-lived tool calls, so idle timeout doesn't apply
        tracker = ConnectionTracker(idle_timeout_s=0.01, max_lifetime_s=10.0)

        async def slow_post_app(scope: Scope, receive: Receive, send: Send) -> None:
            await anyio.sleep(0.05)  # Longer than idle timeout
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = MCPStreamLimitsMiddleware(slow_post_app, tracker=tracker)
        scope = self.make_scope(method="POST")

        status, body = await self.call_app(middleware, scope, disconnect_after=0.1)

        # POST should complete normally without idle timeout
        assert status == 200
        assert tracker._closed_idle == 0

    @pytest.mark.asyncio
    async def test_concurrent_connection_tracking(self):
        tracker = ConnectionTracker(max_connections=10)
        app = self.make_app(response_delay=0.1)
        middleware = MCPStreamLimitsMiddleware(app, tracker=tracker)

        async def make_request():
            scope = self.make_scope()
            return await self.call_app(middleware, scope, disconnect_after=0.15)

        # Start multiple concurrent requests using asyncio.create_task
        tasks = [asyncio.create_task(make_request()) for _ in range(5)]

        # Give tasks time to start and register connections
        await anyio.sleep(0.05)
        assert len(tracker._connections) > 0, f"Expected active connections but found {len(tracker._connections)}"

        # Wait for all to complete
        await asyncio.gather(*tasks)
        assert len(tracker._connections) == 0
        assert tracker._total_connections == 5


class TestWrapMcpApp:
    """Tests for the wrap_mcp_app helper."""

    def test_wraps_app_with_middleware(self):
        async def inner_app(scope, receive, send):
            pass

        wrapped = wrap_mcp_app(inner_app)
        assert isinstance(wrapped, MCPStreamLimitsMiddleware)

    def test_uses_global_tracker_by_default(self):
        async def inner_app(scope, receive, send):
            pass

        wrapped = wrap_mcp_app(inner_app)
        assert wrapped.tracker is get_tracker()

    def test_accepts_custom_tracker(self):
        async def inner_app(scope, receive, send):
            pass

        custom_tracker = ConnectionTracker(max_connections=5)
        wrapped = wrap_mcp_app(inner_app, tracker=custom_tracker)
        assert wrapped.tracker is custom_tracker


class TestMetrics:
    """Tests for metrics and logging."""

    @pytest.mark.asyncio
    async def test_log_mcp_metrics_with_active_connections(self, caplog):
        from treg.mcp_limits import log_mcp_metrics

        tracker = get_tracker()
        tracker.register("org-1", "GET", "/mcp/")
        tracker.record_rejection("instance_capacity")

        with caplog.at_level("INFO", logger="treg.mcp_limits"):
            await log_mcp_metrics()

        # Should have logged metrics
        assert any("mcp_stream_metrics" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_log_mcp_metrics_silent_when_empty(self, caplog):
        from treg.mcp_limits import log_mcp_metrics

        # No connections, no rejections
        with caplog.at_level("INFO", logger="treg.mcp_limits"):
            await log_mcp_metrics()

        # Should NOT have logged (nothing interesting)
        assert not any("mcp_stream_metrics" in record.message for record in caplog.records)
