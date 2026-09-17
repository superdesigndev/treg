"""The per-team daily allowance on a catalog endpoint served on treg's key.

Money brakes nothing at $0, so `cost.calls_per_team_day` (else the deployment default) is the
only thing between one looping client and the shared vendor key's whole quota; a paid route may
declare the same figure for a vendor whose per-key daily quota one team could spend for everyone.
These tests pin the gate: it bites at the declared number, counts attempts, is per team and per
UTC day, exact under concurrency, fail-closed, visible in the catalog, applies to a paid route only
when declared, and is never in the way of a team's own key.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from httpx import AsyncClient

from treg import audit
from treg.application.call import reserve as call_reserve
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.governance import allowance as allowance_policy
from treg.infra.db import session_maker
from treg.models import EndpointAllowance
from treg.timeutil import utcnow_naive


@pytest.fixture
def platform_on(monkeypatch):
    """Tier 4 for the paid route: treg's TikHub key in the environment and the provider allow-listed."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATFORM-TIKHUB-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

EP = "contactout.people.work_email.available"     # $0 on treg's key, allowance declared in YAML
PARAMS = {"profile": "https://www.linkedin.com/in/someone"}


def _relay(status: int = 200, seen: list | None = None):
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        if seen is not None:
            seen.append(upstream_url)

        async def stream():
            yield b'{"status_code":200,"profile":{"work_email_status":"found"}}'

        async def close():
            pass

        return UpstreamResponse(status, (), stream(), close)
    return relay


@pytest.fixture
def allowance(monkeypatch):
    """Set the endpoint's own figure for one test; the catalog is a process-wide singleton."""
    cost = catalog_store.load().by_id[EP]["cost"]

    def _set(n: int | None) -> None:
        if n is None:
            monkeypatch.delitem(cost, "calls_per_team_day", raising=False)
        else:
            monkeypatch.setitem(cost, "calls_per_team_day", n)
    return _set


async def _balance(clients: AsyncClient) -> int:
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org}/balance")).json()["balance_micro"]


async def _newest_call(clients: AsyncClient) -> dict:
    await audit.drain()
    return (await clients.get("/calls")).json()[0]


async def test_the_allowance_bites_at_the_declared_number_unbilled_and_before_the_vendor(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(3)
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(200, seen))
    before = await _balance(clients)
    for _ in range(3):
        assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200
    r = await clients.get(f"/call/{EP}", params=PARAMS)
    assert r.status_code == 429, r.text
    d = r.json()["detail"]
    assert d["error"] == "endpoint_allowance_reached"
    assert (d["allowance_per_day"], d["used_today"], d["provider"], d["endpoint_id"]) == (3, 3, "contactout", EP)
    assert d["resets_at"].endswith("T00:00:00Z") and "treg connections connect" in d["message"]
    assert len(seen) == 3, "the refused call must never reach the vendor"
    assert await _balance(clients) == before, "a $0 endpoint moves no money, refused or not"
    row = await _newest_call(clients)
    assert (row["status_code"], row["refused_by"]) == (429, "cap"), "a product refusal, not a platform failure"


async def test_failed_attempts_consume_the_allowance_because_the_vendor_counts_attempts(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(2)
    monkeypatch.setattr(call_service, "relay", _relay(500))
    for _ in range(2):
        assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 500
    r = await clients.get(f"/call/{EP}", params=PARAMS)
    assert r.status_code == 429 and r.json()["detail"]["error"] == "endpoint_allowance_reached"


async def test_a_refused_call_writes_nothing_so_a_hammering_client_reads_exactly_the_allowance(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(2)
    monkeypatch.setattr(call_service, "relay", _relay())
    for _ in range(6):
        await clients.get(f"/call/{EP}", params=PARAMS)
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        assert await allowance_policy.used_today(db, org, EP) == 2


async def test_the_gate_is_exact_under_concurrency(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    """Twenty calls in flight at once against an allowance of five admit exactly five: the WHERE
    is the check and the SET is the count, so no two callers can both read a compliant figure."""
    allowance(5)
    monkeypatch.setattr(call_service, "relay", _relay())
    results = await asyncio.gather(*(clients.get(f"/call/{EP}", params=PARAMS) for _ in range(20)))
    codes = sorted(r.status_code for r in results)
    assert codes == [200] * 5 + [429] * 15


async def test_another_orgs_usage_never_burns_MY_allowance(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(1)
    other = await clients.post("/orgs", json={"name": "another-team"})
    assert other.status_code == 200, other.text
    async with session_maker() as db:
        db.add(EndpointAllowance(org_id=other.json()["org_id"], endpoint_id=EP,
                             day=allowance_policy.utc_day(), used=1))
        await db.commit()
    monkeypatch.setattr(call_service, "relay", _relay())
    assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200


async def test_yesterdays_usage_does_not_count_today(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(1)
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    yesterday = (utcnow_naive() - timedelta(days=1)).strftime("%Y-%m-%d")
    async with session_maker() as db:
        db.add(EndpointAllowance(org_id=org, endpoint_id=EP, day=yesterday, used=1))
        await db.commit()
    monkeypatch.setattr(call_service, "relay", _relay())
    assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200
    assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 429


async def test_the_allowance_is_per_endpoint_not_per_provider(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    """A vendor's allowances are per operation; so is ours. Exhausting one free route leaves the
    provider's other free route untouched."""
    allowance(1)
    monkeypatch.setattr(call_service, "relay", _relay())
    assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200
    assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 429
    sibling = "contactout.people.personal_email.available"
    assert (await clients.get(f"/call/{sibling}", params=PARAMS)).status_code == 200


async def test_the_deployment_default_covers_an_endpoint_that_declares_nothing(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(None)
    monkeypatch.setenv("TREG_FREE_ALLOWANCE_PER_TEAM_DAY", "1")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(call_service, "relay", _relay())
        assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200
        r = await clients.get(f"/call/{EP}", params=PARAMS)
        assert r.status_code == 429 and r.json()["detail"]["allowance_per_day"] == 1
    finally:
        get_settings.cache_clear()


async def test_a_zero_default_means_no_allowance_where_none_is_declared(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(None)
    monkeypatch.setenv("TREG_FREE_ALLOWANCE_PER_TEAM_DAY", "0")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(call_service, "relay", _relay())
        for _ in range(3):
            assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200
    finally:
        get_settings.cache_clear()


async def test_an_endpoints_own_figure_wins_over_the_default(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(2)
    monkeypatch.setenv("TREG_FREE_ALLOWANCE_PER_TEAM_DAY", "1")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(call_service, "relay", _relay())
        assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200
        assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200
        assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 429
    finally:
        get_settings.cache_clear()


async def test_a_teams_own_key_never_meets_the_allowance(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    """Non-negotiable 1: the team's own key wins and is never metered, so no shared-key brake
    applies to it — even one already exhausted on treg's key."""
    allowance(1)
    org = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        db.add(EndpointAllowance(org_id=org, endpoint_id=EP, day=allowance_policy.utc_day(), used=1))
        await db.commit()
    assert (await clients.post("/secrets", json={"name": "contactout", "value": "OWN-TEST"})).status_code == 200
    monkeypatch.setattr(call_service, "relay", _relay())
    for _ in range(3):
        assert (await clients.get(f"/call/{EP}", params=PARAMS)).status_code == 200
    async with session_maker() as db:
        assert await allowance_policy.used_today(db, org, EP) == 1, "own-key calls are not counted"


async def test_fail_closed_when_the_slot_cannot_be_taken(
        clients: AsyncClient, contactout_platform, monkeypatch, allowance):
    allowance(5)
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(200, seen))

    async def broken(*args, **kwargs):
        raise RuntimeError("database away")
    monkeypatch.setattr(call_reserve.allowance_policy, "take_endpoint_allowance_slot", broken)
    r = await clients.get(f"/call/{EP}", params=PARAMS)
    assert r.status_code == 429 and "retry shortly" in r.json()["detail"]
    assert seen == [], "serving blind is how the shared pool dies"


PAID = "tikhub.tiktok.video.comments"     # per_success on treg's key: money is its brake


async def test_a_paid_endpoint_has_no_allowance_unless_it_declares_one(
        clients: AsyncClient, platform_on, monkeypatch):
    """The default belongs to $0 prices only: the hold, the balance and the daily cap already
    bound a paid route, so the deployment default must not touch it."""
    monkeypatch.setenv("TREG_FREE_ALLOWANCE_PER_TEAM_DAY", "1")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(call_service, "relay", _relay())
        for _ in range(3):
            assert (await clients.get(f"/call/{PAID}", params={"aweme_id": "7"})).status_code == 200
    finally:
        get_settings.cache_clear()


async def test_a_paid_endpoint_that_declares_a_figure_is_gated_the_same_way(
        clients: AsyncClient, platform_on, monkeypatch):
    """The mechanism is not about zeros: a paid route on a vendor whose per-key daily quota one
    team could spend for everyone declares `calls_per_team_day` and gets the same gate, before
    any hold."""
    monkeypatch.setitem(catalog_store.load().by_id[PAID]["cost"], "calls_per_team_day", 2)
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(200, seen))
    before = await _balance(clients)
    assert (await clients.get(f"/call/{PAID}", params={"aweme_id": "7"})).status_code == 200
    assert (await clients.get(f"/call/{PAID}", params={"aweme_id": "7"})).status_code == 200
    r = await clients.get(f"/call/{PAID}", params={"aweme_id": "7"})
    assert r.status_code == 429 and r.json()["detail"]["error"] == "endpoint_allowance_reached"
    assert len(seen) == 2
    assert await _balance(clients) == before - 2 * 1_000, "two paid calls settled, the refused one held nothing"


def test_the_allowance_travels_with_the_zero_in_the_catalog(monkeypatch):
    """A bare $0.00 reads as unlimited, so every surface that shows the price also shows the
    allowance: the endpoint's own figure, else the deployment default, never for a trial pool
    (which carries its own field) and never on a paid price."""
    cat = catalog_store.load()
    ep = cat.by_id[EP]
    assert cat.cost_view(ep["cost"], "contactout")["calls_per_team_day"] == 1000
    cost_without = {k: v for k, v in ep["cost"].items() if k != "calls_per_team_day"}
    assert cat.cost_view(cost_without, "contactout")["calls_per_team_day"] == \
        get_settings().free_allowance_per_team_day
    trial = cat.cost_view(cat.by_id["finnhub.quote"]["cost"], "finnhub")
    assert "trial_calls_per_team_day" in trial and "calls_per_team_day" not in trial
    paid = {"type": "per_call", "value": 0.01, "currency": "USD", "per": 1, "unit": "call"}
    assert "calls_per_team_day" not in cat.cost_view(paid, "contactout"), "no default on a paid price"
    assert cat.cost_view(paid | {"calls_per_team_day": 500}, "contactout")["calls_per_team_day"] == 500


def test_the_validator_keeps_the_field_honest():
    from scripts.catalog_validate import check_cost
    free = {"type": "free", "value": 0, "currency": "USD", "per": 1, "unit": "call"}
    for bad, why in [
        ({**free, "calls_per_team_day": 0}, "zero"),
        ({**free, "calls_per_team_day": -5}, "negative"),
        ({**free, "calls_per_team_day": "1000"}, "a string"),
        ({**free, "calls_per_team_day": True}, "a boolean"),
    ]:
        errors: list[str] = []
        check_cost(bad, "test", errors, [])
        assert any("calls_per_team_day" in e for e in errors), why
    paid = {"type": "per_call", "value": 0.01, "currency": "USD", "per": 1, "unit": "call",
            "confidence": "documented", "source": "docs", "source_url": "https://x.example",
            "checked": "2026-09-17"}
    for fine in ({**free, "calls_per_team_day": 1000}, {**paid, "calls_per_team_day": 100}):
        errors = []
        check_cost(fine, "test", errors, [])
        assert errors == [], "a positive figure is honest on any price"
