"""Admission queues outside database sessions and never owns financial correctness."""
import asyncio
import gc
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from treg.config import Settings
from treg.infra import kv, money_admission as admission


def test_admission_wait_default_and_lease_are_unchanged(monkeypatch):
    monkeypatch.delenv("TREG_MONEY_ADMISSION_WAIT_S", raising=False)
    monkeypatch.delenv("TREG_MONEY_ADMISSION_LEASE_S", raising=False)
    settings = Settings(_env_file=None)
    assert settings.money_admission_wait_s == 5.0
    assert settings.money_admission_lease_s == 15.0


@pytest.mark.parametrize("wait_s", [0.001, 60, 90, 120])
def test_admission_wait_accepts_positive_values_through_120(monkeypatch, wait_s):
    monkeypatch.setenv("TREG_MONEY_ADMISSION_WAIT_S", str(wait_s))
    settings = Settings(_env_file=None, money_admission_lease_s=15.0)
    assert settings.money_admission_wait_s == wait_s
    # The lease starts on acquisition and renews; it need not exceed the queue wait.
    assert settings.money_admission_lease_s == 15.0


@pytest.mark.parametrize("wait_s, error_type", [
    (-1, "greater_than"), (0, "greater_than"), (121, "less_than_equal"),
])
def test_admission_wait_rejects_values_outside_bounds(monkeypatch, wait_s, error_type):
    monkeypatch.setenv("TREG_MONEY_ADMISSION_WAIT_S", str(wait_s))
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    [error] = caught.value.errors(include_input=False)
    assert error["loc"] == ("money_admission_wait_s",)
    assert error["type"] == error_type


class LeaseStore:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.unavailable = False
        self.lose_on_renew = False
        self.renewed = asyncio.Event()

    async def acquire_lease(self, key, token, ttl_ms):
        self.calls.append(("acquire", key))
        if self.unavailable:
            return "unavailable"
        now = time.monotonic()
        if key in self.values and self.values[key][1] > now:
            return "busy"
        self.values[key] = (token, now + ttl_ms / 1000)
        return "acquired"

    async def renew_lease(self, key, token, ttl_ms, *, deadline=None):
        self.renewed.set()
        if self.lose_on_renew:
            self.values[key] = ("new-owner", time.monotonic() + 10)
            return "lost"
        if self.unavailable:
            return "unavailable"
        if self.values.get(key, (None,))[0] != token:
            return "lost"
        self.values[key] = (token, time.monotonic() + ttl_ms / 1000)
        return "renewed"

    async def release_lease(self, key, token):
        self.calls.append(("release", key))
        if self.unavailable:
            return "unavailable"
        if self.values.get(key, (None,))[0] != token:
            return "lost"
        del self.values[key]
        return "released"


@pytest.fixture
def gate(monkeypatch):
    settings = SimpleNamespace(
        money_admission_enabled=True, money_admission_org_ids=[],
        money_admission_wait_s=0.5, money_admission_lease_s=0.15)
    store = LeaseStore()
    monkeypatch.setattr(admission, "get_settings", lambda: settings)
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "store", lambda: store)
    monkeypatch.setattr(admission, "_local_loop", None)
    monkeypatch.setattr(admission, "_windows", {})
    monkeypatch.setattr(admission, "_current", {})
    return settings, store


def completed():
    return [row for row in admission.snapshot() if row["completed"]]


async def test_disabled_baseline_never_contacts_kv_or_locks(gate, monkeypatch):
    settings, store = gate
    settings.money_admission_enabled = False
    monkeypatch.setattr(admission, "_local_lock", lambda _: pytest.fail("must not queue"))
    monkeypatch.setattr(kv, "configured", lambda: pytest.fail("must not contact store"))
    async with admission.admit([7], operation="close"):
        pass
    [row] = completed()
    assert row["mode"] == "disabled" and row["completed"] == 1
    assert not store.calls


async def test_empty_release_is_excluded_from_baseline(gate):
    async with admission.admit([], operation="close"):
        pass
    assert admission.snapshot() == []


@pytest.mark.parametrize("operation", ["reserve", "release"])
@pytest.mark.parametrize("orgs", [[], [None], [0], [-3], [True], [7, None]])
async def test_balance_ops_refuse_missing_identity_before_the_body(gate, operation, orgs):
    entered = False
    with pytest.raises(admission.AdmissionRefused, match="missing_identity") as caught:
        async with admission.admit(orgs, operation=operation):
            entered = True
    assert not entered
    assert not hasattr(caught.value, "status_code")
    assert admission.snapshot() == []
    with pytest.raises(admission.AdmissionRefused):
        admission.require_balance_orgs(orgs)


@pytest.mark.parametrize("operation", ["reserve", "release"])
@pytest.mark.parametrize("reason", ["kv_not_configured", "kv_unavailable", "wait_timeout"])
async def test_balance_ops_keep_existing_wait_and_kv_fallback(gate, monkeypatch, operation, reason):
    settings, store = gate
    if reason == "kv_not_configured":
        monkeypatch.setattr(kv, "configured", lambda: False)
    elif reason == "kv_unavailable":
        store.unavailable = True
    else:
        settings.money_admission_wait_s = 0.02
        store.values["money-admission:org:7"] = ("another-owner", time.monotonic() + 10)
    entered = False
    async with admission.admit([7], operation=operation):
        entered = True
        assert not admission._local_lock(7).locked()
    assert entered
    [row] = completed()
    assert row["operation"] == operation
    assert row["mode"] == "fallback" and row["fallback_" + reason] == 1
    assert row["failed"] == 0


async def test_reserve_and_release_share_one_org_lease(gate):
    _, store = gate
    entered, leave = asyncio.Event(), asyncio.Event()

    async def holding():
        async with admission.admit([7], operation="reserve"):
            entered.set()
            await leave.wait()

    task = asyncio.create_task(holding())
    await entered.wait()
    release_entered = asyncio.Event()

    async def releasing():
        async with admission.admit([7], operation="release"):
            release_entered.set()

    waiter = asyncio.create_task(releasing())
    await asyncio.sleep(0)
    assert not release_entered.is_set()
    async with admission.admit([8], operation="release"):
        assert not release_entered.is_set()
        assert set(store.values) == {"money-admission:org:7", "money-admission:org:8"}
    leave.set()
    await asyncio.gather(task, waiter)
    assert release_entered.is_set() and not store.values


async def test_same_org_waits_outside_scope_but_other_org_can_run(gate):
    _, store = gate
    entered, leave = asyncio.Event(), asyncio.Event()

    async def first():
        async with admission.admit([7], operation="close"):
            entered.set()
            await leave.wait()

    task = asyncio.create_task(first())
    await entered.wait()
    second_entered = asyncio.Event()

    async def second():
        async with admission.admit([7], operation="close"):
            second_entered.set()

    waiter = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    async with admission.admit([8], operation="close"):
        assert not second_entered.is_set()
        assert len(store.values) == 2
    [row] = admission.snapshot()
    assert row["waiting"] == 1 and row["active"] == 1
    assert row["active_peak"] == 2
    leave.set()
    await asyncio.gather(task, waiter)
    assert second_entered.is_set() and not store.values
    [row] = completed()
    assert row["waiting"] == row["active"] == 0


async def test_org_order_is_sorted_deduplicated_and_idle_locks_are_reclaimed(gate):
    _, store = gate
    async with admission.admit([9, 3, 9, 7], operation="deferred"):
        assert [key for action, key in store.calls if action == "acquire"] == [
            "money-admission:org:3", "money-admission:org:7", "money-admission:org:9"]
    gc.collect()
    assert not admission._local_locks
    assert not store.values


async def test_allowlist_only_gates_selected_orgs(gate):
    settings, store = gate
    settings.money_admission_org_ids = [7]
    async with admission.admit([8], operation="close"):
        pass
    async with admission.admit([8, 7], operation="deferred"):
        assert list(store.values) == ["money-admission:org:7"]
    rows = completed()
    assert {(row["operation"], row["mode"]) for row in rows} == {
        ("close", "disabled"), ("deferred", "redis")}


async def test_unknown_identity_falls_back_as_a_whole_without_locking_known_org(gate):
    _, store = gate
    async with admission.admit([7, None], operation="deferred"):
        assert not store.values
    [row] = completed()
    assert row["mode"] == "fallback" and row["fallback_missing_identity"] == 1
    assert not store.calls


@pytest.mark.parametrize("reason", ["kv_not_configured", "kv_unavailable"])
async def test_kv_failure_preserves_database_execution(gate, monkeypatch, reason):
    _, store = gate
    if reason == "kv_not_configured":
        monkeypatch.setattr(kv, "configured", lambda: False)
    else:
        store.unavailable = True
    reached = False
    async with admission.admit([7], operation="close"):
        reached = True
        assert not admission._local_lock(7).locked()
    assert reached
    [row] = completed()
    assert row["mode"] == "fallback" and row["fallback_" + reason] == 1
    assert row["failed"] == 0


async def test_total_deadline_covers_multi_org_wait_and_releases_partial_leases(gate):
    settings, store = gate
    settings.money_admission_wait_s = 0.03
    store.values["money-admission:org:9"] = ("another-owner", time.monotonic() + 10)
    async with admission.admit([3, 9], operation="hub"):
        assert "money-admission:org:3" not in store.values
        assert not admission._local_lock(3).locked()
        assert not admission._local_lock(9).locked()
    [row] = completed()
    assert row["mode"] == "fallback" and row["fallback_wait_timeout"] == 1
    assert store.values["money-admission:org:9"][0] == "another-owner"


async def test_cancelled_local_waiter_does_not_enter_or_leak(gate):
    async with admission.admit([7], operation="close"):
        async def waiting():
            async with admission.admit([7], operation="close"):
                pytest.fail("cancelled waiter must not enter")
        task = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admission._local_lock(7).locked()
    assert not gate[1].values
    [row] = completed()
    assert row["completed"] == 2 and row["cancelled"] == 1 and row["waiting"] == 0


async def test_cancel_during_set_cleans_lease_even_if_response_was_not_observed(gate, monkeypatch):
    _, store = gate
    acquired = asyncio.Event()
    original = store.acquire_lease

    async def uncertain(*args):
        await original(*args)
        acquired.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "acquire_lease", uncertain)
    async def work():
        async with admission.admit([7], operation="close"):
            pytest.fail("cancelled acquisition")
    task = asyncio.create_task(work())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not store.values
    assert not admission._local_lock(7).locked()


async def test_lease_covers_transaction_rollback_and_session_exit(gate):
    _, store = gate
    rollback_started, rollback_finished = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def session():
        try:
            yield
        finally:
            rollback_started.set()
            await rollback_finished.wait()
            assert store.values  # Outer admission cannot release before DB cleanup.

    async def work():
        async with admission.admit([7], operation="close"):
            async with session():
                raise ValueError("business failure")

    task = asyncio.create_task(work())
    await rollback_started.wait()
    assert store.values
    rollback_finished.set()
    with pytest.raises(ValueError, match="business failure"):
        await task
    assert not store.values


async def test_repeated_cancellation_joins_lease_cleanup(gate, monkeypatch):
    _, store = gate
    releasing, finish_release = asyncio.Event(), asyncio.Event()
    original = store.release_lease

    async def release(*args):
        releasing.set()
        await finish_release.wait()
        return await original(*args)

    monkeypatch.setattr(store, "release_lease", release)
    entered = asyncio.Event()
    async def work():
        async with admission.admit([7], operation="close"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    await releasing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and store.values
    finish_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not store.values
    assert not admission._local_lock(7).locked()


async def test_lease_loss_does_not_cancel_money_and_old_owner_cannot_delete_new_lease(gate):
    _, store = gate
    store.lose_on_renew = True
    async with admission.admit([7], operation="close"):
        await asyncio.wait_for(store.renewed.wait(), 1)
        await asyncio.sleep(0)
        # Simulates the database transaction completing safely after isolation degraded.
        assert store.values["money-admission:org:7"][0] == "new-owner"
    assert store.values["money-admission:org:7"][0] == "new-owner"
    [row] = completed()
    assert row["lease_lost"] == 1 and row["failed"] == 0


async def test_ambiguous_release_retry_does_not_claim_lease_was_lost(gate, monkeypatch):
    _, store = gate
    original = store.release_lease

    async def already_released(key, token):
        # The first DEL succeeded but its reply was lost; a retry found no owner.
        assert await original(key, token) == "released"
        return "not_owned"

    monkeypatch.setattr(store, "release_lease", already_released)
    async with admission.admit([7], operation="close"):
        pass
    [row] = completed()
    assert row["lease_lost"] == 0 and row["kv_errors"] == 0
    assert row["completed"] == 1 and row["failed"] == 0
    assert not store.values
    assert not admission._local_lock(7).locked()


async def test_ambiguous_release_retry_preserves_previously_observed_lease_loss(gate, monkeypatch):
    _, store = gate
    store.lose_on_renew = True

    async def no_longer_owner(key, token):
        assert store.values[key][0] == "new-owner"
        return "not_owned"

    monkeypatch.setattr(store, "release_lease", no_longer_owner)
    async with admission.admit([7], operation="close"):
        await asyncio.wait_for(store.renewed.wait(), 1)
        await asyncio.sleep(0)
    [row] = completed()
    assert row["lease_lost"] == 1 and row["failed"] == 0
    assert store.values["money-admission:org:7"][0] == "new-owner"
    assert not admission._local_lock(7).locked()


async def test_lease_renewal_preserves_ownership_while_body_runs(gate):
    _, store = gate
    async with admission.admit([7], operation="close"):
        await asyncio.wait_for(store.renewed.wait(), 1)
        assert store.values["money-admission:org:7"][1] > time.monotonic()
    [row] = completed()
    assert row["lease_lost"] == 0


@pytest.mark.parametrize("pause_s", (6, 10, 15))
async def test_acquire_reply_does_not_restart_the_lease_validity_clock(gate, monkeypatch, pause_s):
    settings, store = gate
    settings.money_admission_lease_s = 15
    now = [100.0]
    monkeypatch.setattr(admission, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = store.acquire_lease

    async def delayed_reply(*args):
        result = await original(*args)
        now[0] += pause_s
        return result

    monkeypatch.setattr(store, "acquire_lease", delayed_reply)
    async with admission.admit([7], operation="reserve"):
        assert bool(store.values) == (pause_s < 15)
    [row] = completed()
    assert row["mode"] == ("redis" if pause_s < 15 else "fallback")
    assert row["fallback_lease_lost"] == (pause_s >= 15)
    assert not store.values


async def test_multi_org_acquire_cannot_enter_after_an_earlier_lease_expired(gate, monkeypatch):
    settings, store = gate
    settings.money_admission_lease_s = 15
    now = [100.0]
    monkeypatch.setattr(admission, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = store.acquire_lease

    async def delayed_second(key, *args):
        if key.endswith(":9"):
            now[0] += 16
            store.values["money-admission:org:3"] = ("new-owner", time.monotonic() + 10)
        return await original(key, *args)

    monkeypatch.setattr(store, "acquire_lease", delayed_second)
    async with admission.admit([3, 9], operation="deferred"):
        assert not admission._local_lock(3).locked()
        assert not admission._local_lock(9).locked()
        assert list(store.values) == ["money-admission:org:3"]
    assert store.values["money-admission:org:3"][0] == "new-owner"
    [row] = completed()
    assert row["mode"] == "fallback" and row["fallback_lease_lost"] == 1


@pytest.mark.parametrize("pause_s", (6, 10))
async def test_renewer_resuming_after_a_pause_checks_last_confirmed_deadline(gate, monkeypatch, pause_s):
    _, store = gate
    now = [100.0]
    monkeypatch.setattr(admission, "time", SimpleNamespace(monotonic=lambda: now[0]))
    observed = admission._Observation("close")
    owned = admission._Gate(observed, 15)
    try:
        assert await owned.acquire([7], 1) is None
        owned.renewer.cancel()
        await asyncio.gather(owned.renewer, return_exceptions=True)
        owned.renewer = None
        wakeups = 0

        async def delayed_wakeup(interval):
            nonlocal wakeups
            wakeups += 1
            if wakeups > 1:
                raise asyncio.CancelledError
            now[0] += interval + pause_s

        # Advance the gate's clock without pausing the suite or the real asyncio scheduler.
        with monkeypatch.context() as patch:
            patch.setattr(admission, "asyncio", SimpleNamespace(
                sleep=delayed_wakeup, timeout=asyncio.timeout, CancelledError=asyncio.CancelledError))
            if pause_s < 10:
                with pytest.raises(asyncio.CancelledError):
                    await owned._renew()
            else:
                await owned._renew()
        assert store.renewed.is_set() == (pause_s < 10)
        assert owned.lost == (pause_s >= 10)
        assert observed.lease_lost == (pause_s >= 10)
        assert observed.deadline_expired == (pause_s >= 10)
        assert observed.kv_errors == 0
        assert admission._local_lock(7).locked()  # Loss never releases an active session's gate.
        assert sum(phase == "acquire" for phase, _ in store.calls) == 1
    finally:
        await owned.release()


async def test_renewal_reply_after_confirmed_validity_does_not_restore_ownership(gate, monkeypatch):
    _, store = gate
    now = [100.0]
    monkeypatch.setattr(admission, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = store.renew_lease

    async def late_reply(*args, **kwargs):
        result = await original(*args, **kwargs)
        now[0] += 10  # Simulate a reply observed after the old lease's remaining validity.
        return result

    monkeypatch.setattr(store, "renew_lease", late_reply)
    async with admission.admit([7], operation="close"):
        await asyncio.wait_for(store.renewed.wait(), 1)
        await asyncio.sleep(0)
        assert admission._local_lock(7).locked()
    [row] = completed()
    assert row["lease_lost"] == 1 and row["failed"] == 0
    assert row["deadline_expired"] == 1 and row["kv_errors"] == 0
    assert sum(phase == "acquire" for phase, _ in store.calls) == 1


@pytest.mark.parametrize("expiry", ("result", "outer_timeout"))
async def test_renewal_is_bounded_by_remaining_validity_before_cleanup(gate, monkeypatch, expiry):
    _, store = gate
    cancelled = asyncio.Event()

    async def blocked_renewal(*args, **kwargs):
        try:
            if expiry == "result":
                return "deadline_expired"
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(store, "renew_lease", blocked_renewal)
    async with admission.admit([7], operation="close"):
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.sleep(0)
        assert admission._local_lock(7).locked()
        assert store.values  # The caller must still finish its DB rollback/close.
    [row] = completed()
    assert row["lease_lost"] == 1 and row["kv_errors"] == 0 and row["failed"] == 0
    assert row["deadline_expired"] == 1


@pytest.mark.parametrize("error", (TimeoutError, RuntimeError))
async def test_store_exception_before_validity_timeout_is_still_a_kv_error(gate, monkeypatch, error):
    _, store = gate

    async def failed_renewal(*args, **kwargs):
        store.renewed.set()
        raise error("store failure before the gate deadline")

    monkeypatch.setattr(store, "renew_lease", failed_renewal)
    async with admission.admit([7], operation="close"):
        await asyncio.wait_for(store.renewed.wait(), 1)
        assert admission._local_lock(7).locked()
    [row] = completed()
    assert row["lease_lost"] == 1 and row["kv_errors"] == 1
    assert row["deadline_expired"] == 0 and row["failed"] == 0


@pytest.mark.parametrize("failure", (
    "late_transient_after_pause", "deadline_cancel_translated_by_cleanup", "earlier_org_expiry",
    "gate_timeout",
))
async def test_gate_deadline_reaches_each_redis_renewal_attempt(monkeypatch, failure):
    from redis.exceptions import TimeoutError as RedisTimeoutError

    monkeypatch.setattr(kv, "_lease_errors", {})
    monkeypatch.setattr(kv, "_lease_timings", {})
    observation = admission._Observation("deferred")
    owned = admission._Gate(observation, 3)
    owned.leases = [("org:3", "token-a")]
    started = time.monotonic()
    owned.valid_until = {"org:3": started + 0.050}
    if failure == "earlier_org_expiry":
        owned.leases.append(("org:9", "token-b"))
        owned.valid_until = {"org:3": started + 0.100, "org:9": started + 0.020}
    earliest = min(owned.valid_until.values())
    attempts = []

    async def command(script, count, key, token, ttl):
        attempts.append((key, token, time.monotonic()))
        if len(attempts) == 1:
            if failure == "gate_timeout":
                await asyncio.Event().wait()
            elif failure == "late_transient_after_pause":
                time.sleep(0.070)  # Real short loop stall; the timeout callback cannot run.
            elif failure == "deadline_cancel_translated_by_cleanup":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await asyncio.sleep(0.030)
            else:
                try:
                    await asyncio.sleep(0.030)
                except asyncio.CancelledError:
                    pass  # Simulate a driver translating the shorter deadline's cancellation.
            raise RedisTimeoutError("late transient failure")
        return 1

    store = kv.RedisStore.__new__(kv.RedisStore)
    store._client = SimpleNamespace(eval=command)
    owned.store = store

    async def wake(_):
        return None

    # Bypass only the initial cadence sleep. Both timeout layers use real asyncio and clocks.
    monkeypatch.setattr(admission, "asyncio", SimpleNamespace(
        sleep=wake, timeout=asyncio.timeout, CancelledError=asyncio.CancelledError))
    await owned._renew()
    assert len(attempts) == 1
    assert attempts[0][:2] == ("org:3", "token-a")
    assert attempts[0][2] < earliest <= time.monotonic()
    assert owned.lost and observation.lease_lost == 1
    assert observation.deadline_expired == 1 and observation.kv_errors == 0
    errors = kv.drain_lease_errors()
    assert [(row["error_type"], row["count"]) for row in errors] == (
        [] if failure == "gate_timeout" else [("redis_timeout", 1)])


async def test_redis_outage_during_renewal_does_not_interrupt_the_transaction(gate):
    _, store = gate
    transaction_completed = False
    async with admission.admit([7], operation="close"):
        store.unavailable = True
        await asyncio.wait_for(store.renewed.wait(), 1)
        await asyncio.sleep(0)
        transaction_completed = True
    [row] = completed()
    assert transaction_completed and row["failed"] == 0
    assert row["lease_lost"] == 1 and row["kv_errors"] >= 1
    assert not admission._local_lock(7).locked()


async def test_unexpected_store_exception_also_falls_back(gate, monkeypatch):
    async def broken(*_):
        raise RuntimeError("store failure")
    monkeypatch.setattr(gate[1], "acquire_lease", broken)
    async with admission.admit([7], operation="close"):
        assert not admission._local_lock(7).locked()
    [row] = completed()
    assert row["mode"] == "fallback" and row["fallback_gate_error"] == 1
    assert row["failed"] == 0


async def test_expiry_discovered_only_at_release_is_also_reported(gate):
    _, store = gate
    async with admission.admit([7], operation="close"):
        # No await gives the renewer a turn: simulate expiry during an event-loop pause.
        store.values["money-admission:org:7"] = ("new-owner", time.monotonic() + 10)
    [row] = completed()
    assert row["lease_lost"] == 1 and row["failed"] == 0
    assert store.values["money-admission:org:7"][0] == "new-owner"


async def test_business_exception_and_metric_failure_never_get_suppressed(gate, monkeypatch):
    failure = ValueError("original business error")
    async with admission.admit([8], operation="close"):
        pass
    with pytest.raises(ValueError) as caught:
        async with admission.admit([7], operation="close"):
            monkeypatch.setattr(admission, "_clock", lambda: (_ for _ in ()).throw(RuntimeError()))
            raise failure
    assert caught.value is failure
    assert not gate[1].values


async def test_metrics_are_fixed_dimension_and_execution_excludes_wait_and_cleanup(gate, monkeypatch):
    settings, _ = gate
    settings.money_admission_enabled = False
    now = [0.0]
    monkeypatch.setattr(admission, "_clock", lambda: now[0])
    for org_id in range(1, 101):
        async with admission.admit([org_id], operation="close"):
            now[0] += 0.1
    [row] = completed()
    assert row["completed"] == 100
    assert row["execution_total_ms"] == row["total_total_ms"] == 10000
    assert row["wait_total_ms"] == 0
    assert "org_id" not in row and "call_id" not in row
    assert sum(v for k, v in row.items() if k.startswith("wait_bucket_")) == 100
    assert admission.snapshot() == []
