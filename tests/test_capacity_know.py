"""Step B of docs/PROVIDER-CAPACITY-PLAN.md — the "Know" layer: policies, snapshots, the sweep,
and the latest-state view. Observe-only: nothing here touches the call path."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from treg import ratestore
from treg.infra.db import reset_db, session_maker
from treg.domain.capacity import collectors
from treg.domain.capacity.policy import (
    AGGREGATORS, LatestState, default_policy, ensure_policies, latest_state, policy_population,
)
from treg.domain.capacity.sweep import STATE_NS, run_sweep, snapshot_from
from treg.domain.capacity.view import LatestStateView
from treg.models import CapacityPolicy, CapacitySnapshot
from treg.timeutil import utcnow_naive


async def test_policy_population_covers_every_platform_slot_and_both_aggregators():
    pop = policy_population()
    assert set(collectors.all_platform_providers()) <= set(pop)
    assert {f"overflow:{a}" for a in AGGREGATORS} <= set(pop)
    assert "tomba_secret" not in pop  # the second half of a credential pair is not an account


async def test_import_creates_one_policy_per_account_and_flags_unknowns_without_overwriting():
    await reset_db()
    async with session_maker() as db:
        db.add(CapacityPolicy(provider="findymail", capacity_type="credits", funding_mode="manual",
                              source="api", owner_email="jason@example.com"))
        await db.commit()
    async with session_maker() as db:
        unknown = await ensure_policies(db, has_key=lambda p: p in ("findymail", "crustdata"))
        await db.commit()
    async with session_maker() as db:
        rows = {r.provider: r for r in (await db.execute(select(CapacityPolicy))).scalars()}
    assert set(rows) == set(policy_population())
    assert rows["findymail"].owner_email == "jason@example.com"  # the hand-edited row survived
    assert rows["crustdata"].capacity_type == "unknown" and "crustdata" in unknown
    assert rows["findymail"].enabled and not rows["dataforseo"].enabled  # enabled ⇔ a key exists
    assert rows["overflow:orthogonal"].source == "manual"
    assert rows["hunter"].quota["period"] == "billing"
    assert rows["leadsforge"].rate_limit == {"limit": 120, "window_s": 60, "source": "headers"}
    # a second import is a no-op
    async with session_maker() as db:
        assert await ensure_policies(db, has_key=lambda p: False) == []


def test_snapshot_never_carries_a_credential():
    snap = snapshot_from("x", {"value": 5, "unit": "credits", "note": "plan pro, key SECRET-LOOKING-VALUE-abc"})
    assert "sk_live" not in snap.note and snap.remaining == 5.0 and snap.error == ""
    fail = snapshot_from("x", {"value": None, "unit": "", "note": "HTTPStatusError: 401"})
    assert fail.remaining is None and fail.error.startswith("HTTPStatusError") and fail.confidence == "stale"
    assert snapshot_from("x", {"value": None, "unit": "", "no_api": True, "note": "n/a"}).error == "no_balance_api"


def test_latest_state_rules():
    pol = default_policy("findymail", has_key=True)
    now = utcnow_naive()
    assert latest_state(pol, None, now).health == "unknown"
    ok = CapacitySnapshot(provider="findymail", observed_at=now, remaining=120, unit="credits")
    assert latest_state(pol, ok, now).health == "ok"
    old = CapacitySnapshot(provider="findymail", observed_at=now - timedelta(hours=7), remaining=120, unit="credits")
    assert latest_state(pol, old, now).health == "stale"
    failed = CapacitySnapshot(provider="findymail", observed_at=now, remaining=None, error="boom")
    st = latest_state(pol, failed, now)
    assert st.health == "stale" and not st.is_exhausted(now)  # a failed collector never refuses calls
    reset = now + timedelta(hours=3)
    empty = CapacitySnapshot(provider="findymail", observed_at=now, remaining=0, unit="credits", resets_at=reset)
    st = latest_state(pol, empty, now)
    assert st.health == "exhausted" and st.exhausted_until == reset and st.is_exhausted(now)
    assert not st.is_exhausted(reset + timedelta(seconds=1))
    assert LatestState.from_json(st.to_json()) == st


async def test_sweep_one_failing_collector_does_not_stop_the_others(monkeypatch):
    await reset_db()
    monkeypatch.setenv("TREG_PLATFORM_KEY_FINDYMAIL", "k1")
    monkeypatch.setenv("TREG_PLATFORM_KEY_LEADSFORGE", "k2")
    from treg.config import get_settings
    get_settings.cache_clear()

    async def good(c, key):
        assert key == "k1"
        return {"value": 42, "unit": "finder credits", "note": ""}

    async def bad(c, key):
        raise httpx.ConnectError("dns")

    monkeypatch.setitem(collectors.BALANCE_ROUTES, "findymail", good)
    monkeypatch.setitem(collectors.BALANCE_ROUTES, "leadsforge", bad)
    try:
        async with session_maker() as db:
            result = await run_sweep(db, only={"findymail", "leadsforge", "overflow:monid"})
    finally:
        get_settings.cache_clear()
    assert result.states["findymail"].health == "ok" and result.states["findymail"].remaining == 42
    assert result.states["leadsforge"].health == "stale" and "ConnectError" in result.states["leadsforge"].note
    assert result.states["overflow:monid"].health == "stale"
    async with session_maker() as db:
        snaps = (await db.execute(select(CapacitySnapshot))).scalars().all()
        assert {s.provider for s in snaps} == {"findymail", "leadsforge", "overflow:monid"}
        assert next(s for s in snaps if s.provider == "leadsforge").error.startswith("ConnectError")
        assert await ratestore.kv_get(db, STATE_NS, "findymail") == result.states["findymail"].to_json()


async def test_view_sees_a_published_state_from_a_fresh_process_within_the_ttl():
    await reset_db()
    state = LatestState("findymail", 0.0, "credits", utcnow_naive(), "exact",
                        exhausted_until=utcnow_naive() + timedelta(hours=1), health="exhausted")
    view = LatestStateView(ttl_s=60)
    await view.load()
    assert view.get("findymail") is None
    async with session_maker() as db:  # the sweep (another process) publishes
        await ratestore.kv_put(db, STATE_NS, "findymail", state.to_json(), ttl_s=3600)
        await db.commit()
    assert view.get("findymail") is None  # still cached: within the TTL, no DB read
    view.invalidate()
    await view.load()
    assert view.is_exhausted("findymail")
    assert view.get("findymail").exhausted_until == state.exhausted_until


def test_capacity_view_getters_are_sync_and_io_free():
    import inspect
    assert not inspect.iscoroutinefunction(LatestStateView.get)
    assert not inspect.iscoroutinefunction(LatestStateView.is_exhausted)


def test_worker_cli_parses_the_sweep_command(monkeypatch):
    from treg import worker
    seen = {}

    async def fake(args):
        seen["only"] = args.only
        return 0

    monkeypatch.setattr(worker, "_capacity_sweep", fake)
    assert worker.main(["capacity", "sweep", "--only", "hunter,lusha"]) == 0
    assert seen["only"] == "hunter,lusha"
    with pytest.raises(SystemExit):
        worker.main(["capacity"])
