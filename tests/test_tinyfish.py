"""TinyFish catalog, per-step settlement, and wallet-capacity integration."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from sqlmodel import select

from treg import oauth_providers
from treg.application import asynctasks as task_app
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import Settings, get_settings
from treg.domain.capacity import collectors, policy
from treg.domain.catalog import store
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord
from treg.timeutil import utcnow_naive


def _response(status: int, document: object) -> UpstreamResponse:
    body = json.dumps(document).encode()

    async def stream():
        yield body

    async def close():
        return None

    return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)


@pytest.fixture
def tinyfish_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TINYFISH", "test-platform-tinyfish")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tinyfish")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_tinyfish_registry_and_catalog_are_byok_and_platform_ready(tinyfish_platform):
    provider = oauth_providers.get("tinyfish")
    assert provider is not None
    assert provider.auth_kind == "key"
    assert provider.base_url == "https://agent.tinyfish.ai"
    assert provider.probe_path == "/v1/wallet"
    assert {target.host for target in provider.catalog_targets} == {
        "api.search.tinyfish.ai", "api.fetch.tinyfish.ai",
    }
    assert oauth_providers.platform_bindings(provider) == [{
        "platform_setting": "platform_key_tinyfish",
        "injector": "env",
        "location": "header",
        "name": "X-API-Key",
        "format": "{secret}",
    }]
    assert Settings(_env_file=None).platform_key_for("tinyfish") == "test-platform-tinyfish"

    catalog = store.load()
    rows = [row for row in catalog.endpoints if row["provider"] == "tinyfish"]
    assert len(rows) == 9
    assert all(catalog.platform_eligible(row) for row in rows)
    assert not any("batch" in row["id"] or "monitor" in row["id"] for row in rows)
    assert not any(row.get("kind") == "account" for row in rows)

    agent = catalog.by_id["tinyfish.web.agent.run"]
    shown = catalog.cost_view(agent["cost"], "tinyfish")
    assert (shown["rate_usd_min"], shown["rate_usd"], shown["rate_unit"]) == (
        0.016, 0.016, "step",
    )
    assert shown["usd_min"] == 0.016 and shown["usd"] == 8.0
    assert agent["async"]["status"] == {
        "path": "status", "progress": ["PENDING", "RUNNING"],
        "success": ["COMPLETED"], "failure": [],
        "billed_failure": ["FAILED", "CANCELLED"],
    }


async def test_tinyfish_agent_price_is_exposed_as_per_step_to_clients(clients):
    response = await clients.get("/catalog/endpoints/tinyfish.web.agent.run")
    assert response.status_code == 200
    endpoint = response.json()["endpoint"]
    assert endpoint["platform_eligible"] is True
    assert endpoint["cost"]["rate_usd_min"] == 0.016
    assert endpoint["cost"]["rate_usd"] == 0.016
    assert endpoint["cost"]["rate_unit"] == "step"
    assert endpoint["cost"]["usd_min"] == 0.016
    assert endpoint["cost"]["usd"] == 8.0


@pytest.mark.parametrize(("terminal_status", "steps", "task_status", "settled_micro"), [
    ("COMPLETED", 3, "settled", 48_000),
    ("FAILED", 2, "settled", 32_000),
    ("CANCELLED", 4, "settled", 64_000),
    ("CANCELLED", 0, "settled", 0),
])
async def test_tinyfish_agent_holds_requested_ceiling_then_settles_terminal_steps(
    clients, monkeypatch, tinyfish_platform, terminal_status, steps, task_status, settled_micro,
):
    async def submit(*_args, **_kwargs):
        return _response(200, {"run_id": "tinyfish-run-1", "status": "PENDING"})

    monkeypatch.setattr(call_service, "relay", submit)
    response = await clients.post("/call/tinyfish.web.agent.run", json={
        "url": "https://example.com",
        "goal": "Return the heading.",
        "browser_profile": "lite",
        "agent_config": {"max_steps": 10},
    })
    assert response.status_code == 200, response.text

    async with session_maker() as db:
        row = (await db.execute(select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_id == "tinyfish-run-1"))).scalar_one()
        assert row.reserved_micro == 160_000
        assert row.settlement_basis["amount"] == {
            "kind": "usage", "path": "num_of_steps", "unit": "step", "unit_micro": 16_000,
        }
        row.next_check_at = utcnow_naive() - timedelta(seconds=1)
        call_id = row.call_id
        await db.commit()

    async def completed(_row, _client):
        return 200, json.dumps({
            "run_id": "tinyfish-run-1", "status": terminal_status,
            "num_of_steps": steps, "result": {"heading": "Example Domain"},
        }).encode()

    monkeypatch.setattr(task_app, "_poll", completed)
    result = await task_app.settle_due()
    assert result.settled == (task_status == "settled")
    assert result.released == (task_status == "released")
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        assert row.status == task_status
        assert row.settled_micro == settled_micro


async def test_tinyfish_wallet_collector_and_policy():
    def probe(request):
        assert request.method == "GET"
        assert request.url == "https://agent.tinyfish.ai/v1/wallet"
        assert request.headers["x-api-key"] == "test"
        return httpx.Response(200, json={
            "available_balance": "75.904",
            "currency": "usd",
            "auto_reload": {"state": "unconfigured"},
            "rates": {"meters": [{"product": "Agent", "rate": "0.016", "unit": "step"}]},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._tinyfish(client, "test")
    assert row == {
        "value": 75.904, "unit": "USD", "note": "vendor auto-reload unconfigured",
    }
    capacity = policy.default_policy("tinyfish", has_key=True)
    assert capacity.capacity_type == "cash"
    assert capacity.funding_mode == "manual"
    assert capacity.source == "api"
    assert capacity.rate_limit == {"limit": 30, "window_s": 60, "source": "docs"}


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", -1, "bad"])
async def test_tinyfish_wallet_rejects_invalid_balances(value):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"available_balance": value})
    )) as client:
        with pytest.raises(ValueError):
            await collectors._tinyfish(client, "test")
