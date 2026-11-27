"""Money observations cannot own a transaction, suppress a failure or grow with team count."""

import asyncio

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
