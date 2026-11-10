"""Bounded local observability, using in-process HTTP/MCP and synthetic organizations only."""
import asyncio
import io
import json
import logging
import subprocess
import sys
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
    caplog.set_level(logging.INFO, logger=traffic.log.name)
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


@pytest.fixture
async def caller_key_id(clients):
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    keys = (await clients.get(f"/orgs/{org}/api-keys")).json()
    return next(key["id"] for key in keys if key["kind"] == "default_human")


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


@pytest.mark.parametrize("configuration", ["mcp", "host", "managed"])
def test_event_logging_preserves_root_configuration_and_single_lines(configuration):
    script = '''
import io, json, logging, sys
root = logging.getLogger()
stream, root_stream = io.StringIO(), io.StringIO()
configuration = sys.argv[1]
if configuration == "host":
    handler = logging.StreamHandler(root_stream)
    handler.setFormatter(logging.Formatter("HOST:%(levelname)s:%(message)s"))
    handler.addFilter(logging.Filter())
    root.addHandler(handler)
    root.setLevel(logging.ERROR)
elif configuration == "mcp":
    from mcp.server import MCPServer
    MCPServer(name="logging-baseline")
else:
    # A host may own logging and suppress SDK defaults, including leaving root unconfigured.
    import mcp.server.mcpserver.server as server
    server.configure_logging = lambda *args, **kwargs: None
baseline = (root.level, list(root.handlers), [h.formatter for h in root.handlers],
            [list(h.filters) for h in root.handlers],
            logging.getLogger("unrelated.library").isEnabledFor(logging.INFO))
from treg.api import app
from treg.infra import org_flow_control as flow, org_traffic as traffic
from treg.config import get_settings
assert not get_settings().org_flow_control_enabled
assert (root.level, list(root.handlers), [h.formatter for h in root.handlers],
        [list(h.filters) for h in root.handlers],
        logging.getLogger("unrelated.library").isEnabledFor(logging.INFO)) == baseline
assert not flow.log.propagate and not traffic.log.propagate
assert flow.log.level == traffic.log.level == logging.NOTSET
for logger in (flow.log, traffic.log):
    [handler] = logger.handlers
    assert type(handler) is logging.StreamHandler
    handler.setStream(stream)
# Parent-level filtering remains effective without touching root or library loggers.
logging.getLogger("treg").setLevel(logging.WARNING)
flow._emit("org_flow_control_paused", 42, None)
traffic.emit_snapshot()
assert not stream.getvalue()
logging.getLogger("treg").setLevel(logging.INFO)
flow._emit("org_flow_control_paused", 42, "synthetic" * 100)
traffic.emit_snapshot()
lines = stream.getvalue().splitlines()
assert len(lines) == 2
assert json.loads(lines[0].split(" ", 1)[1])["call_ref"] == "synthetic" * 100
assert lines[1].startswith("org_traffic_top ")
json.loads(lines[1].split(" ", 1)[1])
assert not root_stream.getvalue()  # No duplicate propagation to the root sink.
assert root.level == baseline[0] and root.handlers == baseline[1]
'''
    result = subprocess.run([sys.executable, "-c", script, configuration],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("propagate", [False, True])
def test_explicit_named_event_logger_configuration_is_preserved(monkeypatch, propagate):
    from treg.infra.structured_logging import get_logger

    stream = io.StringIO()
    logger = logging.getLogger("treg.test_structured")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("CUSTOM:%(message)s"))
    handler.addFilter(lambda record: "allowed" in record.getMessage())
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "level", logging.WARNING)
    monkeypatch.setattr(logger, "propagate", propagate)
    assert get_logger(logger.name) is logger
    assert logger.handlers == [handler]
    assert logger.level == logging.WARNING
    assert logger.propagate is propagate
    logger.info("allowed but below configured level")
    logger.warning("filtered")
    logger.warning("allowed")
    assert stream.getvalue() == "CUSTOM:allowed\n"


def test_event_logger_import_does_not_configure_bare_root():
    script = '''
import logging
root = logging.getLogger()
assert root.level == logging.WARNING and root.handlers == []
from treg.infra import org_flow_control, org_traffic
from treg.infra.structured_logging import get_logger
assert root.level == logging.WARNING and root.handlers == []
for logger in (org_flow_control.log, org_traffic.log):
    assert get_logger(logger.name) is logger
    assert len(logger.handlers) == 1
    assert logger.level == logging.NOTSET
    assert not logger.isEnabledFor(logging.INFO)
    assert logger.isEnabledFor(logging.WARNING)
assert not logging.getLogger("unrelated.library").isEnabledFor(logging.INFO)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr


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
async def test_trusted_org_log_and_unresolved_null(
    clients, counter, caller_key_id, monkeypatch, caplog, path,
):
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    response = await clients.get(path + "no-such-tool/path?secret=must-not-log",
                                 headers={"X-Treg-Org-Id": "999999", "User-Agent": "must-not-log",
                                          "X-Treg-Call-Id": "forged-call", "X-Treg-Api-Key-Id": "999999"})
    assert response.status_code == 404
    [row] = logs(caplog)
    assert row["org_id"] == org and row["status"] == 404
    assert row["api_key_id"] == caller_key_id
    assert row["call_ref"] == response.headers["X-Treg-Call-Id"] != "forged-call"
    assert row["method"] == "GET"
    assert row["tool_name"] is None and row["endpoint_id"] is None and row["replay_of"] is None
    assert "must-not-log" not in json.dumps(logs(caplog))
    assert counter.snapshot()["top"][0]["requests"] == 1
    caplog.clear()
    response = await clients.get(path + "no-such-tool/path", headers={"X-Treg-Token": "invalid"})
    assert response.status_code == 401
    [row] = logs(caplog)
    assert row["org_id"] is None and row["handler_ms"] is None
    assert row["api_key_id"] is None
    assert row["tool_name"] is None and row["endpoint_id"] is None and row["replay_of"] is None
    assert row["call_ref"] == response.headers["X-Treg-Call-Id"]
    assert counter.snapshot()["top"] == []
    assert "must-not-log" not in json.dumps(logs(caplog))


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
@pytest.mark.parametrize("failure,status,failure_kind", [
    (RuntimeError("fault"), 500, "unexpected_exception"),
    (PoolTimeoutError(), 503, "db_pool"),
])
async def test_exception_or_pool_timeout_keeps_status_and_finishes(
    clients, counter, caller_key_id, monkeypatch, caplog, posthog_events, path, failure, status,
    failure_kind,
):
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    execute = AsyncMock(side_effect=failure)
    monkeypatch.setattr(routes, "execute_call", execute)
    if status == 500:
        with pytest.raises(RuntimeError, match="fault"):
            await clients.get(path + "example/path")
    else:
        assert (await clients.get(path + "example/path")).status_code == status
    [row] = counter.snapshot()["top"]
    assert row["requests"] == row["completed"] == 1
    assert row["status_503"] == (status == 503)
    [logged] = logs(caplog)
    assert logged["status"] == status
    assert logged["api_key_id"] == caller_key_id
    assert logged["call_ref"] == execute.await_args.args[0].call_ref
    [event] = await posthog_events()
    assert logged["failure_kind"] == event["properties"]["failure_kind"] == failure_kind
    assert not counter.orgs


@pytest.mark.parametrize("path", ["/call/", "/catalog/call/", "/table/"])
async def test_pool_timeout_before_identity_logs_intake_failure_kind(
    clients, counter, monkeypatch, caplog, posthog_events, path,
):
    from treg.domain.identity import access

    monkeypatch.setattr(get_settings(), "table_enabled", True)
    with monkeypatch.context() as patch:
        patch.setattr(access, "_membership_by_token", AsyncMock(side_effect=PoolTimeoutError()))
        response = await clients.get(path + "example/path")
    assert response.status_code == 503
    assert response.json()["treg_saturated"] is True
    assert response.headers["Retry-After"] == "2"
    [logged] = logs(caplog)
    [event] = await posthog_events("call_intake_failed")
    assert logged["status"] == 503
    assert logged["failure_kind"] == event["properties"]["failure_kind"] == "db_pool"
    assert logged["call_ref"] == response.headers["X-Treg-Call-Id"]
    assert logged["org_id"] is None and logged["api_key_id"] is None
    assert await posthog_events() == []
    assert counter.snapshot()["top"] == []

    # A later request on the same client must not inherit the previous request's failure.
    caplog.clear()
    response = await clients.get(path + "example/path")
    assert response.status_code == 404
    [logged] = logs(caplog)
    assert logged["failure_kind"] is None


@pytest.mark.parametrize("ending", ["success", "error", "cancel"])
async def test_stream_lifetime_across_windows(
    clients, counter, caller_key_id, monkeypatch, ending, caplog,
):
    entered, proceed = asyncio.Event(), asyncio.Event()
    closed = AsyncMock()

    async def body():
        entered.set()
        yield b"first"
        await proceed.wait()
        if ending == "error":
            raise RuntimeError("stream fault")
        yield b"last"

    async def invoke(context, _client):
        context.tool_name = "example"
        context.endpoint_id = "example.read"
        return UpstreamResponse(
            200, ((b"x-treg-call-id", b"untrusted-upstream-id"),
                  (b"x-treg-idempotent-replay", b"true")), body(), closed)

    execute = AsyncMock(side_effect=invoke)
    monkeypatch.setattr(routes, "execute_call", execute)
    task = asyncio.create_task(clients.get("/call/example/path"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert counter.snapshot()["top"][0]["in_flight_max"] == 1
        assert next(iter(counter.orgs.values())).live == 1
        assert not logs(caplog)  # Correlation is emitted after the same stream/cleanup boundary.
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
        [logged] = logs(caplog)
        assert logged["status"] == 200  # don't invent a status after headers were sent
        assert logged["api_key_id"] == caller_key_id
        assert logged["call_ref"] == execute.await_args.args[0].call_ref != "untrusted-upstream-id"
        assert logged["replay_of"] is None
        assert logged["tool_name"] == "example" and logged["endpoint_id"] == "example.read"
    finally:
        proceed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_guard_log_has_effective_limit_and_aggregate(clients, counter, caller_key_id, gate, caplog):
    from treg.infra import org_concurrency as guard

    org, _ = await _configure_org(clients, gate)
    held = await guard.acquire(org, limit=1, version=1)
    try:
        response = await clients.get("/call/example/path")
        assert response.status_code == 429
        [logged] = logs(caplog)
        assert logged["api_key_id"] == caller_key_id
        assert logged["call_ref"] == response.headers["X-Treg-Call-Id"]
        assert logged["tool_name"] is None and logged["endpoint_id"] is None
        assert f"rejected org_id={org} limit=1 status=429" in caplog.text
        [row] = counter.snapshot()["top"]
        assert row["requests"] == row["completed"] == row["status_429"] == 1
    finally:
        await held.finish()


async def test_rate_refusal_logs_one_trusted_root_and_counts_429(
    clients, counter, caller_key_id, gate, caplog,
):
    from treg.infra import kv, org_flow_control as flow

    org, store = await _configure_org(clients, gate, limit=None, rate={"rps": 0.25, "burst": 1})
    store.admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=4)
    flow.log.addHandler(caplog.handler)
    try:
        response = await clients.get("/call/example/path?secret=must-not-log", headers={
            "X-Treg-Org-Id": "999999", "X-Treg-Call-Id": "forged-call",
        })
        assert response.status_code == 429 and response.headers["Retry-After"] == "4"
        [logged] = logs(caplog)
        assert logged["org_id"] == org and logged["api_key_id"] == caller_key_id
        assert logged["call_ref"] == response.headers["X-Treg-Call-Id"] != "forged-call"
        assert logged["tool_name"] is None and logged["endpoint_id"] is None
        [summary] = counter.snapshot()["top"]
        assert summary["requests"] == summary["completed"] == summary["status_429"] == 1
        [event] = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
                   if record.name == flow.log.name
                   and record.getMessage().startswith("org_flow_control_rate_limited ")]
        assert event["org_id"] == org and event["config_version"] == 1
        assert event["rps"] == 0.25 and event["burst"] == 1
        assert event["retry_after_s"] == 4 and event["status"] == 429
        assert "must-not-log" not in json.dumps(event)
    finally:
        flow.log.removeHandler(caplog.handler)


async def test_rate_warning_volume_is_bounded_and_suppression_is_reported(gate, monkeypatch, caplog):
    from treg.infra import kv, org_concurrency as guard, org_flow_control as flow

    _, store = gate
    store.admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=2)
    clock = [100.0]
    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    flow.log.addHandler(caplog.handler)
    try:
        for _ in range(101):
            with pytest.raises(guard.AdmissionRejected):
                await guard.acquire(1, limit=None, version=7, rate_limit=flow.RateLimit(0.5, 2))
        events = [record for record in caplog.records if record.name == flow.log.name
                  and record.getMessage().startswith("org_flow_control_rate_limited ")]
        assert len(events) == 1
        assert json.loads(events[0].getMessage().split(" ", 1)[1])["suppressed_since_last_log"] == 0
        clock[0] += 5
        with pytest.raises(guard.AdmissionRejected):
            await guard.acquire(2, limit=3, version=8, rate_limit=flow.RateLimit(1, 3))
        events = [record for record in caplog.records if record.name == flow.log.name
                  and record.getMessage().startswith("org_flow_control_rate_limited ")]
        assert len(events) == 2
        latest = json.loads(events[-1].getMessage().split(" ", 1)[1])
        assert latest["org_id"] == 2 and latest["config_version"] == 8
        assert latest["suppressed_since_last_log"] == 100
    finally:
        flow.log.removeHandler(caplog.handler)


async def test_routed_children_count_one_root(clients, counter, enrichment_on, monkeypatch, caplog):
    from treg.application.call import service
    from test_routing import _relay_by_provider, ROUTED

    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "person@example.com", "score": 99,
                                  "verification": {"status": "valid"}}})]}, seen))
    response = await clients.post(f"/call/{ROUTED}", json={"full_name": "Test", "domain": "example.com"})
    assert response.status_code == 200, response.text
    assert seen
    [logged] = logs(caplog)
    assert logged["call_ref"] == response.headers["X-Treg-Call-Id"]
    assert logged["tool_name"] == logged["endpoint_id"] == ROUTED
    assert logged["method"] == "POST" and logged["replay_of"] is None
    [row] = counter.snapshot()["top"]
    assert row["requests"] == row["completed"] == 1


@pytest.mark.parametrize("path,tool,args", [
    ("/mcp/", "call", {"endpoint_id": "tikhub.tiktok.video.comments"}),
    ("/mcp/v2/", "catalog_call_read", {"endpoint_id": "tikhub.tiktok.video.comments"}),
    ("/mcp/v2/", "catalog_call_write", {"endpoint_id": "dataforseo.web.page.audit"}),
    ("/mcp/", "call_media", {"endpoint_id": "fishaudio.tts.s2-1-pro", "body": {"text": "test"}}),
])
async def test_mcp_logs_and_counts_one_root(
    clients, counter, caller_key_id, gate, caplog, path, tool, args,
):
    from test_mcp_directory import paired_mcp_session, _call_tool
    from treg.infra import org_concurrency as guard

    org, _ = await _configure_org(clients, gate)
    held = await guard.acquire(org, limit=1, version=1)
    try:
        async with paired_mcp_session() as client:
            out = await _call_tool(client, tool, args, clients.headers["X-Treg-Token"], path=path)
        assert out["status"] == 429
        [row] = logs(caplog)
        assert row["org_id"] == org and row["adapter"] in ("call", "call_media")
        assert row["api_key_id"] == caller_key_id
        assert row["call_ref"]
        [row] = counter.snapshot()["top"]
        assert row["requests"] == row["completed"] == 1
    finally:
        await held.finish()


async def test_mcp_preflight_has_null_identity(counter, caplog):
    out = await mcp._call_impl("example/path", ctx=SimpleNamespace(headers={}), catalog_only=False,
                               allowed_methods=None, surface=mcp._TEAM_SURFACE)
    assert out["error"] == "not authenticated"
    [row] = logs(caplog)
    assert row["org_id"] is None and row["api_key_id"] is None and row["call_ref"] is None
    assert all(row[key] is None for key in ("method", "tool_name", "endpoint_id", "replay_of"))
    assert counter.snapshot()["top"] == []


@pytest.mark.parametrize("path", ["/mcp/", "/mcp/v2/"])
async def test_mcp_eager_auth_challenge_logs_null(clients, counter, caplog, path):
    response = await clients.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    assert response.status_code == 401
    [row] = logs(caplog)
    assert row["org_id"] is None and row["status"] == 401
    assert row["api_key_id"] is None and row["call_ref"] is None
    assert row["method"] == "POST"
    assert row["tool_name"] is None and row["endpoint_id"] is None and row["replay_of"] is None
    assert counter.snapshot()["top"] == []


@pytest.mark.parametrize("path", ["/table/", "/catalog/call/"])
async def test_disabled_surface_logs_resolved_org_without_handler_arrival(
    clients, counter, caller_key_id, monkeypatch, caplog, path,
):
    monkeypatch.setattr(get_settings(), "table_enabled", False)
    monkeypatch.setattr(get_settings(), "claude_connector_enabled", False)
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    response = await clients.get(path + "example/path")
    assert response.status_code == 404
    [row] = logs(caplog)
    assert row["org_id"] == org and row["handler_ms"] is None
    assert row["api_key_id"] == caller_key_id
    assert row["call_ref"] == response.headers.get("X-Treg-Call-Id")
    assert row["method"] == "GET"
    assert row["tool_name"] is None and row["endpoint_id"] is None
    assert counter.snapshot()["top"] == []


async def test_keys_and_session_keep_distinct_attribution_with_one_org(
    clients, counter, caller_key_id, caplog,
):
    from treg.domain.identity import session

    org = (await clients.get("/orgs")).json()[0]["org_id"]
    second = (await clients.post(f"/orgs/{org}/api-keys", json={"name": "second"})).json()
    for token in (clients.headers["X-Treg-Token"], second["secret"]):
        assert (await clients.get("/call/example/path", headers={"X-Treg-Token": token})).status_code == 404
    claims = session.read_identity_claims(clients.headers["X-Treg-Token"])
    clients.cookies.set(session.COOKIE, session.make_session(claims["uid"], token_version=claims["tv"]))
    assert (await clients.get("/call/example/path", headers={
        "X-Treg-Token": "", "X-Treg-Org": str(org),
    })).status_code == 404
    rows = logs(caplog)
    assert [row["api_key_id"] for row in rows] == [caller_key_id, second["id"], None]
    assert {row["org_id"] for row in rows} == {org}
    assert len({row["call_ref"] for row in rows}) == 3
    assert all(row["call_ref"] for row in rows)
    assert second["secret"] not in json.dumps(rows)
    [summary] = counter.snapshot()["top"]
    assert summary["org_id"] == org and summary["requests"] == summary["completed"] == 3


@pytest.mark.parametrize("original_ref", ["a" * 32, ""])
async def test_replay_log_keeps_current_attempt_ref(clients, counter, monkeypatch, caplog, original_ref):
    from treg.application.call import service
    from treg.application.call.types import IdempotentReplay

    monkeypatch.setattr(service, "prepare_call_intake", AsyncMock(return_value=SimpleNamespace(
        idempotency_key="replayed", fingerprint="same-request", replay=IdempotentReplay(
            body=b"{}", status_code=200, media_type="application/json", charged_micro=0,
            call_ref=original_ref))))
    resolve = AsyncMock(side_effect=AssertionError("replay must not resolve a target"))
    monkeypatch.setattr(service, "resolve_call_target", resolve)
    response = await clients.get("/call/example/path", headers={"X-Treg-Replay-Of": "forged"})
    assert response.status_code == 200 and response.headers["X-Treg-Idempotent-Replay"] == "true"
    if original_ref:
        assert response.headers["X-Treg-Call-Id"] == original_ref
    [row] = logs(caplog)
    assert row["call_ref"] and row["call_ref"] != original_ref
    assert row["replay_of"] == (original_ref or None)
    assert row["tool_name"] is None and row["endpoint_id"] is None
    resolve.assert_not_awaited()
    assert counter.snapshot()["top"][0]["requests"] == 1


@pytest.mark.parametrize("surface,target", [
    ("/call/", "tikhub.tiktok.video.comments"),
    ("/table/", "tikhub.tiktok.video.comments"),
    ("/call/", "http://upstream/private-path"),
])
async def test_resolved_own_tool_does_not_guess_catalog_identity(
    clients, counter, monkeypatch, caplog, surface, target,
):
    name = "tikhub.tiktok.video.comments"
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    registered = await clients.post("/tools", json={"name": name, "base_url": "http://upstream"})
    assert registered.status_code == 200, registered.text
    response = await clients.get(surface + target + "?secret=must-not-log")
    assert response.status_code == 200, response.text
    [row] = logs(caplog)
    assert row["tool_name"] == name and row["endpoint_id"] is None
    assert row["method"] == "GET" and row["replay_of"] is None
    assert row["provider"] is None and row["credential_tier"] is None and row["refused_by"] is None
    assert "must-not-log" not in json.dumps(row) and "private-path" not in json.dumps(row)
    assert row["failure_kind"] is None


@pytest.mark.parametrize("surface", ["/call/", "/catalog/call/", "/table/"])
@pytest.mark.parametrize("valid", [True, False])
async def test_catalog_identity_survives_success_and_parameter_refusal(
    clients, counter, platform_on, monkeypatch, caplog, surface, valid,
):
    from treg.application.call import service
    from test_routing import _relay_by_provider

    endpoint = "tikhub.tiktok.video.comments"
    monkeypatch.setattr(get_settings(), "table_enabled", True)
    monkeypatch.setattr(get_settings(), "claude_connector_enabled", True)
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"tikhub": [(200, {"comments": []})]}, seen))
    response = await clients.get(surface + endpoint, params={"aweme_id": "synthetic"} if valid else {})
    assert response.status_code == (200 if valid else 400), response.text
    [row] = logs(caplog)
    assert row["endpoint_id"] == endpoint and row["tool_name"]
    assert row["method"] == "GET" and row["replay_of"] is None
    assert row["provider"] == "tikhub"
    assert row["credential_tier"] == ("platform" if valid else None)
    assert row["refused_by"] == (None if valid else "request")
    assert bool(seen) == valid


async def test_unknown_http_method_is_not_logged_as_request_content(caplog):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 405})

    await traffic.RequestLogMiddleware(app)(
        {"type": "http", "path": "/call/example", "method": "PRIVATE-METHOD"},
        AsyncMock(), AsyncMock())
    [row] = logs(caplog)
    assert row["method"] is None and "PRIVATE-METHOD" not in json.dumps(row)


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
        await guard.acquire(1, limit=1, version=1)
    assert error.value.status_code == 429


async def test_rate_logging_failure_preserves_rejection(gate, monkeypatch):
    from treg.infra import kv, org_concurrency as guard, org_flow_control as flow

    gate[1].admit_result = kv.OrgAdmissionResult("rate_limited", retry_after_s=3)
    monkeypatch.setattr(flow.log, "log", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(guard.AdmissionRejected) as error:
        await guard.acquire(1, limit=None, version=7, rate_limit=flow.RateLimit(0.5, 1))
    assert error.value.status_code == 429 and error.value.detail["error"] == "org_rate_limit"
    assert error.value.retry_after_s == 3 and error.value.detail["config_version"] == 7
