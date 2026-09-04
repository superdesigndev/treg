"""analytics.py — bounded, batched, droppable, and OFF by default.

The module's whole contract is negative space: with no posthog_key it must do nothing, and
with one it must never raise, never block, and never grow without bound. Each test pins one
of those guarantees; the event payload shape (distinct_id / $groups / $lib) is pinned once.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.requests import Request
from uvicorn.protocols.http.h11_impl import RequestResponseCycle

from treg import analytics
from treg.application.call.types import CallFailure
from treg.bootstrap_handlers import _pool_saturated
from treg.config import get_settings
from treg.routers.call import _translate_call_failure


@pytest.fixture(autouse=True)
async def _clean():
    while analytics._installed_fault_handler is not None:
        analytics.remove_fault_handler(analytics._installed_fault_handler)
    analytics._queue.clear()
    analytics._flusher = None
    analytics._fault_windows.clear()
    yield
    analytics._fault_windows.clear()
    # Enabled tests inspect queued payloads but must never send them to a real PostHog host.
    analytics._queue.clear()
    await analytics.drain()
    while analytics._installed_fault_handler is not None:
        analytics.remove_fault_handler(analytics._installed_fault_handler)
    analytics._queue.clear()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test_suite", raising=False)


@pytest.fixture
def posts(monkeypatch):
    """Record batches instead of talking to PostHog."""
    sent: list[list[dict]] = []

    async def _fake_post(batch):
        sent.append(batch)

    monkeypatch.setattr(analytics, "_post", _fake_post)
    return sent


async def test_disabled_is_a_noop():
    # default settings: no key → capture must not even queue (self-hosters send nothing)
    analytics.capture("a@b.c", "tool_called", {"x": 1})
    assert analytics._queue == []
    assert analytics._flusher is None


async def test_batch_shape_and_drain(enabled, posts):
    analytics.capture("a@b.c", "tool_called", {"provider": "tikhub"}, groups={"team": "acme"})
    analytics.capture("a@b.c", "tool_called", {"provider": "dataforseo"})
    await analytics.drain()
    assert len(posts) == 1
    batch = posts[0]
    assert [e["event"] for e in batch] == ["tool_called", "tool_called"]
    first = batch[0]
    assert first["distinct_id"] == "a@b.c"
    assert first["properties"]["provider"] == "tikhub"
    assert first["properties"]["$groups"] == {"team": "acme"}
    assert first["properties"]["$lib"] == "treg-server"
    assert "$groups" not in batch[1]["properties"]  # no groups passed → no key at all


async def test_queue_is_bounded(enabled, monkeypatch):
    monkeypatch.setattr(analytics, "_MAX_PENDING", 10)
    for i in range(25):
        analytics.capture("a@b.c", "e", {"i": i})
    assert len(analytics._queue) == 10  # newest dropped past the bound, no growth


async def test_oversized_drain_splits_batches(enabled, posts, monkeypatch):
    monkeypatch.setattr(analytics, "_BATCH_MAX", 100)
    for i in range(150):
        analytics.capture("a@b.c", "e", {"i": i})
    await analytics.drain()
    assert [len(b) for b in posts] == [100, 50]


async def test_post_failure_is_swallowed_and_capture_never_raises(enabled, monkeypatch):
    async def _boom(batch):
        raise RuntimeError("posthog is down")

    monkeypatch.setattr(analytics, "_post", _boom)
    analytics.capture("a@b.c", "e")
    await analytics.drain()  # must return, not raise
    assert analytics._queue == []  # the batch was dropped, not retried

    # capture() itself swallows internal failures — the never-raise contract call sites
    # (the Stripe webhook above all) depend on
    def _explode():
        raise RuntimeError("scheduling broke")

    monkeypatch.setattr(analytics, "_ensure_flusher", _explode)
    analytics.capture("a@b.c", "e")  # must not raise


def _exception_events() -> list[dict]:
    return [event for event in analytics._queue if event["event"] == "$exception"]


def test_fault_payload_is_secret_minimal_and_truncated(enabled):
    analytics.capture_fault(RuntimeError("x" * 700), component="scheduler")

    event = _exception_events()[0]
    assert event["distinct_id"] == "treg-server"
    assert event["properties"] == {
        "$exception_list": [{
            "type": "RuntimeError",
            "value": "x" * 500,
            "mechanism": {"handled": False},
        }],
        "component": "scheduler",
        "fault_type": "RuntimeError",
        "fault_occurrences": 1,
        "$lib": "treg-server",
    }
    assert "frames" not in str(event).lower()
    assert "traceback" not in str(event).lower()


def test_fault_value_redacts_query_credentials_before_capture(enabled):
    analytics.capture_fault(RuntimeError(
        "request failed for https://api.example.com/x?api_key=sk-SECRET&y=1"),
        component="relay")

    value = _exception_events()[0]["properties"]["$exception_list"][0]["value"]
    assert value == "request failed for https://api.example.com/x?[redacted]"
    assert "sk-SECRET" not in value
    assert "api_key=" not in value


def _occurrences() -> int:
    """What a `sum(fault_occurrences)` query in PostHog would return."""
    return sum(e["properties"]["fault_occurrences"] for e in _exception_events())


def test_a_storm_costs_one_event_per_window_but_reports_every_occurrence(enabled, monkeypatch):
    """The point of the rollup: cost is bounded, the COUNT is not approximated. The old token
    bucket emitted ~10/minute and pinned the dashboard at its own refill rate, so the graph of a
    two-hour incident was a flat line at the limiter."""
    now = [100.0]
    monkeypatch.setattr(analytics.time, "monotonic", lambda: now[0])

    for _ in range(100):
        analytics.capture_fault(PoolTimeoutError("pool full"), component="db_pool")
    assert len(_exception_events()) == 1  # one event stands for the window

    now[0] += analytics._FAULT_WINDOW_S
    analytics._emit_fault_summaries()
    assert len(_exception_events()) == 2
    assert _occurrences() == 100  # ...and nothing was approximated away


def test_a_loud_fault_cannot_silence_an_unrelated_one(enabled, monkeypatch):
    """The bug the global bucket had: one key exhausting a shared budget dropped every OTHER key,
    including a first sighting — the highest-information event there is, and the one least likely
    to recur and carry its own count out later."""
    now = [100.0]
    monkeypatch.setattr(analytics.time, "monotonic", lambda: now[0])

    for _ in range(500):
        analytics.capture_fault(PoolTimeoutError("pool full"), component="db_pool")
    analytics.capture_fault(RuntimeError("something new"), component="scheduler")

    assert [e["properties"]["fault_type"] for e in _exception_events()] == [
        "TimeoutError", "RuntimeError"]


def test_the_key_ledger_is_bounded(enabled, monkeypatch):
    """Cardinality is what caps the cost now that volume does not, so it must be finite."""
    monkeypatch.setattr(analytics, "_FAULT_MAX_KEYS", 5)
    for i in range(50):
        analytics.capture_fault(RuntimeError("x"), component=f"site-{i}")
    assert len(analytics._fault_windows) == 5


def test_eviction_never_discards_a_count_that_was_never_reported(enabled, monkeypatch):
    """Bounding the ledger must not reintroduce the loss this rewrite exists to remove: a window
    holding occurrences nobody has seen is not eligible for eviction, even over the cap."""
    monkeypatch.setattr(analytics, "_FAULT_MAX_KEYS", 3)
    for i in range(3):
        for _ in range(4):  # first reports, next three land in `pending`
            analytics.capture_fault(RuntimeError("x"), component=f"loud-{i}")
    for i in range(20):
        analytics.capture_fault(RuntimeError("x"), component=f"quiet-{i}")

    holding = {k: w.pending for k, w in analytics._fault_windows.items() if w.pending}
    assert sorted(k[1] for k in holding) == ["loud-0", "loud-1", "loud-2"]
    assert set(holding.values()) == {3}


def test_a_full_ledger_still_rolls_up_a_newly_seen_key(enabled, monkeypatch):
    """A new window is the only one with an empty count when every other key is holding one, so an
    eviction scan looking for `not pending` picks the key it just created. That key then never
    stays in the ledger: each occurrence opens a window, loses it, and reports on its own. The
    rollup would be off exactly during a broad storm, which is the case the cap exists for."""
    monkeypatch.setattr(analytics, "_FAULT_MAX_KEYS", 3)
    for i in range(3):
        for _ in range(2):
            analytics.capture_fault(RuntimeError("x"), component=f"loud-{i}")
    assert all(w.pending for w in analytics._fault_windows.values())  # ledger full, all holding

    analytics._queue.clear()
    for _ in range(50):
        analytics.capture_fault(RuntimeError("newcomer"), component="new-site")

    assert len(_exception_events()) == 1     # one report, 49 rolled up — not 50 reports
    assert _occurrences() == 1
    assert ("RuntimeError", "new-site") in analytics._fault_windows


def test_a_summary_shares_the_fingerprint_of_the_event_it_summarises(enabled, monkeypatch):
    """PostHog fingerprints on type + value. A summary carrying a LATER occurrence's message lands
    in a different issue from its own first report, so `sum(fault_occurrences)` splits and anyone
    filtering by issue — the normal way to read Error Tracking — gets a number that is quietly low.
    Constant messages hide this; any `str(exc)` carrying an id, path or URL does not."""
    now = [100.0]
    monkeypatch.setattr(analytics.time, "monotonic", lambda: now[0])

    for message in ("first for /a", "second for /b", "third for /c"):
        analytics.capture_fault(RuntimeError(message), component="relay")
    now[0] += analytics._FAULT_WINDOW_S
    analytics._emit_fault_summaries()

    values = [e["properties"]["$exception_list"][0]["value"] for e in _exception_events()]
    assert values == ["first for /a", "first for /a"]
    assert _occurrences() == 3


async def test_a_shutdown_does_not_take_the_counts_with_it(enabled, posts, monkeypatch):
    """A storm that stops, or a process that restarts mid-incident, used to lose its accumulated
    count: the old code released it only on the back of the NEXT event for that key. Restarting is
    what you do during an incident, so the counts died exactly when they were worth the most."""
    now = [100.0]
    monkeypatch.setattr(analytics.time, "monotonic", lambda: now[0])

    for _ in range(30):
        analytics.capture_fault(PoolTimeoutError("pool full"), component="db_pool")
    analytics._queue.clear()  # the window's one reported event is already on its way

    await analytics.drain()  # sweeps unconditionally: after this there is no later
    sent = [e for batch in posts for e in batch if e["event"] == "$exception"]
    assert [e["properties"]["fault_occurrences"] for e in sent] == [29]


def test_faults_yield_to_analytics_only_when_the_queue_is_actually_backing_up(enabled, monkeypatch):
    """The old rate limit shed at full speed against an empty queue — it fired on elapsed time,
    while the resource it existed to protect was at ~1% of its bound."""
    monkeypatch.setattr(analytics, "_MAX_PENDING", 10)
    analytics.capture_fault(RuntimeError("first"), component="quiet")
    assert len(_exception_events()) == 1  # queue empty: no reason to drop anything

    for i in range(9):
        analytics.capture("a@b.c", "tool_called", {"i": i})
    analytics.capture_fault(RuntimeError("second"), component="congested")
    assert len(_exception_events()) == 1  # past the share: counted, not queued


def test_handler_recursion_guard_swallows_capture_failure(enabled, monkeypatch):
    calls = 0

    def broken_capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        logging.getLogger("capture.failure").error("capture failed too")
        raise RuntimeError("broken fault capture")

    handler = analytics.install_fault_handler()
    monkeypatch.setattr(analytics, "capture", broken_capture)
    logging.getLogger("background.worker").error("original failure")
    assert calls == 1
    analytics.remove_fault_handler(handler)


def test_handler_ignores_analytics_logger_and_children(enabled):
    handler = analytics.install_fault_handler()
    logging.getLogger("treg.analytics").error("self failure")
    logging.getLogger("treg.analytics.sender").error("child failure")
    assert _exception_events() == []
    analytics.remove_fault_handler(handler)


def test_bare_error_log_does_not_forward_formatted_arguments(enabled):
    handler = analytics.install_fault_handler()
    logging.getLogger("background.worker").error("request body was %s", "secret-value")
    analytics.remove_fault_handler(handler)

    event = _exception_events()[0]
    assert event["properties"]["fault_type"] == "background.worker"
    assert event["properties"]["$exception_list"][0]["value"] == "background.worker"
    assert "secret-value" not in str(event)


def test_handler_is_installed_only_while_enabled(enabled):
    root = logging.getLogger()
    uvicorn_error = logging.getLogger("uvicorn.error")
    handler = analytics.install_fault_handler()
    assert handler in root.handlers
    assert handler in uvicorn_error.handlers

    analytics.remove_fault_handler(handler)
    assert handler not in root.handlers
    assert handler not in uvicorn_error.handlers


def test_disabled_handler_install_does_not_touch_logging():
    root_handlers = list(logging.getLogger().handlers)
    uvicorn_handlers = list(logging.getLogger("uvicorn.error").handlers)
    assert analytics.install_fault_handler() is None
    assert logging.getLogger().handlers == root_handlers
    assert logging.getLogger("uvicorn.error").handlers == uvicorn_handlers


class _Transport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Cycle:
    """Small protocol shell: exercise Uvicorn's real run_asgi logging without a socket."""

    def __init__(self, scope: dict):
        self.logger = logging.getLogger("uvicorn.error")
        self.scope = scope
        self.transport = _Transport()
        self.response_started = False
        self.response_complete = False
        self.disconnected = False
        self.on_response = lambda: None

    async def receive(self):
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.response_started = True
        elif message["type"] == "http.response.body" and not message.get("more_body", False):
            self.response_complete = True

    async def send_500_response(self):
        self.response_started = True
        self.response_complete = True


def _scope(path: str, query: bytes = b"") -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query,
        "root_path": "", "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1234), "server": ("test", 80), "state": {},
    }


async def _run_through_uvicorn(app: FastAPI, path: str, query: bytes = b"") -> _Cycle:
    cycle = _Cycle(_scope(path, query))
    await RequestResponseCycle.run_asgi(cycle, app)
    return cycle


async def test_unhandled_route_exception_reaches_handler_via_uvicorn(enabled):
    app = FastAPI()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("route exploded")

    handler = analytics.install_fault_handler()
    cycle = await _run_through_uvicorn(app, "/boom")
    analytics.remove_fault_handler(handler)

    assert cycle.transport.closed
    event = _exception_events()[0]
    assert event["properties"]["component"] == "asgi"
    assert event["properties"]["logger"] == "uvicorn.error"
    assert event["properties"]["fault_type"] == "RuntimeError"


async def test_typed_refusals_auth_and_validation_are_not_faults(enabled):
    app = FastAPI()

    @app.get("/call-refusal")
    async def call_refusal():
        failure = CallFailure("tool_access_denied", status_code=403, detail="not allowed")
        raise _translate_call_failure(failure)

    @app.get("/auth-refusal")
    async def auth_refusal():
        raise HTTPException(status_code=401, detail="not authenticated")

    @app.get("/validated")
    async def validated(count: int):
        return {"count": count}

    handler = analytics.install_fault_handler()
    await _run_through_uvicorn(app, "/call-refusal")
    await _run_through_uvicorn(app, "/auth-refusal")
    await _run_through_uvicorn(app, "/validated", b"count=not-an-int")
    analytics.remove_fault_handler(handler)
    assert _exception_events() == []


def test_the_lifespan_drains_analytics_after_everything_that_reports_into_it():
    """Order matters now that audit and archive report their losses at ERROR: analytics is their
    sink, so draining it first leaves those events queued behind a cancelled flusher and they are
    lost — silently, and exactly at shutdown, which is when a loss is most worth hearing about."""
    import pathlib

    from treg import bootstrap

    source = pathlib.Path(bootstrap.__file__).read_text()
    drains = ("audit.drain()", "archive.drain()", "analytics.drain()")
    assert all(name in source for name in drains)
    assert sorted(drains, key=source.index) == list(drains)


async def test_typed_pool_saturation_is_explicitly_captured(enabled):
    request = Request(_scope("/health"))
    response = await _pool_saturated(request, PoolTimeoutError("QueuePool limit reached"))
    assert response.status_code == 503
    assert _exception_events()[0]["properties"]["component"] == "db_pool"
