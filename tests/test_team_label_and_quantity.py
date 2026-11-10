"""Shared-key objects that outlive a call: a per-team label and a price the caller chooses.

A provider can keep work running after a call (a page monitor, a schedule). On treg's one shared
account, `team_label` keeps each team's objects under its own label, and `cost.quantity` lets a
team buy a budget of a size it names, charged exactly once, settled at what was bought.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from treg.application.call import resolve as call_resolution
from treg.application.call import service as call_service
from treg.application.call.types import ResolutionFailed, UpstreamResponse
from treg.config import get_settings
from treg.domain import money as ledger
from treg.domain.catalog import store as catalog_store

EP = "crawl4ai.test.budget"
COST = {"type": "per_success", "value": 1, "currency": "credit", "per": 1, "unit": "credit",
        "quantity": {"field": "body.credits", "max": 100_000, "applied": "added"}, "settle": "quantity",
        "source": "docs", "confidence": "documented", "checked": "2026-10-19"}


@pytest.fixture
def budget_row(monkeypatch):
    """A budget row on Crawl4AI's shared key: the label rides the path, the quantity the body."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "PLATFORM-C4-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai")
    get_settings.cache_clear()
    cat = catalog_store.load()
    base = next(e for e in cat.by_id.values()
                if e.get("provider") == "crawl4ai" and cat.platform_eligible(e))
    ep = {**base, "id": EP, "method": "POST", "path": "/monitors/agents/{label}/budget",
          "input": {"pathParams": {"label": {"type": "string", "required": True}},
                    "body": {"credits": {"type": "integer", "required": True},
                             "ref": {"type": "string", "required": True}}},
          "team_label": ["pathParams.label"], "cost": COST, "platform_request": None}
    monkeypatch.setitem(cat.by_id, EP, ep)
    yield ep
    get_settings.cache_clear()


def _relay(status: int, seen: list, headers: tuple = ((b"x-c4-cost", b"0"),), added: bool = True):
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        seen.append(upstream_url)

        async def stream():
            yield json.dumps({"credit_limit": 1000, "spent": 0, "state": "running",
                              "added": added}).encode()

        async def close():
            return None

        return UpstreamResponse(status, headers, stream(), close)
    return relay


async def _team(clients: AsyncClient) -> tuple[int, int]:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return org_id, (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


def test_a_team_label_is_stable_and_one_per_team():
    assert call_resolution.team_label(7) == call_resolution.team_label(7)
    assert call_resolution.team_label(7) != call_resolution.team_label(8)
    assert call_resolution.team_label(7).startswith("team-") and len(call_resolution.team_label(7)) == 21


async def test_a_budget_is_charged_exactly_once_even_when_the_provider_reports_zero(
    clients: AsyncClient, budget_row, monkeypatch,
):
    """The provider adds room for free and says `x-c4-cost: 0`; the team still pays for the
    credits it bought, exactly, and the call reaches the provider under its own label."""
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(200, seen))
    org_id, before = await _team(clients)
    label = call_resolution.team_label(org_id)
    r = await clients.post(f"/call/{EP}?label={label}", json={"credits": 1000, "ref": "p1"})
    assert r.status_code == 200, r.text
    cv = catalog_store.load().cost_view(COST, "crawl4ai")
    expected = ledger.with_margin(call_resolution._platform_estimate_micro(
        cv, None, json.dumps({"credits": 1000, "ref": "p1"}).encode()))
    assert expected > 0
    assert before - (await _team(clients))[1] == expected
    assert seen and seen[0].endswith(f"/monitors/agents/{label}/budget")


@pytest.mark.parametrize("label_of, body, why", [
    (lambda me: call_resolution.team_label(me + 1), {"credits": 1000}, "another team's label"),
    (lambda me: "", {"credits": 1000}, "no label"),
    (lambda me: call_resolution.team_label(me), {}, "no quantity"),
    (lambda me: call_resolution.team_label(me), {"credits": 0}, "a zero quantity"),
    (lambda me: call_resolution.team_label(me), {"credits": 100_001}, "past the maximum"),
    (lambda me: call_resolution.team_label(me), {"credits": 10.5}, "not a whole number"),
    (lambda me: call_resolution.team_label(me), {"credits": "9" * 5000}, "thousands of digits"),
])
async def test_a_wrong_label_or_quantity_is_refused_before_any_money_moves(
    clients: AsyncClient, budget_row, monkeypatch, label_of, body, why,
):
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(200, seen))
    org_id, before = await _team(clients)
    label = label_of(org_id)
    r = await clients.post(f"/call/{EP}" + (f"?label={label}" if label else ""), json=body)
    assert r.status_code == 400, f"{why}: {r.text}"
    assert seen == [], f"{why} must not reach the provider"
    assert (await _team(clients))[1] == before, f"{why} must not charge"


async def test_a_failed_purchase_is_free(clients: AsyncClient, budget_row, monkeypatch):
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(503, seen))
    org_id, before = await _team(clients)
    r = await clients.post(f"/call/{EP}?label={call_resolution.team_label(org_id)}",
                           json={"credits": 1000, "ref": "p1"})
    assert r.status_code == 503
    assert (await _team(clients))[1] == before


def test_label_checks_cover_body_query_and_header_and_name_the_expected_value():
    ep = {"id": "p.x", "team_label": ["body.agent", "queryParams.agent", "headers.x-agent"]}
    me = call_resolution.team_label(3)

    class Q:
        def __init__(self, items):
            self.items = items

        def multi_items(self):
            return self.items

    good = (json.dumps({"agent": me}).encode(), {"x-agent": me}, Q([("agent", me)]))
    call_resolution._enforce_team_label(ep, 3, *good)
    with pytest.raises(ResolutionFailed) as exc:
        call_resolution._enforce_team_label(ep, 3, good[0], good[1], Q([("agent", me), ("agent", me)]))
    assert exc.value.detail["expected"] == me and exc.value.detail["parameter"] == "queryParams.agent"


async def test_a_retry_the_provider_recognised_adds_nothing_and_charges_nothing(
    clients: AsyncClient, budget_row, monkeypatch,
):
    """Same purchase id again: the provider adds no room (`added: false`), so treg charges 0."""
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(200, seen, added=False))
    org_id, before = await _team(clients)
    r = await clients.post(f"/call/{EP}?label={call_resolution.team_label(org_id)}",
                           json={"credits": 1000, "ref": "p1"})
    assert r.status_code == 200, r.text
    assert seen and (await _team(clients))[1] == before


def test_an_alert_email_may_only_be_the_callers_own():
    """`caller_email`: absent is fine, the caller's own address passes (any case), anyone else's
    is refused with the caller's own address named; nested body paths are read."""
    ep = {"id": "p.watch", "caller_email": ["body.notify.email"]}
    own = "tim@superdesign.dev"
    call_resolution._enforce_caller_email(ep, own, json.dumps({"url": "https://x.test"}).encode())
    call_resolution._enforce_caller_email(ep, own, json.dumps({"notify": {"email": "Tim@Superdesign.dev"}}).encode())
    call_resolution._enforce_caller_email(ep, own, json.dumps({"notify": {"webhook": "https://hook.test"}}).encode())
    with pytest.raises(ResolutionFailed) as exc:
        call_resolution._enforce_caller_email(ep, own, json.dumps({"notify": {"email": "stranger@example.com"}}).encode())
    assert exc.value.status_code == 400 and exc.value.detail["expected"] == own


async def test_a_strangers_alert_email_never_reaches_the_provider(
    clients: AsyncClient, budget_row, monkeypatch,
):
    """End to end on treg's key: another address is refused before any call; the caller's own goes."""
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(200, seen))
    cat = catalog_store.load()
    ep = {**budget_row, "id": "crawl4ai.test.watch", "path": "/monitors", "team_label": ["body.agent"],
          "caller_email": ["body.notify.email"], "cost": {"type": "free", "value": 0, "currency": "USD", "unit": "call"},
          "input": {"body": {"agent": {"type": "string", "required": True},
                             "notify": {"type": "object", "properties": {"email": {"type": "string"}}}}}}
    monkeypatch.setitem(cat.by_id, "crawl4ai.test.watch", ep)
    org_id, _ = await _team(clients)
    label = call_resolution.team_label(org_id)
    r = await clients.post("/call/crawl4ai.test.watch", json={"agent": label, "notify": {"email": "stranger@example.com"}})
    assert r.status_code == 400 and seen == []
    r = await clients.post("/call/crawl4ai.test.watch", json={"agent": label, "notify": {"email": "tim@superdesign.dev"}})
    assert r.status_code == 200, r.text
    assert len(seen) == 1


async def test_a_purchase_cancelled_after_it_was_sent_is_charged_and_its_retry_is_free(
    clients: AsyncClient, budget_row, monkeypatch,
):
    """The caller drops the call after treg sent it: the provider may have added the credits, so
    the hold is charged instead of returned. The retry with the same ref (`added: false`) is free,
    so the team pays exactly once."""
    import asyncio

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(call_service, "relay", cancelled)
    org_id, before = await _team(clients)
    label = call_resolution.team_label(org_id)
    with pytest.raises(asyncio.CancelledError):
        await clients.post(f"/call/{EP}?label={label}", json={"credits": 100, "ref": "p9"})
    cv = catalog_store.load().cost_view(COST, "crawl4ai")
    expected = ledger.with_margin(call_resolution._platform_estimate_micro(
        cv, None, json.dumps({"credits": 100, "ref": "p9"}).encode()))
    after_cancel = (await _team(clients))[1]
    assert before - after_cancel == expected
    seen: list = []
    monkeypatch.setattr(call_service, "relay", _relay(200, seen, added=False))
    r = await clients.post(f"/call/{EP}?label={label}", json={"credits": 100, "ref": "p9"})
    assert r.status_code == 200 and (await _team(clients))[1] == after_cancel


async def test_an_ordinary_call_cancelled_mid_relay_is_still_refunded(clients: AsyncClient, budget_row, monkeypatch):
    """Only a bought quantity is charged on cancel; every other row keeps the full refund."""
    import asyncio

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    cat = catalog_store.load()
    plain = {**budget_row, "cost": {**COST, "settle": None, "quantity": None, "type": "per_success"}}
    plain["cost"] = {k: v for k, v in plain["cost"].items() if v is not None}
    monkeypatch.setitem(cat.by_id, EP, plain)
    monkeypatch.setattr(call_service, "relay", cancelled)
    org_id, before = await _team(clients)
    with pytest.raises(asyncio.CancelledError):
        await clients.post(f"/call/{EP}?label={call_resolution.team_label(org_id)}", json={"credits": 1000, "ref": "p8"})
    assert (await _team(clients))[1] == before
async def test_tool_details_tell_a_signed_in_team_its_own_values(clients: AsyncClient, budget_row, monkeypatch):
    """catalog detail adds `your_values` for a row's team_label and caller_email fields when the
    reader is a signed-in team, and nothing for an anonymous reader."""
    cat = catalog_store.load()
    monkeypatch.setitem(cat.by_id, EP, {**budget_row, "caller_email": ["body.ref"]})
    org_id, _ = await _team(clients)
    r = await clients.get(f"/catalog/endpoints/{EP}")
    assert r.headers["cache-control"] == "private, no-store" and "X-Treg-Token" in r.headers["vary"]
    body = r.json()
    assert body["endpoint"]["your_values"] == {"pathParams.label": call_resolution.team_label(org_id),
                                               "body.ref": "tim@superdesign.dev"}
    anonymous = (await clients.get(f"/catalog/endpoints/{EP}", headers={"X-Treg-Token": ""})).json()
    assert "your_values" not in anonymous["endpoint"]
