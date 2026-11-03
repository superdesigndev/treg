"""Local admission failures do not spend endpoint call allowances."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from treg.application.call import service as call_service
from treg.application.call.types import AuthorizationFailed
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.infra import org_endpoint_quota
from treg.infra.upstream import health
from treg.models import Hold, LedgerEntry, Org, OverflowSpend

from test_capacity_overflow import O, OK_ANSWER, _exhausted, _orthogonal, _route, overflow_on  # noqa: F401
from test_marketplace_call import EP, _balance, _fake_relay, platform_on  # noqa: F401
from test_org_endpoint_quota_call import _enable
from test_route_verify import ASK, FIND, verifiers_on  # noqa: F401
from test_tag_billing import _set_budget


async def _org_id(clients):
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _empty_balance(clients):
    async with session_maker() as db:
        org = await db.get(Org, await _org_id(clients))
        org.balance_micro = 0
        await db.commit()


async def _assert_released(*, reserved=False):
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.kind.in_(["reserve", "release", "settle"])))).scalars().all()
        assert not [entry for entry in entries if entry.kind == "settle"]
        assert sum(entry.amount_micro for entry in entries) == 0
        assert bool([entry for entry in entries if entry.kind == "reserve"]) is reserved


async def test_insufficient_balance_does_not_consume_allowance(clients, monkeypatch, platform_on):
    await _empty_balance(clients)
    attempts, used = _enable(monkeypatch, {EP: 1})
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 402, response.text
    assert attempts == used == {}
    await _assert_released()


async def test_routed_parent_is_not_consumed_when_child_money_admission_fails(
    clients, monkeypatch, verifiers_on,
):
    await _empty_balance(clients)
    attempts, used = _enable(monkeypatch, {FIND: 1, "tomba.people.email.find": 1})
    response = await clients.post(f"/call/{FIND}", json=ASK)
    assert response.status_code == 402, response.text
    assert attempts == used == {}
    await _assert_released()


@pytest.mark.parametrize("cap", ["day", "tag"])
async def test_spend_cap_refusal_does_not_consume_allowance(clients, monkeypatch, platform_on, cap):
    org_id = await _org_id(clients)
    headers = {}
    if cap == "day":
        response = await clients.patch(f"/orgs/{org_id}/settings", json={"daily_cap_micro": 1})
        assert response.status_code == 200, response.text
    else:
        await _set_budget(clients, org_id, "customer", "synthetic", daily_cap_micro=1)
        headers = {"X-Treg-Meta": "customer=synthetic"}
    attempts, used = _enable(monkeypatch, {EP: 1})
    response = await clients.get(f"/call/{EP}?aweme_id=7", headers=headers)
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error"] == (
        "platform_daily_cap_reached" if cap == "day" else "tag_spend_cap_reached")
    assert attempts == used == {}
    await _assert_released()


async def test_public_resource_refusal_does_not_consume_allowance(clients, monkeypatch, platform_on):
    resolve = call_service.resolve_marketplace_target

    async def with_public_resource(*args, **kwargs):
        mk = await resolve(*args, **kwargs)
        mk.public_resource_ids = ("synthetic-resource",)
        return mk

    verify = AsyncMock(side_effect=AuthorizationFailed(
        "tool_access_denied", status_code=403, detail="Synthetic resource is private"))
    monkeypatch.setattr(call_service, "resolve_marketplace_target", with_public_resource)
    monkeypatch.setattr(call_service, "_verify_public_managed_resources", verify)
    attempts, used = _enable(monkeypatch, {EP: 1})
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 403, response.text
    verify.assert_awaited_once()
    assert attempts == used == {}
    await _assert_released()


@pytest.mark.parametrize("refusal", ["ssrf", "json_injection"])
async def test_relay_preparation_refusal_does_not_consume_allowance(
    clients, monkeypatch, platform_on, refusal,
):
    if refusal == "ssrf":
        monkeypatch.setattr(get_settings(), "proxy_ssrf_check", True)
        monkeypatch.setattr(health, "host_is_public", lambda _: False)
    else:
        resolve = call_service.resolve_marketplace_target

        async def with_json_binding(*args, **kwargs):
            mk = await resolve(*args, **kwargs)
            mk.tool.bindings = [dict(binding, location="json") for binding in mk.tool.bindings]
            return mk

        monkeypatch.setattr(call_service, "resolve_marketplace_target", with_json_binding)
    attempts, used = _enable(monkeypatch, {EP: 1})
    before = await _balance(clients)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 502, response.text
    assert attempts == used == {}
    assert await _balance(clients) == before
    await _assert_released(reserved=True)


@pytest.mark.parametrize("refusal", ["budget", "balance"])
async def test_skip_direct_local_admission_does_not_consume_allowance(
    clients, monkeypatch, overflow_on, refusal,
):
    await _route(price_micro=3000)
    await _exhausted()
    if refusal == "budget":
        monkeypatch.setattr(get_settings(), "overflow_daily_budget_usd", 0.000001)
    else:
        await _empty_balance(clients)
    attempts, used = _enable(monkeypatch, {EP: 1})
    outbound = AsyncMock(side_effect=AssertionError("local refusal cannot call the aggregator"))
    monkeypatch.setattr(O, "_send", outbound)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == (503 if refusal == "budget" else 402), response.text
    assert attempts == used == {}
    outbound.assert_not_called()
    await _assert_released()
    async with session_maker() as db:
        assert all(row.cost_micro == 0 for row in (await db.execute(select(OverflowSpend))).scalars())


@pytest.mark.parametrize("skip_direct", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
async def test_direct_or_overflow_holds_follow_exhaustion_and_unavailability(
    clients, monkeypatch, overflow_on, skip_direct, unavailable,
):
    if skip_direct:
        await _route(price_micro=3000)
        await _exhausted()
    attempts, used = _enable(monkeypatch, {EP: 0}, unavailable=unavailable)
    refusal = AssertionError("quota refusal cannot make a provider call")
    outbound = AsyncMock(side_effect=_fake_relay(200) if unavailable else refusal)
    overflow_outbound = AsyncMock(side_effect=_orthogonal([OK_ANSWER], []) if unavailable else refusal)

    async def relay(*args, before_outbound=None, **kwargs):
        if before_outbound is not None:
            await before_outbound()
        return await outbound(*args, **kwargs)

    monkeypatch.setattr(call_service, "relay", relay)
    monkeypatch.setattr(O, "_send", overflow_outbound)
    before = await _balance(clients)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == (200 if unavailable else 429), response.text
    charged = int(response.headers["X-Treg-Cost-Micro"])
    assert attempts == {EP: 1} and used[EP] == 0
    assert before - await _balance(clients) == charged
    if unavailable:
        assert charged > 0
        (overflow_outbound if skip_direct else outbound).assert_awaited_once()
        async with session_maker() as db:
            assert (await db.execute(select(Hold))).scalars().all() == []
    else:
        assert charged == 0
        outbound.assert_not_called()
        overflow_outbound.assert_not_called()
        await _assert_released(reserved=True)
        async with session_maker() as db:
            assert all(row.cost_micro == 0 for row in (await db.execute(select(OverflowSpend))).scalars())


@pytest.mark.parametrize("skip_direct", [False, True])
async def test_cancellation_during_quota_releases_existing_hold_and_overflow_budget(
    clients, monkeypatch, overflow_on, skip_direct,
):
    if skip_direct:
        await _route(price_micro=3000)
        await _exhausted()
    _enable(monkeypatch, {EP: 1})
    monkeypatch.setattr(org_endpoint_quota, "admit", AsyncMock(side_effect=asyncio.CancelledError))
    before = await _balance(clients)
    with pytest.raises(asyncio.CancelledError):
        await clients.get(f"/call/{EP}?aweme_id=7")
    assert await _balance(clients) == before
    await _assert_released(reserved=True)
    async with session_maker() as db:
        assert all(row.cost_micro == 0 for row in (await db.execute(select(OverflowSpend))).scalars())


async def test_cancel_during_overflow_quota_refusal_cleanup_still_releases_budget(
    clients, monkeypatch, overflow_on,
):
    await _route(price_micro=3000)
    await _exhausted()
    _enable(monkeypatch, {EP: 0})
    settle = O._platform_settle

    async def cancel_after_release(*args, **kwargs):
        result = await settle(*args, **kwargs)
        if kwargs.get("reason") == "overflow_admission_refused":
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(O, "_platform_settle", cancel_after_release)
    before = await _balance(clients)
    with pytest.raises(asyncio.CancelledError):
        await clients.get(f"/call/{EP}?aweme_id=7")
    assert await _balance(clients) == before
    await _assert_released(reserved=True)
    async with session_maker() as db:
        assert all(row.cost_micro == 0 for row in (await db.execute(select(OverflowSpend))).scalars())


@pytest.mark.parametrize("answer,status,charged,closing_kind", [
    (OK_ANSWER, 200, 3000, "settle"),
    ((402, {"success": False, "error": "insufficient balance"}), 503, 0, "release"),
    ((400, {"success": False, "error": "x", "_orthogonal": {
        "error": "orthogonal_endpoint_contract", "message": "missing"}}), 503, 0, "release"),
], ids=["served", "aggregator-refused", "contract-refused"])
async def test_completed_overflow_does_not_reenter_quota_refusal_cleanup(
    clients, monkeypatch, overflow_on, answer, status, charged, closing_kind,
):
    await _route(price_micro=3000)
    await _exhausted()
    attempts, used = _enable(monkeypatch, {EP: 1})
    settle = AsyncMock(wraps=O._platform_settle)
    monkeypatch.setattr(O, "_platform_settle", settle)
    monkeypatch.setattr(O, "_send", _orthogonal([answer], []))
    before = await _balance(clients)

    response = await clients.get(f"/call/{EP}?aweme_id=7")

    assert response.status_code == status, response.text
    assert before - await _balance(clients) == charged
    assert attempts == used == {EP: 1}
    settle.assert_awaited_once()
    assert settle.await_args.kwargs.get("reason") != "overflow_admission_refused"
    assert settle.await_args.args[0].call_id is None
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.kind.in_(["reserve", "release", "settle"])))).scalars().all()
    assert sorted(entry.kind for entry in entries) == sorted(["reserve", closing_kind])
