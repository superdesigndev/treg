"""Bounded local observability, using in-process HTTP/MCP and synthetic organizations only."""
import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from treg import mcp
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.infra import org_traffic as traffic
from treg.routers import call as routes
from test_marketplace_call import platform_on  # noqa: F401
from test_org_concurrency import gate, _configure_org  # noqa: F401
from test_routing import enrichment_on  # noqa: F401


@pytest.fixture(autouse=True)
def capture_traffic_logs(caplog):
    traffic.log.addHandler(caplog.handler)
    try:
        yield
    finally:
        traffic.log.removeHandler(caplog.handler)


@pytest.fixture
def counter(monkeypatch):
    counter = traffic.Counters()
    monkeypatch.setattr(traffic, "counters", counter)
    return counter


def logs(caplog, event="request"):
    return [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
            if record.name == "treg.org_traffic" and record.getMessage().startswith(event + " ")]


def test_aggregation_top_ten_and_histogram(counter):
    for org in range(1, 13):
        for _ in range(org):
            assert counter.begin(org)
            counter.finish(org, 429 if org % 2 else 503, 65)
    rows = counter.snapshot()["top"]
    assert [row["org_id"] for row in rows] == list(range(12, 2, -1))
    assert rows[0] == {"org_id": 12, "requests": 12, "completed": 12, "in_flight_max": 1,
                       "status_429": 0, "status_503": 12, "p95_ms": 128}
    assert rows[1]["status_429"] == 11
    assert counter.snapshot()["top"] == []


def test_percentile_nearest_rank_and_overflow(counter):
    for ms in [1] * 19 + [1025]:
        counter.begin(1)
        counter.finish(1, 200, ms)
    assert counter.snapshot()["top"][0]["p95_ms"] == 1
    counter.begin(1)
    counter.finish(1, 200, 2**31 + 1)
    assert counter.snapshot()["top"][0]["p95_ms"] is None


def test_summary_is_one_parseable_physical_line(counter, monkeypatch, caplog):
    stream = io.StringIO()
    handler = next(handler for handler in traffic.log.handlers if handler is not caplog.handler)
    monkeypatch.setattr(handler, "stream", stream)
    for org in range(10):
        counter.begin(org)
        counter.finish(org, 503, 65)
    traffic.emit_snapshot()
    [line] = stream.getvalue().splitlines()
    event, payload = line.split(" ", 1)
    assert event == "org_traffic_top"
    assert len(json.loads(payload)["top"]) == 10


def test_cross_window_in_flight_and_completion_denominator(counter):
    counter.begin(7)
    counter.begin(7)
    first = counter.snapshot()["top"][0]
    assert first["requests"] == first["in_flight_max"] == 2
    assert first["completed"] == 0 and first["p95_ms"] is None
    counter.finish(7, 503, 120000)
    second = counter.snapshot()["top"][0]
    assert second["requests"] == 0 and second["completed"] == second["status_503"] == 1
    assert second["in_flight_max"] == 2
    counter.begin(7)
    counter.finish(7, None, 10)  # cancellation is a completion without an HTTP status
    counter.finish(7, 200, 10)
    third = counter.snapshot()["top"][0]
    assert third["requests"] == 1 and third["completed"] == third["in_flight_max"] == 2
    assert not counter.orgs


def test_bounded_storage_and_overflow_are_explicit(counter, monkeypatch):
    monkeypatch.setattr(traffic, "MAX_ORGS", 2)
    for org in (1, 2):
        counter.begin(org)
    for org in range(3, 1000):
        assert not counter.begin(org)
    for _ in range(10000):
        counter.begin(1)
        counter.finish(1, 200, 12)
    assert len(counter.orgs) == 2
    assert all(len(row.latency) == 33 for row in counter.orgs.values())
    assert counter.snapshot()["overflow_requests"] == 997
    assert counter.snapshot()["overflow_in_flight"] == 997
    for org in range(3, 1000):
        counter.finish(org, 200, 10, tracked=False)
    overflow = counter.snapshot()
    assert overflow["overflow_completed"] == 997 and overflow["overflow_in_flight"] == 0
    assert len(counter.orgs) == 2  # live owners cannot be evicted at rollover
    counter.finish(1, 200, 20)
    counter.finish(2, 200, 20)
    counter.snapshot()
    assert counter.begin(3)  # inactive identities are reclaimed, not leaked
    assert list(counter.orgs) == [3]


async def test_one_minute_timer_shared_and_removed(counter, monkeypatch, caplog):
    handles = []

    class Handle:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    def later(delay, callback):
        assert delay == 60
        handle = Handle()
        handle.callback = callback
        handles.append(handle)
        return handle

    monkeypatch.setattr(traffic, "_users", 0)
    monkeypatch.setattr(traffic, "_timer", None)
    monkeypatch.setattr(asyncio.get_running_loop(), "call_later", later)
    assert traffic.start_reporting() and traffic.start_reporting()
    assert len(handles) == 1
    handles[0].callback()
    assert len(logs(caplog, "org_traffic_top")) == 1
    assert len(handles) == 2
    traffic.stop_reporting()
    assert not handles[1].cancelled
    traffic.stop_reporting()
    assert handles[1].cancelled and traffic._timer is None
    assert traffic.start_reporting()
    assert len(handles) == 3
    traffic.stop_reporting()


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
async def test_trusted_org_log_and_unresolved_null(clients, counter, monkeypatch, caplog, path):
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    response = await clients.get(path + "no-such-tool/path?secret=must-not-log",
                                 headers={"X-Treg-Org-Id": "999999", "User-Agent": "must-not-log"})
    assert response.status_code == 404
    [row] = logs(caplog)
    assert row["org_id"] == org and row["status"] == 404
    assert "must-not-log" not in json.dumps(logs(caplog))
    assert counter.snapshot()["top"][0]["requests"] == 1
    caplog.clear()
    response = await clients.get(path + "no-such-tool/path", headers={"X-Treg-Token": "invalid"})
    assert response.status_code == 401
    [row] = logs(caplog)
    assert row["org_id"] is None and row["handler_ms"] is None
    assert counter.snapshot()["top"] == []
    assert "must-not-log" not in json.dumps(logs(caplog))


@pytest.mark.parametrize("failure,status", [(RuntimeError("fault"), 500), (PoolTimeoutError(), 503)])
async def test_exception_or_pool_timeout_keeps_status_and_finishes(
    clients, counter, monkeypatch, caplog, failure, status,
):
    monkeypatch.setattr(routes, "execute_call", AsyncMock(side_effect=failure))
    if status == 500:
        with pytest.raises(RuntimeError, match="fault"):
            await clients.get("/call/example/path")
    else:
        assert (await clients.get("/call/example/path")).status_code == status
    [row] = counter.snapshot()["top"]
    assert row["requests"] == row["completed"] == 1
    assert row["status_503"] == (status == 503)
    assert logs(caplog)[-1]["status"] == status
    assert not counter.orgs


@pytest.mark.parametrize("ending", ["success", "error", "cancel"])
async def test_stream_lifetime_across_windows(clients, counter, monkeypatch, ending, caplog):
    entered, proceed = asyncio.Event(), asyncio.Event()
    closed = AsyncMock()

    async def body():
        entered.set()
        yield b"first"
        await proceed.wait()
        if ending == "error":
            raise RuntimeError("stream fault")
        yield b"last"

    monkeypatch.setattr(routes, "execute_call", AsyncMock(return_value=UpstreamResponse(
        200, (), body(), closed)))
    task = asyncio.create_task(clients.get("/call/example/path"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert counter.snapshot()["top"][0]["in_flight_max"] == 1
        assert next(iter(counter.orgs.values())).live == 1
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            proceed.set()
            if ending == "error":
                with pytest.raises(RuntimeError, match="stream fault"):
                    await task
            else:
                assert (await task).content == b"firstlast"
        closed.assert_awaited_once()
        [row] = counter.snapshot()["top"]
        assert row["requests"] == 0 and row["completed"] == row["in_flight_max"] == 1
        assert not counter.orgs
        assert logs(caplog)[-1]["status"] == 200  # don't invent a status after headers were sent
    finally:
        proceed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_guard_log_has_effective_limit_and_aggregate(clients, counter, gate, caplog):
    from treg.infra import org_concurrency as guard

    org, _ = await _configure_org(clients, gate)
    held = await guard.acquire(org)
    try:
        assert (await clients.get("/call/example/path")).status_code == 429
        assert f"rejected org_id={org} limit=1 status=429" in caplog.text
        [row] = counter.snapshot()["top"]
        assert row["requests"] == row["completed"] == row["status_429"] == 1
    finally:
        await held.finish()


async def test_routed_children_count_one_root(clients, counter, enrichment_on, monkeypatch):
    from treg.application.call import service
    from test_routing import _relay_by_provider, ROUTED

    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "person@example.com", "score": 99,
                                  "verification": {"status": "valid"}}})]}, seen))
    response = await clients.post(f"/call/{ROUTED}", json={"full_name": "Test", "domain": "example.com"})
    assert response.status_code == 200, response.text
    assert seen
    [row] = counter.snapshot()["top"]
    assert row["requests"] == row["completed"] == 1


@pytest.mark.parametrize("path,tool,args", [
    ("/mcp/", "call", {"endpoint_id": "tikhub.tiktok.video.comments"}),
    ("/mcp/v2/", "catalog_call_read", {"endpoint_id": "tikhub.tiktok.video.comments"}),
    ("/mcp/v2/", "catalog_call_write", {"endpoint_id": "dataforseo.web.page.audit"}),
    ("/mcp/", "call_media", {"endpoint_id": "fishaudio.tts.s2-1-pro", "body": {"text": "test"}}),
])
async def test_mcp_logs_and_counts_one_root(clients, counter, gate, caplog, path, tool, args):
    from test_mcp_directory import paired_mcp_session, _call_tool
    from treg.infra import org_concurrency as guard

    org, _ = await _configure_org(clients, gate)
    held = await guard.acquire(org)
    try:
        async with paired_mcp_session() as client:
            out = await _call_tool(client, tool, args, clients.headers["X-Treg-Token"], path=path)
        assert out["status"] == 429
        [row] = logs(caplog)
        assert row["org_id"] == org and row["adapter"] in ("call", "call_media")
        [row] = counter.snapshot()["top"]
        assert row["requests"] == row["completed"] == 1
    finally:
        await held.finish()


async def test_mcp_preflight_has_null_identity(counter, caplog):
    out = await mcp._call_impl("example/path", ctx=SimpleNamespace(headers={}), catalog_only=False,
                               allowed_methods=None, surface=mcp._TEAM_SURFACE)
    assert out["error"] == "not authenticated"
    assert logs(caplog)[0]["org_id"] is None
    assert counter.snapshot()["top"] == []


@pytest.mark.parametrize("path", ["/mcp/", "/mcp/v2/"])
async def test_mcp_eager_auth_challenge_logs_null(clients, counter, caplog, path):
    response = await clients.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    assert response.status_code == 401
    [row] = logs(caplog)
    assert row["org_id"] is None and row["status"] == 401
    assert counter.snapshot()["top"] == []


async def test_disabled_surface_logs_resolved_org_without_handler_arrival(clients, counter, caplog):
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    assert (await clients.get("/table/example/path")).status_code == 404
    [row] = logs(caplog)
    assert row["org_id"] == org and row["handler_ms"] is None
    assert counter.snapshot()["top"] == []


async def test_observability_failure_does_not_change_request(clients, counter, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(counter, "begin", broken)
    monkeypatch.setattr(traffic.log, "info", broken)
    assert (await clients.get("/call/example/path")).status_code == 404
    traffic.emit_snapshot()


async def test_guard_logging_failure_preserves_rejection(gate, monkeypatch):
    from treg.infra import org_concurrency as guard

    gate[1].acquire_result = "busy"
    monkeypatch.setattr(guard.log, "warning", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(guard.AdmissionRejected) as error:
        await guard.acquire(1)
    assert error.value.status_code == 429
