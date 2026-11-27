"""Local diagnostics survive telemetry/sink failures without owning business execution."""

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from treg import analytics, bootstrap, worker
from treg.infra import money_trace_runner as runner_module


@pytest.fixture
def trace(monkeypatch):
    pending = []
    calls = []
    monkeypatch.setattr(runner_module.kv, "_lease_errors", {})
    monkeypatch.setattr(runner_module.kv, "_lease_timings", {})

    def drain():
        result, pending[:] = list(pending), []
        return result

    def shutdown():
        calls.append("shutdown")
        pending.append({"event": "money_txn_end", "process_instance": "trace-process",
                        "org_id": 7, "call_id": "test-call", "transaction_id": "tx-1"})
        return drain()

    monkeypatch.setattr(runner_module.money_trace, "sample", lambda: calls.append("sample"))
    monkeypatch.setattr(runner_module.money_trace, "drain_events", drain)
    monkeypatch.setattr(runner_module.money_trace, "shutdown_events", shutdown)
    monkeypatch.setattr(runner_module.money_trace, "PROCESS_INSTANCE", "trace-process")
    monkeypatch.setattr(runner_module.money_trace, "stats", lambda: {
        "enabled": True, "active": 2, "queued": len(pending),
        "dropped_active_total": 3, "dropped_events_total": 5, "diagnostic_errors_total": 1,
        "tracked_transactions_total": 8, "ended_transactions_total": 6,
        "org_id": 999, "call_id": "must-not-reach-analytics", "statement": "private",
    })
    monkeypatch.setattr(analytics, "build_id", lambda: "test-build")
    return SimpleNamespace(pending=pending, calls=calls)


async def _until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("diagnostic did not reach the expected state")


async def test_local_records_and_shutdown_work_without_posthog(trace, monkeypatch):
    monkeypatch.setattr(analytics, "enabled", lambda: False)
    logs, gauges = [], []
    trace.pending.append({"event": "money_txn_slow", "process_instance": "trace-process",
                          "org_id": 7, "call_id": "test-call"})
    runner = runner_module.MoneyTraceRunner(
        role="worker", sample_s=0.005, emit_s=0.015,
        log_event=logs.append, capture_gauge=gauges.append).start()
    assert runner.start() is runner
    await _until(lambda: gauges)
    await runner.stop()
    await runner.stop()
    assert runner._task.done()
    assert not runner._sink.alive
    assert trace.calls.count("shutdown") == 1
    assert {event["event"] for event in logs} >= {
        "money_txn_slow", "money_txn_end", "money_trace_gauge"}
    assert all(event["build"] == "test-build" and event["schema_version"] == 1 for event in logs)
    assert all(event["process_instance"] == "trace-process" for event in logs)
    assert any(event.get("call_id") == "test-call" for event in logs)
    for properties in gauges:
        assert not {"org_id", "call_id", "transaction_id", "statement"} & properties.keys()
        assert properties["trace_dropped_events_total"] == 5
        assert properties["trace_tracked_transactions_total"] == 8
        assert properties["trace_ended_transactions_total"] == 6
    assert not [task for task in asyncio.all_tasks()
                if task.get_name() == "treg-money-trace-sample" and not task.done()]


async def test_sink_failure_does_not_change_worker_return_or_error(trace, monkeypatch):
    original = runner_module.MoneyTraceRunner
    runners, gauges = [], []

    def broken_sink(event):
        raise OSError("logging destination unavailable")

    def factory(**kwargs):
        runner = original(**kwargs, log_event=broken_sink, capture_gauge=gauges.append)
        runners.append(runner)
        return runner

    monkeypatch.setattr(runner_module, "MoneyTraceRunner", factory)

    async def success(args):
        return 23

    assert await worker._run_command(SimpleNamespace(fn=success)) == 23
    failure = ValueError("business failure")

    async def fail(args):
        raise failure

    with pytest.raises(ValueError) as caught:
        await worker._run_command(SimpleNamespace(fn=fail))
    assert caught.value is failure
    assert all(not runner._sink.alive and runner._task.done() for runner in runners)
    assert any(props["log_sink_errors_total"] > 0 for props in gauges)


async def test_worker_cancellation_is_preserved_and_sampler_is_stopped(trace, monkeypatch):
    original = runner_module.MoneyTraceRunner
    runners = []

    def factory(**kwargs):
        runner = original(**kwargs, log_event=lambda event: None, capture_gauge=lambda props: None)
        runners.append(runner)
        return runner

    monkeypatch.setattr(runner_module, "MoneyTraceRunner", factory)
    entered = asyncio.Event()

    async def command(args):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(worker._run_command(SimpleNamespace(fn=command)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert trace.calls.count("shutdown") == 1
    assert runners[0]._task.done() and not runners[0]._sink.alive


async def test_blocked_log_handler_has_bounded_queue_and_cannot_block_shutdown(trace):
    release, entered = threading.Event(), threading.Event()
    gauges = []

    def blocked_sink(event):
        entered.set()
        release.wait(2)

    first = runner_module.MoneyTraceRunner(
        role="all", log_event=blocked_sink, capture_gauge=gauges.append,
        log_capacity=2, shutdown_s=0.01).start()
    try:
        first._submit([{"event": "money_txn_slow"}])
        await _until(entered.is_set)
        first._submit([{"event": "money_txn_slow"}] * 8)
        start = time.monotonic()
        await first.stop()
        assert time.monotonic() - start < 0.2
        assert first._task.done()
        assert first._sink.stats()["log_queue_dropped_total"] > 0
        assert first._sink.stats()["log_shutdown_dropped_total"] == 2
        assert gauges[-1]["log_inflight"] == 1
        # Repeated lifecycles cannot accumulate blocked logger threads in one process.
        second = runner_module.MoneyTraceRunner(
            role="worker", capture_gauge=lambda props: None).start()
        await second.stop()
        assert second._sink.stats()["log_sink_available"] is False
        assert second._sink.stats()["log_queue_dropped_total"] > 0
    finally:
        release.set()
        await first.stop()
        await _until(lambda: not first._sink.alive)


async def test_second_cancellation_during_log_drain_leaves_no_sampler_task(trace, monkeypatch):
    release, logging_started = threading.Event(), threading.Event()
    original = runner_module.MoneyTraceRunner
    runners = []

    def sink(event):
        logging_started.set()
        release.wait(2)

    def factory(**kwargs):
        runner = original(**kwargs, log_event=sink, capture_gauge=lambda props: None)
        runners.append(runner)
        return runner

    monkeypatch.setattr(runner_module, "MoneyTraceRunner", factory)
    trace.pending.append({"event": "money_txn_slow"})

    async def command(args):
        await asyncio.Event().wait()

    task = asyncio.create_task(worker._run_command(SimpleNamespace(fn=command)))
    try:
        await _until(logging_started.is_set)
        task.cancel()
        await _until(lambda: runners[0]._closed)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runners[0]._task.done()
        assert runners[0]._sink._closed.is_set()
        assert runners[0]._sink.stats()["log_queued"] == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await _until(lambda: not runners[0]._sink.alive)


async def test_diagnostic_startup_failure_does_not_prevent_worker_business(trace, monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError("cannot create diagnostic resources")

    monkeypatch.setattr(runner_module, "MoneyTraceRunner", unavailable)

    async def command(args):
        return 9

    assert await worker._run_command(SimpleNamespace(fn=command)) == 9


async def test_each_web_role_owns_diagnostics_without_analytics(trace, monkeypatch):
    role = "all"
    original = runner_module.MoneyTraceRunner
    runners, logs, lifecycle = [], [], []

    def factory(**kwargs):
        runner = original(**kwargs, log_event=logs.append, capture_gauge=lambda props: None)
        runners.append(runner)
        return runner

    async def nothing(*args, **kwargs):
        return None

    @asynccontextmanager
    async def store(*args):
        yield None

    async def analytics_drain():
        assert runners[0]._task.done()
        assert not runners[0]._sink.alive
        lifecycle.append("analytics_drained")

    monkeypatch.setattr(runner_module, "MoneyTraceRunner", factory)
    monkeypatch.setattr(analytics, "enabled", lambda: False)
    monkeypatch.setattr(bootstrap, "archive_object_store", store)
    monkeypatch.setattr(bootstrap, "verify_db", nothing)
    monkeypatch.setattr(bootstrap, "_mcp", None)
    monkeypatch.setattr(bootstrap.kv, "configured", lambda: False)
    monkeypatch.setattr(bootstrap.kv, "close", nothing)
    monkeypatch.setattr(bootstrap.adsconv, "enabled", lambda: False)
    monkeypatch.setattr(bootstrap.archive, "worker_enabled", lambda: False)
    monkeypatch.setattr(bootstrap.archive, "prune_enabled", lambda: False)
    monkeypatch.setattr(bootstrap.find_index, "enabled", lambda: False)
    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", lambda **kw: SimpleNamespace(aclose=nothing))
    monkeypatch.setattr(bootstrap.arena, "shutdown", nothing)
    monkeypatch.setattr(bootstrap.audit, "drain", nothing)
    monkeypatch.setattr(bootstrap.archive, "drain", nothing)
    monkeypatch.setattr(analytics, "drain", analytics_drain)
    app = SimpleNamespace(state=SimpleNamespace(endpoint_observation_reader=SimpleNamespace(aclose=nothing)))
    async with bootstrap._lifespan(role)(app):
        await _until(lambda: "sample" in trace.calls)
        assert runners[0].role == role
    assert lifecycle == ["analytics_drained"]
    assert any(event["event"] == "money_trace_gauge" and event["shutdown"] for event in logs)
