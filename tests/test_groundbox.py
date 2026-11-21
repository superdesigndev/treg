"""Groundbox sandboxes: a run is create -> exec -> delete, priced at create for the whole timeout."""
from __future__ import annotations

import json

import httpx
import pytest

from treg.application.call import cost
from treg.application.call.resolve import _enforce_strict_body
from treg.application.call.types import ResolutionFailed
from treg.domain.capacity import collectors
from treg.domain.catalog import store
from treg.domain.money import settlement


@pytest.fixture(scope="module")
def catalog():
    return store.load()


def _rows(catalog) -> list[dict]:
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "groundbox"]
    assert rows
    return rows


def _hold(catalog, body: dict) -> int:
    ep = catalog.by_id["groundbox.sandbox.create"]
    request = settlement.request_evidence([], json.dumps(body).encode())
    return cost.price(ep, catalog, request)[0]


def test_every_groundbox_row_is_platform_eligible(catalog):
    ineligible = [ep["id"] for ep in _rows(catalog) if not catalog.platform_eligible(ep)]
    assert not ineligible, ineligible


def test_a_sandbox_is_held_for_its_vcpus_times_its_whole_timeout(catalog):
    # $0.04 per vCPU-hour: 1 vCPU for 60 minutes is 40,000 micro-USD.
    assert _hold(catalog, {"cpus": 1, "timeout_minutes": 60}) == 40_000
    assert _hold(catalog, {"cpus": 4, "timeout_minutes": 15}) == 40_000
    # cpus defaults to 2, as Groundbox's own default does.
    assert _hold(catalog, {"timeout_minutes": 30}) == 40_000
    assert _hold(catalog, {"cpus": 16, "timeout_minutes": 60}) == 640_000
    assert _hold(catalog, {"cpus": 2, "timeout_minutes": 1}) == pytest.approx(1_334, abs=1)


@pytest.mark.parametrize("body,parameter", [
    ({"cpus": 1}, "body.timeout_minutes"),                                  # the timeout is the price
    ({"cpus": 1, "timeout_minutes": 61}, "body.timeout_minutes"),           # past the priced hour
    ({"cpus": 17, "timeout_minutes": 5}, "body.cpus"),
    ({"cpus": 1, "timeout_minutes": 5, "on_timeout": "stop"}, "body.on_timeout"),   # keeps the disk
    ({"cpus": 1, "timeout_minutes": 5, "idle_timeout_seconds": 60}, "body.idle_timeout_seconds"),
    ({"cpus": 1, "timeout_minutes": 5, "volumes": [{"id": "vol_x"}]}, "body.volumes"),
])
def test_the_create_body_asks_only_for_what_the_price_covers(catalog, body, parameter):
    ep = catalog.by_id["groundbox.sandbox.create"]
    with pytest.raises(ResolutionFailed) as refused:
        _enforce_strict_body(ep, json.dumps(body).encode())
    assert refused.value.status_code == 400
    assert parameter in json.dumps(refused.value.__dict__, default=str)


def test_a_declared_create_body_passes(catalog):
    ep = catalog.by_id["groundbox.sandbox.create"]
    _enforce_strict_body(ep, json.dumps({
        "cpus": 2, "memory_gb": 4, "timeout_minutes": 5, "image": "python:3.12-slim",
        "env": {"MODE": "test"}, "allow_hosts": ["pypi.org"], "metadata": {"project": "demo"},
    }).encode())


def test_only_the_team_that_created_a_sandbox_can_reach_it_on_treg_s_key(catalog):
    create = catalog.by_id["groundbox.sandbox.create"]
    assert create["resource_ownership"]["produces"] == [{"kind": "sandbox", "path": "id"}]
    for ep in _rows(catalog):
        if "id" in ((ep.get("input") or {}).get("pathParams") or {}):
            assert ep["resource_ownership"]["requires"] == {"kind": "sandbox", "param": "id"}, ep["id"]


def test_only_create_is_charged(catalog):
    for ep in _rows(catalog):
        usd = catalog.cost_view(ep["cost"], "groundbox")["usd"]
        assert (usd > 0) is (ep["id"] == "groundbox.sandbox.create"), ep["id"]


def test_answers_are_never_archived_or_replayed(catalog):
    for ep in _rows(catalog):
        assert ep.get("cache") == "forbidden", ep["id"]


async def test_groundbox_balance_reads_the_account_in_micro_dollars() -> None:
    def probe(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == "https://api.groundbox.dev/v1/account"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"balance": {
            "total_micros": 12_500_000, "plan_micros": 10_000_000, "wallet_micros": 2_500_000}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._groundbox(client, "test-key")
    assert row["value"] == 12.5 and row["unit"] == "USD"
    assert collectors.BALANCE_ROUTES["groundbox"] is collectors._groundbox


@pytest.mark.parametrize("payload", [
    {"balance": {"total_micros": -1}},
    {"balance": {"total_micros": "100"}},
    {"balance": {"total_micros": 1.5}},
    {"balance": None},
    {"error": {"code": "missing_scope"}},
])
async def test_groundbox_balance_refuses_a_missing_or_malformed_figure(payload) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ValueError, match="no valid balance"):
            await collectors._groundbox(client, "test-key")
