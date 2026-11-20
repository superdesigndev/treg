"""Money observations cannot own a transaction, suppress a failure or grow with team count."""

import asyncio
from types import SimpleNamespace

import pytest

from treg import analytics, bootstrap
from treg.infra import money_admission, money_timing


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    # The shared emitter also drains admission; unrelated earlier calls are not this window.
    monkeypatch.setattr(money_admission, "_windows", {})
    monkeypatch.setattr(money_admission, "_current", {})
    monkeypatch.setattr(money_timing, "_clock", lambda: now[0])
    monkeypatch.setattr(money_timing, "_opened", 0.0)
    monkeypatch.setattr(money_timing, "_windows", {})
    monkeypatch.setattr(money_timing, "_inflight", {})
    return now


def test_observation_includes_session_cleanup_and_keeps_phases_separate(clock):
    with money_timing.observe_money("reserve", org_id=7, call_id="opaque-call") as timing:
        with timing.phase("preflight"):
            clock[0] += 0.020
        with timing.phase("ledger"):
            clock[0] += 0.030
        with timing.phase("commit"):
            clock[0] += 0.010
        clock[0] += 0.040  # session cleanup remains in the enclosing operation
    [(props, slowest)] = money_timing.snapshot()
    assert props["completed"] == 1 and props["failed"] == 0
    assert props["duration_total_ms"] == 100
    assert props["preflight_total_ms"] == 20
    assert props["ledger_total_ms"] == 30
    assert props["commit_total_ms"] == 10
    assert props["inflight"] == 0 and props["inflight_peak"] == 1
    assert props["process_instance"] == money_timing.PROCESS_INSTANCE
    assert "org_id" not in props and "call_id" not in props
    assert slowest is None
    assert sum(value for key, value in props.items() if key.startswith("duration_bucket_")) == 1


def test_rollover_keeps_active_operations_and_resets_completed_counts(clock):
    with money_timing.observe_money("close"):
        with money_timing.observe_money("close"):
            clock[0] += 1
            [(first, _)] = money_timing.snapshot()
            assert first["completed"] == 0
            assert first["inflight"] == 2 and first["inflight_peak"] == 2
        clock[0] += 1
    [(second, _)] = money_timing.snapshot()
    assert second["completed"] == 2
    assert second["duration_total_ms"] == 3000
    assert second["inflight"] == 0 and second["inflight_peak"] == 2
    assert money_timing.snapshot() == []


@pytest.mark.parametrize("error", [ValueError("private detail"), asyncio.CancelledError()])
def test_failure_and_cancellation_propagate_unchanged(clock, error):
    with pytest.raises(type(error)) as caught:
        with money_timing.observe_money("close") as timing:
            with timing.phase("ledger"):
                clock[0] += 1
                raise error
    assert caught.value is error
    [(props, slowest)] = money_timing.snapshot()
    assert props["failed"] == 1
    assert props["cancelled"] == int(isinstance(error, asyncio.CancelledError))
    assert props["ledger_total_ms"] == 1000
    assert props["inflight"] == 0
    assert "private detail" not in repr((props, slowest))


def test_deadlocks_are_counted_by_sqlstate_not_error_text(clock):
    error = RuntimeError("private database detail")
    error.orig = SimpleNamespace(sqlstate="40P01")
    with pytest.raises(RuntimeError):
        with money_timing.observe_money("deferred", batch_size=3):
            raise error
    [(props, _)] = money_timing.snapshot()
    assert props["deadlocks"] == 1 and props["batch_items"] == 3


def test_cardinality_is_fixed_and_only_slowest_sample_is_retained(clock):
    for org_id in range(2000):
        with money_timing.observe_money("reserve", org_id=org_id, call_id="opaque-call") as timing:
            with timing.phase(f"untrusted-phase-{org_id}"):
                clock[0] += 1.0 + org_id / 1000
        with money_timing.observe_money(f"untrusted-op-{org_id}"):
            pass
    [(props, slowest)] = money_timing.snapshot()
    assert props["completed"] == 2000
    assert slowest["org_id"] == 1999
    assert slowest["duration_ms"] == 2999
    assert slowest["phases_ms"] == {}
    assert len(props) < 30


def test_one_sample_log_per_operation_and_window_with_private_data_excluded(clock, monkeypatch, caplog):
    captured = []
    monkeypatch.setattr(analytics, "capture", lambda d, e, p: captured.append((d, e, p)))
    for duration in (1.1, 1.2, 1.3):
        with money_timing.observe_money("reserve", org_id=7, call_id="person@example.com\nbody"):
            clock[0] += duration
    bootstrap._emit_money_timings()
    bootstrap._emit_money_timings()
    assert len(captured) == 1
    assert captured[0][1] == "money_operation_gauge"
    assert captured[0][2]["completed"] == 3
    assert len(caplog.records) == 1
    assert "person@example.com" not in caplog.text and "body" not in caplog.text
    assert '"duration_ms":1300.0' in caplog.text
    assert '"org_id":7' in caplog.text


def test_diagnostic_failure_never_replaces_business_exception(clock, monkeypatch):
    def unavailable():
        raise RuntimeError("diagnostic unavailable")

    original = ValueError("business failure")
    with pytest.raises(ValueError) as caught:
        with money_timing.observe_money("reserve") as timing:
            monkeypatch.setattr(money_timing, "_clock", unavailable)
            with timing.phase("ledger"):
                raise original
    assert caught.value is original


def test_emission_failure_does_not_escape(clock, monkeypatch):
    with money_timing.observe_money("reserve"):
        pass

    def unavailable(*_):
        raise RuntimeError("sink unavailable")

    monkeypatch.setattr(analytics, "capture", unavailable)
    bootstrap._emit_money_timings()
