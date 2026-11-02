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


@pytest.mark.parametrize(("provider", "rate_limit", "where"), [
    ("octen", {"limit": 5, "window_s": 1, "source": "policy"}, "dashboard"),
    # The balance route takes an Account API OAuth token, never the API key treg holds.
    ("parallel", {"limit": 600, "window_s": 60, "source": "docs"}, "Platform"),
])
async def test_platform_account_has_a_known_dashboard_only_capacity_policy(
    monkeypatch, provider, rate_limit, where,
):
    from treg.config import get_settings

    monkeypatch.setenv(f"TREG_PLATFORM_KEY_{provider.upper()}", "TEST-KEY")
    get_settings.cache_clear()
    try:
        policy = default_policy(provider, has_key=True)
        assert (policy.capacity_type, policy.funding_mode, policy.source) == (
            "cash", "manual", "manual",
        )
        assert policy.rate_limit == rate_limit
        balance = await collectors.provider_balance(provider)
        assert balance["no_api"] is True
        assert where in balance["note"]
    finally:
        get_settings.cache_clear()


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
    # Icypeas' spent pool read 1.9987e-05 credits for hours: float dust is empty, a real remainder is not.
    dust = CapacitySnapshot(provider="icypeas", observed_at=now, remaining=1.9986932203931718e-05, unit="credits")
    assert latest_state(pol, dust, now).health == "exhausted"
    assert latest_state(pol, CapacitySnapshot(provider="icypeas", observed_at=now, remaining=0.5,
                                              unit="credits"), now).health == "ok"


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


@pytest.mark.parametrize(("provider", "path", "body"), [
    ("fiber-ai", "/v1/get-org-credits", {"output": [{"available": 0, "max": 100}]}),
    ("mobiusengine-jobs", "/v1/balance", {"mode": "wallet", "balance_usd": 0}),
    ("findymail", "/api/credits", {"credits": 0}),
])
async def test_a_swept_empty_account_reaches_the_call_path_under_its_catalog_id(
    monkeypatch, provider, path, body,
):
    """The sweep keys an account by its Settings slot (`fiber_ai`); the call path asks with the
    catalog id (`fiber-ai`). Both must name the same published state."""
    from treg.application.call import keyslots
    from treg.config import get_settings, platform_setting_name

    await reset_db()
    slot = platform_setting_name(provider).removeprefix("platform_key_")
    monkeypatch.setenv(f"TREG_PLATFORM_KEY_{slot.upper()}", "k")
    get_settings.cache_clear()

    def serve(request):
        assert request.url.path == path
        return httpx.Response(200, json=body)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
            async with session_maker() as db:
                result = await run_sweep(db, client=upstream, only={slot})
    finally:
        get_settings.cache_clear()
    assert result.states[slot].health == "exhausted"
    view = LatestStateView()
    await view.load(force=True)
    monkeypatch.setattr(keyslots, "capacity_view", view)
    assert view.get(provider) is not None and view.is_exhausted(provider)
    assert keyslots.exhausted(provider, f"{provider}.x", None)


def test_capacity_view_getters_are_sync_and_io_free():
    import inspect
    assert not inspect.iscoroutinefunction(LatestStateView.get)
    assert not inspect.iscoroutinefunction(LatestStateView.is_exhausted)


def _apollo_profile(lead_left):
    """api_profile's credit fields as Apollo returns them: the "unified" meter moves with phone
    reveals only, so the lead pool minus it says nothing about lead credits."""
    return {"num_credits_remaining": lead_left, "effective_num_lead_credits": 5000,
            "num_lead_credits_used": 0, "effective_num_direct_dial_credits": 5000,
            "num_direct_dial_credits_used": 40, "total_unified_credits_used": 40,
            "effective_num_ai_credits": 1000, "num_ai_credits_used": 0}


@pytest.mark.parametrize("lead_left,health", [(0, "exhausted"), (1234, "ok")])
async def test_sweep_reads_apollos_lead_credits_not_the_unified_meter(monkeypatch, lead_left, health):
    """Apollo answers every enrich with 422 `credit_type: lead credits, credit_balance: 0` while its
    unified meter still shows almost the whole pool unused. The sweep must read the lead pool, so a
    paid call is refused before reserve (or skips to overflow) instead of relaying into the 422."""
    await reset_db()
    monkeypatch.setenv("TREG_PLATFORM_KEY_APOLLO", "apollo-test-key")
    from treg.config import get_settings
    get_settings.cache_clear()

    def serve(request):
        assert request.url.path == "/api/v1/users/api_profile"
        assert request.url.params["include_credit_usage"] == "true"
        assert request.headers["x-api-key"] == "apollo-test-key"
        return httpx.Response(200, json=_apollo_profile(lead_left))

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
            async with session_maker() as db:
                result = await run_sweep(db, client=upstream, only={"apollo"})
    finally:
        get_settings.cache_clear()
    state = result.states["apollo"]
    assert state.remaining == lead_left and state.health == health
    assert "direct-dial 4960 left" in state.note


async def test_apollo_without_the_lead_field_is_no_reading():
    profile = _apollo_profile(None)
    del profile["num_credits_remaining"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=profile))) as upstream:
        row = await collectors._apollo(upstream, "k")
    assert row["value"] is None, "the unified meter is no stand-in: stale, never a guess"
