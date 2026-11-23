"""Default limits for an Org with no published snapshot; a publication replaces them whole."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from treg.config import Settings
from treg.infra import org_flow_control as flow
from test_marketplace_call import EP, platform_on  # noqa: F401
from test_org_concurrency import _gated, _upstream, gate, own_key  # noqa: F401
from test_org_flow_control_reader import env, payload  # noqa: F401

DEFAULT = flow.Snapshot(0, None, "", 2, flow.RateLimit(rps=5, burst=10))


def test_defaults_are_off_and_need_the_guard_and_a_complete_rate():
    settings = Settings(_env_file=None)
    assert flow.default_snapshot(settings) is None
    with pytest.raises(ValidationError, match="require org_flow_control_enabled"):
        Settings(_env_file=None, org_flow_control_default_max_in_flight=4)
    with pytest.raises(ValidationError, match="both rps and burst"):
        Settings(_env_file=None, org_flow_control_enabled=True, kv_url="redis://kv",
                 org_flow_control_default_rps=1)
    for bad in ({"org_flow_control_default_max_in_flight": 0},
                {"org_flow_control_default_rps": 0, "org_flow_control_default_burst": 1},
                {"org_flow_control_default_rps": float("inf"), "org_flow_control_default_burst": 1}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, org_flow_control_enabled=True, kv_url="redis://kv", **bad)
    settings = Settings(_env_file=None, org_flow_control_enabled=True, kv_url="redis://kv",
                        org_flow_control_default_max_in_flight=4,
                        org_flow_control_default_rps=2.5, org_flow_control_default_burst=5)
    snapshot = flow.default_snapshot(settings)
    assert (snapshot.version, snapshot.pause_until, snapshot.max_in_flight, snapshot.rate_limit) == (
        0, None, 4, flow.RateLimit(rps=2.5, burst=5))
    only_concurrency = Settings(_env_file=None, org_flow_control_enabled=True, kv_url="redis://kv",
                                org_flow_control_default_max_in_flight=4)
    assert flow.default_snapshot(only_concurrency).rate_limit is None


async def test_a_confirmed_missing_key_gets_the_default(env):
    reader, store, _, _ = env
    assert await reader.check(42, None, 2, DEFAULT) is DEFAULT
    assert await reader.check(42, None, 2) is None


async def test_a_published_snapshot_wins_whole_including_null_fields(env):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = payload(rate_limit={"rps": 1, "burst": 1})
    snapshot = await reader.check(42, None, 2, DEFAULT)
    assert snapshot.version == 1 and snapshot.max_in_flight is None
    assert snapshot.rate_limit == flow.RateLimit(rps=1, burst=1)
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(version=2)
    exempt = await reader.check(42, None, 2, DEFAULT)
    assert exempt.version == 2 and exempt.max_in_flight is None and exempt.rate_limit is None
    clock[0] += 3
    store.read_org_flow_control.return_value = None
    assert await reader.check(42, None, 2, DEFAULT) is DEFAULT


@pytest.mark.parametrize("bad", [flow.kv.FlowControlReadFailure.UNAVAILABLE,
                                 flow.kv.FlowControlReadFailure.INVALID, "invalid"])
async def test_unknown_state_allows_instead_of_applying_the_default(env, bad):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = bad
    assert await reader.check(42, None, 2, DEFAULT) is None
    clock[0] += 3
    store.read_org_flow_control.return_value = None
    assert await reader.check(42, None, 2, DEFAULT) is DEFAULT
    clock[0] += 3
    store.read_org_flow_control.return_value = bad
    assert await reader.check(42, None, 2, DEFAULT) is None


async def test_a_cache_full_worker_allows_without_the_default(env):
    reader, store, _, _ = env
    for org in (1, 2, 3):
        store.read_org_flow_control.return_value = payload(
            org_id=org, concurrency_limit={"max_in_flight": 1})
        await reader.check(org, None, 2, DEFAULT)
    store.read_org_flow_control.return_value = None
    assert await reader.check(42, None, 2, DEFAULT) is None


async def test_check_passes_the_configured_default(monkeypatch):
    reader = SimpleNamespace(check=AsyncMock(return_value=None))
    monkeypatch.setattr(flow, "_reader", reader)
    monkeypatch.setattr(flow, "get_settings", lambda: SimpleNamespace(
        org_flow_control_enabled=True, org_flow_control_refresh_s=2,
        org_flow_control_default_max_in_flight=3, org_flow_control_default_rps=None,
        org_flow_control_default_burst=None))
    await flow.check(42, "ref")
    (_, _, _, default), _ = reader.check.await_args
    assert default.version == 0 and default.max_in_flight == 3 and default.rate_limit is None


async def _hold_one_and_refuse_the_next(clients, monkeypatch, target):
    entered, proceed = asyncio.Event(), asyncio.Event()
    _upstream(monkeypatch, _gated(entered, proceed, AsyncMock()))
    first = asyncio.create_task(clients.get(target))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        return await clients.get(target)
    finally:
        proceed.set()
        assert (await first).status_code == 200


@pytest.mark.parametrize("road", ["treg_key", "own_key", "own_tool"])
async def test_the_default_bounds_an_unpublished_team_on_every_credential_tier(
    clients, platform_on, gate, monkeypatch, road,
):
    settings, store = gate
    monkeypatch.setattr(settings, "org_flow_control_default_max_in_flight", 1)
    target = f"/call/{EP}?aweme_id=1"
    if road == "own_key":
        await own_key(clients)
    elif road == "own_tool":
        sid = (await clients.post("/secrets", json={"name": "k", "value": "OWN-KEY"})).json()["id"]
        created = await clients.post("/tools", json={
            "name": "mine", "base_url": "https://api.example.com/v2", "secret_id": sid})
        assert created.status_code < 300, created.text
        target = "/call/mine/account"
    refused = await _hold_one_and_refuse_the_next(clients, monkeypatch, target)
    assert refused.status_code == 429, refused.text
    assert refused.headers["Retry-After"] == "1"
    assert refused.json()["detail"] == refused.json()["detail"] | {
        "error": "org_concurrency_limit", "config_version": 0}
    assert store.count == 0


async def test_a_published_null_limit_exempts_a_team_from_the_default(
    clients, platform_on, gate, monkeypatch,
):
    settings, store = gate
    monkeypatch.setattr(settings, "org_flow_control_default_max_in_flight", 1)
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    store.publish(org_id, None, version=3)
    entered, proceed = asyncio.Queue(), asyncio.Event()
    _upstream(monkeypatch, _gated(entered, proceed, AsyncMock()))
    calls = [asyncio.create_task(clients.get(f"/call/{EP}?aweme_id={n}")) for n in range(2)]
    try:
        for _ in calls:
            await asyncio.wait_for(entered.get(), 2)
    finally:
        proceed.set()
        results = await asyncio.wait_for(asyncio.gather(*calls), 5)
    assert [r.status_code for r in results] == [200, 200]
    assert store.acquires == 0 and not store.admissions
