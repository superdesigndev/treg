"""Lease failures stay observable without exposing keys or changing money fallback behavior."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from redis import exceptions as redis_errors

from treg.infra import kv


_PHASES = ("acquire", "renew", "release")
_SECRET = "redis://sensitive-user:password@private-host/3 private-key private-token"


@pytest.fixture(autouse=True)
def clear_lease_errors():
    kv.drain_lease_errors()
    kv.drain_lease_timings()
    yield
    kv.drain_lease_errors()
    kv.drain_lease_timings()


async def _invoke(store, phase):
    method = getattr(store, f"{phase}_lease")
    args = ("private-key", "private-token")
    if phase != "release":
        args += (15000,)
    return await method(*args)


def _store_with(command):
    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(set=command, eval=command)
    return store


def _failing_store(exc, clock=None, elapsed_s=0.025):
    async def command(*args, **kwargs):
        if clock is not None:
            clock[0] += elapsed_s
        raise exc
    return _store_with(command)


@pytest.mark.parametrize("phase", _PHASES)
@pytest.mark.parametrize(("exception_type", "error_type"), (
    (TimeoutError, "deadline_exceeded"),
    (redis_errors.TimeoutError, "redis_timeout"),
    (redis_errors.AuthenticationError, "authentication"),
    (redis_errors.NoPermissionError, "permission"),
    (redis_errors.AuthorizationError, "permission"),
    (redis_errors.ConnectionError, "connection"),
    (redis_errors.ResponseError, "response"),
    (RuntimeError, "other"),
))
async def test_lease_failures_are_classified_without_secret_data(
    monkeypatch, caplog, phase, exception_type, error_type,
):
    clock = [100.0]
    monkeypatch.setattr(kv, "_clock", lambda: clock[0])
    monkeypatch.setattr(kv, "_utcnow", lambda: "2020-01-01T00:00:00+00:00")
    assert await _invoke(_failing_store(exception_type(_SECRET), clock), phase) == "unavailable"

    rows = kv.drain_lease_errors()
    assert len(rows) == 1
    expected_count = (2 if phase in {"renew", "release"} and error_type in {
        "deadline_exceeded", "redis_timeout", "connection",
    } else 1)
    assert rows[0] == {
        "event": "kv_lease_error", "phase": phase, "error_type": error_type, "count": expected_count,
        "first_failed_at": "2020-01-01T00:00:00+00:00",
        "last_failed_at": "2020-01-01T00:00:00+00:00",
        "elapsed_max_ms": pytest.approx(25),
        "socket_timeout_ms": 1000, "operation_deadline_ms": 1500,
    }
    exposed = json.dumps(rows) + caplog.text
    for secret in ("sensitive-user", "password", "private-host", "private-key", "private-token"):
        assert secret not in exposed
    assert kv.drain_lease_errors() == []


async def test_failures_aggregate_first_last_count_and_max_then_start_a_new_window(monkeypatch):
    clock = [100.0]
    timestamps = iter(("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:01+00:00",
                       "2020-01-01T00:01:00+00:00"))
    monkeypatch.setattr(kv, "_clock", lambda: clock[0])
    monkeypatch.setattr(kv, "_utcnow", lambda: next(timestamps))
    for elapsed in (0.025, 0.050):
        await _invoke(_failing_store(redis_errors.ConnectionError(_SECRET), clock, elapsed), "acquire")
    row, = kv.drain_lease_errors()
    assert row["count"] == 2
    assert row["first_failed_at"] == "2020-01-01T00:00:00+00:00"
    assert row["last_failed_at"] == "2020-01-01T00:00:01+00:00"
    assert row["elapsed_max_ms"] == pytest.approx(50)

    await _invoke(_failing_store(redis_errors.ConnectionError(_SECRET), clock), "acquire")
    next_row, = kv.drain_lease_errors()
    assert next_row["count"] == 1
    assert next_row["first_failed_at"] == next_row["last_failed_at"] == "2020-01-01T00:01:00+00:00"
    assert next_row["elapsed_max_ms"] == pytest.approx(25)
    assert row["count"] == 2  # Drained rows are not mutated by the following reporting window.


async def test_unknown_error_classes_and_messages_cannot_create_unbounded_labels():
    for index in range(100):
        error = type(f"VariableError{index}", (Exception,), {})
        for phase in _PHASES:
            await _invoke(_failing_store(error(f"{_SECRET}-{index}")), phase)
    rows = kv.drain_lease_errors()
    assert len(rows) == len(_PHASES)
    assert {row["phase"] for row in rows} == set(_PHASES)
    assert all(row["error_type"] == "other" and row["count"] == 100 for row in rows)


@pytest.mark.parametrize("phase", _PHASES)
async def test_task_cancellation_propagates_without_becoming_a_kv_error(phase):
    with pytest.raises(asyncio.CancelledError):
        await _invoke(_failing_store(asyncio.CancelledError()), phase)
    assert kv.drain_lease_errors() == []


@pytest.mark.parametrize("phase", _PHASES)
async def test_outer_deadline_is_distinct_from_redis_socket_timeout(monkeypatch, phase):
    monkeypatch.setattr(kv, "_LEASE_ATTEMPT_TIMEOUT_S", 0.002)
    monkeypatch.setattr(kv, "_LEASE_RETRY_BUDGET_S", 0.004)

    async def command(*args, **kwargs):
        await asyncio.Event().wait()
    assert await _invoke(_store_with(command), phase) == "unavailable"
    row, = kv.drain_lease_errors()
    assert row["error_type"] == "deadline_exceeded"
    assert row["phase"] == phase
    assert row["elapsed_max_ms"] >= 0


@pytest.mark.parametrize("phase", _PHASES)
async def test_diagnostic_failure_preserves_unavailable_fallback(monkeypatch, phase):
    def broken_clock():
        raise RuntimeError("diagnostic clock failed")
    monkeypatch.setattr(kv, "_utcnow", broken_clock)
    assert await _invoke(_failing_store(redis_errors.ConnectionError(_SECRET)), phase) == "unavailable"


@pytest.mark.parametrize("phase", _PHASES)
@pytest.mark.parametrize("result", (True, False))
async def test_success_and_contention_are_not_diagnostic_errors(phase, result):
    async def command(*args, **kwargs):
        return result
    expected = {"acquire": ("busy", "acquired"), "renew": ("lost", "renewed"),
                "release": ("lost", "released")}[phase][result]
    assert await _invoke(_store_with(command), phase) == expected
    assert kv.drain_lease_errors() == []
