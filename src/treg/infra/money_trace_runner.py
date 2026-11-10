"""Process-owned money diagnostics, independent of the optional analytics transport.

The sampler only reads local state. A single daemon thread owns bounded log output, so a
blocked logging handler cannot hold the event loop, a database connection or worker shutdown.
Scheduler lag measures a late event-loop wakeup; it is never labelled as a database lock wait.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from . import kv, money_trace


_SAMPLE_S = 1.0
_EMIT_S = 60.0
_LOG_QUEUE_SIZE = 128
_LOGS_PER_WINDOW = 600
_SHUTDOWN_S = 0.25
_CORE_COUNTERS = ("dropped_active_total", "dropped_events_total", "diagnostic_errors_total",
                  "tracked_transactions_total", "ended_transactions_total")
_sink_slot = threading.BoundedSemaphore(1)
_sink_pid = os.getpid()


def _log_event(event: dict) -> None:
    logging.getLogger("treg.money_trace").warning(
        "money_trace %s", json.dumps(event, separators=(",", ":"), sort_keys=True))


def _capture_gauge(properties: dict) -> None:
    from .. import analytics
    analytics.capture(analytics.SERVER_DISTINCT_ID, "money_trace_gauge", properties)


def _capture_lease_gauge(properties: dict) -> None:
    from .. import analytics
    analytics.capture(analytics.SERVER_DISTINCT_ID, "kv_lease_gauge", properties)


class _LogSink:
    """One bounded queue and thread. No logging or thread join runs on the asyncio loop."""

    def __init__(self, emit: Callable[[dict], None], *, capacity: int = _LOG_QUEUE_SIZE):
        self._emit = emit
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=capacity)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._written = self._errors = self._dropped = self._shutdown_dropped = 0
        self._inflight = False
        self._last_written: float | None = None
        self._thread = threading.Thread(target=self._run, name="treg-money-trace-log", daemon=True)
        self._started = False

    def start(self) -> None:
        global _sink_slot, _sink_pid
        if not self._started:
            if _sink_pid != os.getpid():
                _sink_slot, _sink_pid = threading.BoundedSemaphore(1), os.getpid()
            if not _sink_slot.acquire(blocking=False):
                return
            try:
                self._thread.start()
                self._started = True
            except Exception:
                _sink_slot.release()
                raise

    def submit(self, event: dict) -> None:
        if self._closed.is_set() or not self._started:
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1

    def _run(self) -> None:
        try:
            self._consume()
        finally:
            _sink_slot.release()

    def _consume(self) -> None:
        while not self._closed.is_set() or not self._queue.empty():
            try:
                event = self._queue.get(timeout=0.02)
            except queue.Empty:
                continue
            with self._lock:
                self._inflight = True
            try:
                if event.get("event") == "money_trace_gauge":
                    # Include losses from preceding records, even if they failed after this
                    # summary was queued. This remains local logging-thread work, never SQL.
                    event = {**event, **self.stats()}
                self._emit(event)
            except Exception:  # noqa: BLE001 - never recursively log a sink failure
                with self._lock:
                    self._errors += 1
            else:
                with self._lock:
                    self._written += 1
                    self._last_written = time.monotonic()
            finally:
                with self._lock:
                    self._inflight = False
                self._queue.task_done()

    def close(self) -> None:
        self._closed.set()

    @property
    def alive(self) -> bool:
        return self._started and self._thread.is_alive()

    def discard_pending(self) -> None:
        """A stuck handler may finish its one current record; queued records cannot delay exit."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._shutdown_dropped += 1
            self._queue.task_done()

    def stats(self) -> dict:
        with self._lock:
            return {
                "log_written_total": self._written,
                "log_sink_errors_total": self._errors,
                "log_queue_dropped_total": self._dropped,
                "log_shutdown_dropped_total": self._shutdown_dropped,
                "log_queued": self._queue.qsize(),
                "log_inflight": int(self._inflight),
                "log_sink_available": self.alive,
                "log_last_success_age_ms": (
                    None if self._last_written is None
                    else round(max(0.0, time.monotonic() - self._last_written) * 1000, 3)),
            }


class MoneyTraceRunner:
    """One lifecycle owner; failures here do not replace the command/request result."""

    def __init__(self, *, role: str, sample_s: float = _SAMPLE_S, emit_s: float = _EMIT_S,
                 log_event: Callable[[dict], None] = _log_event,
                 capture_gauge: Callable[[dict], None] = _capture_gauge,
                 capture_lease_gauge: Callable[[dict], None] = _capture_lease_gauge,
                 log_capacity: int = _LOG_QUEUE_SIZE, logs_per_window: int = _LOGS_PER_WINDOW,
                 shutdown_s: float = _SHUTDOWN_S):
        self.role = role if role in {"all", "control", "dataplane", "worker"} else "unknown"
        self.sample_s = sample_s
        self.emit_s = emit_s
        self.shutdown_s = shutdown_s
        self.logs_per_window = logs_per_window
        self._sink = _LogSink(log_event, capacity=log_capacity)
        self._capture_gauge = capture_gauge
        self._capture_lease_gauge = capture_lease_gauge
        self._task: asyncio.Task | None = None
        self._closed = False
        self._started = self._opened = self._last_sample = 0.0
        self._samples = self._diagnostic_errors = self._rate_dropped = self._window_logs = 0
        self._loop_lag_max_ms = self._loop_lag_total_ms = 0.0
        self._build = "unknown"

    def start(self) -> MoneyTraceRunner:
        if self._task is not None or self._closed:
            return self
        try:
            from .. import analytics
            self._build = analytics.build_id()
            self._sink.start()
            self._started = self._opened = time.monotonic()
            self._task = asyncio.create_task(self._run(), name="treg-money-trace-sample")
        except Exception:  # noqa: BLE001 - an unavailable diagnostic cannot stop startup
            self._diagnostic_errors += 1
            self._sink.close()
        return self

    def _decorate(self, event: dict) -> dict:
        return {**event, "schema_version": 1, "build": self._build, "role": self.role,
                "process_instance": money_trace.PROCESS_INSTANCE,
                "observed_at": datetime.now(timezone.utc).isoformat()}

    def _submit(self, events: list[dict]) -> None:
        for event in events:
            if self._window_logs >= self.logs_per_window:
                self._rate_dropped += 1
                continue
            self._window_logs += 1
            self._sink.submit(self._decorate(event))

    def _sample(self, *, now: float, planned: float) -> None:
        # No catch-up burst: one sample describes the actual wakeup, however late it was.
        lag_ms = max(0.0, now - planned) * 1000
        self._loop_lag_max_ms = max(self._loop_lag_max_ms, lag_ms)
        self._loop_lag_total_ms += lag_ms
        self._samples += 1
        try:
            money_trace.sample()
            self._last_sample = now
            self._submit(money_trace.drain_events())
        except Exception:  # noqa: BLE001
            self._diagnostic_errors += 1

    def _properties(self, now: float) -> dict:
        try:
            core = money_trace.stats()
        except Exception:  # noqa: BLE001
            core = {}
            self._diagnostic_errors += 1
        # Explicit allowlist: org/call/transaction ids and SQL descriptors stay in logs only.
        result = {
            "process_instance": money_trace.PROCESS_INSTANCE,
            "role": self.role,
            "samples": self._samples,
            "window_s": round(max(0.0, now - self._opened), 3),
            "loop_lag_max_ms": round(self._loop_lag_max_ms, 3),
            "loop_lag_mean_ms": round(self._loop_lag_total_ms / max(1, self._samples), 3),
            "sample_age_ms": (round(max(0.0, now - self._last_sample) * 1000, 3)
                              if self._last_sample else None),
            "active": core.get("active", 0),
            "queued": core.get("queued", 0),
            "enabled": core.get("enabled", False),
            "runner_errors_total": self._diagnostic_errors,
            "log_rate_dropped_total": self._rate_dropped,
            **{f"trace_{name}": core.get(name, 0) for name in _CORE_COUNTERS},
            **self._sink.stats(),
        }
        return result

    def _emit(self, *, now: float, shutdown: bool = False, local: bool = True) -> None:
        if local:
            try:
                self._submit(kv.drain_lease_errors())
            except Exception:  # noqa: BLE001 - preserve the gauge even if a detail drain fails
                self._diagnostic_errors += 1
            try:
                lease_rows = kv.drain_lease_timings()
            except Exception:  # noqa: BLE001
                lease_rows = []
                self._diagnostic_errors += 1
            for row in lease_rows:
                try:
                    event = self._decorate({**row, "window_s": round(max(0.0, now - self._opened), 3),
                                            "shutdown": shutdown})
                    self._submit([event])
                    self._capture_lease_gauge({key: value for key, value in event.items() if key != "event"})
                except Exception:  # noqa: BLE001 - one failed summary must not suppress the rest
                    self._diagnostic_errors += 1
        try:
            props = self._properties(now)
            props["shutdown"] = shutdown
            if local:
                # A reserved summary is outside the detail rate limit. All loss counters remain
                # visible even when the detail limit suppressed the core's own dropped event.
                self._sink.submit(self._decorate({"event": "money_trace_gauge", **props}))
            self._capture_gauge(props)
        except Exception:  # noqa: BLE001
            self._diagnostic_errors += 1
        self._samples = self._window_logs = 0
        self._loop_lag_max_ms = self._loop_lag_total_ms = 0.0
        self._opened = now

    async def _run(self) -> None:
        planned = time.monotonic()
        while True:
            now = time.monotonic()
            self._sample(now=now, planned=planned)
            if now - self._opened >= self.emit_s:
                self._emit(now=now)
            planned = time.monotonic() + self.sample_s
            await asyncio.sleep(self.sample_s)

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
        try:
            if self._task is not None:
                await asyncio.gather(self._task, return_exceptions=True)
            try:
                self._submit(money_trace.shutdown_events())
            except Exception:  # noqa: BLE001
                self._diagnostic_errors += 1
            self._emit(now=time.monotonic(), shutdown=True)
            self._sink.close()
            deadline = time.monotonic() + self.shutdown_s
            while self._sink.alive and time.monotonic() < deadline:
                await asyncio.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        finally:
            # A second cancellation also reaches this path. A logger blocked in user code owns
            # at most one daemon thread and one current record; it cannot hold process exit open.
            self._sink.close()
            self._sink.discard_pending()
            self._emit(now=time.monotonic(), shutdown=True, local=False)


@asynccontextmanager
async def money_trace_lifespan(*, role: str):
    runner = start_money_trace(role=role)
    try:
        yield runner
    finally:
        await stop_money_trace(runner)


def start_money_trace(*, role: str) -> MoneyTraceRunner | None:
    try:
        return MoneyTraceRunner(role=role).start()
    except Exception:  # noqa: BLE001 - even an unavailable thread/queue cannot stop the service
        return None


async def stop_money_trace(runner: MoneyTraceRunner | None) -> None:
    if runner is not None:
        try:
            await runner.stop()
        except Exception:  # noqa: BLE001 - preserve a command's original exception/result
            pass
