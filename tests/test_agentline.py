"""AgentLine: shared-key tenancy of agents, the BYOK-only telephony boundary, and the key check."""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import oauth_providers
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.capacity import collectors
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import ProviderResource

TELEPHONY = (
    "agentline.numbers.buy", "agentline.numbers.release", "agentline.calls.create",
    "agentline.calls.get", "agentline.calls.transcript", "agentline.calls.hangup",
    "agentline.messages.send",
)
ACCOUNT_LISTS = (
    "agentline.agents.list", "agentline.numbers.list", "agentline.calls.list",
    "agentline.messages.list", "agentline.account.balance",
)
AGENT_SCOPED = (
    "agentline.agents.get", "agentline.agents.update", "agentline.agents.delete",
    "agentline.knowledge.remember", "agentline.knowledge.search", "agentline.knowledge.list",
    "agentline.contacts.create", "agentline.contacts.list", "agentline.contact_facts.set",
)


@pytest.fixture
def agentline_platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_AGENTLINE", "PLATFORM-AGENTLINE-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "agentline")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _response(status: int, body: bytes = b"{}"):
    async def stream():
        yield body

    async def close():
        return None

    return UpstreamResponse(status, (("content-type", "application/json"),), stream(), close)


async def _org_id(clients: AsyncClient) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _add_agent(clients: AsyncClient, agent_id: str) -> None:
    async with session_maker() as db:
        db.add(ProviderResource(
            org_id=await _org_id(clients), provider="agentline", resource_kind="agent",
            upstream_id=agent_id, display_name="Reception", created_by="tim@superdesign.dev",
            source_call_id="call-test",
        ))
        await db.commit()


def _endpoint(endpoint_id: str) -> dict:
    return next(e for e in catalog_store.load().endpoints if e["id"] == endpoint_id)


def test_registry_entry_checks_the_key_with_a_bearer_header_on_the_free_balance_route():
    provider = oauth_providers.get("agentline")
    assert provider is not None and provider.auth_kind == "key"
    assert (provider.token_header, provider.token_format) == ("Authorization", "Bearer {secret}")
    assert provider.base_url == "https://api.agentline.cloud"
    assert provider.probe_path == "/v1/billing/balance"
    assert provider.probe_reject_statuses == () and provider.token_verify_field == "account_id"
    assert provider.category in oauth_providers.CATEGORY_ORDER


def test_only_free_agent_setup_runs_on_treg_key():
    cat = catalog_store.load()
    rows = [e for e in cat.endpoints if e["provider"] == "agentline"]
    eligible = {e["id"] for e in rows if cat.platform_eligible(e)}
    for endpoint_id in TELEPHONY:
        assert endpoint_id not in eligible, endpoint_id
        assert _endpoint(endpoint_id)["platform_blocked"], endpoint_id
    for endpoint_id in ACCOUNT_LISTS:
        assert endpoint_id not in eligible, endpoint_id
    assert eligible == {"agentline.voices.list", "agentline.agents.create", *AGENT_SCOPED}
    for endpoint_id in eligible:
        assert cat.cost_view(_endpoint(endpoint_id).get("cost"), "agentline")["usd"] == 0, endpoint_id


def test_every_shared_key_agent_row_proves_the_team_owns_the_agent():
    for endpoint_id in AGENT_SCOPED:
        rule = _endpoint(endpoint_id)["managed_resource"]
        assert rule["kind"] == "agent", endpoint_id
        assert rule["id"] == {"in": "pathParams", "name": "agent_id"}, endpoint_id
    create = _endpoint("agentline.agents.create")["managed_resource"]
    assert create["operation"] == "create"
    assert create["cleanup_endpoint"] == "agentline.agents.delete"


async def test_agent_create_on_treg_key_becomes_the_team_resource(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    seen = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        seen.append((request.method, upstream_url))
        return _response(200, b'{"id":"agt_created","name":"Reception desk"}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post("/call/agentline.agents.create", json={"name": "Reception desk"})
    assert response.status_code == 200, response.text
    assert seen == [("POST", "https://api.agentline.cloud/v1/agents")]
    async with session_maker() as db:
        row = (await db.execute(select(ProviderResource).where(
            ProviderResource.provider == "agentline",
            ProviderResource.upstream_id == "agt_created"))).scalars().one()
    assert (row.org_id, row.resource_kind, row.display_name) == (
        await _org_id(clients), "agent", "Reception desk")


async def test_another_team_agent_is_refused_before_agentline_is_called(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _add_agent(clients, "agt_team_one")
    called = 0

    async def relay(*args, **kwargs):
        nonlocal called
        called += 1
        return _response(200)

    monkeypatch.setattr(call_service, "relay", relay)
    unknown = await clients.post(
        "/call/agentline.knowledge.remember?agent_id=agt_unknown", json={"text": "Open 9 to 5."})
    created = await clients.post("/orgs", json={"name": "Second Phone Team"})
    assert created.status_code < 300, created.text
    other = created.json()
    clients.headers["X-Treg-Token"] = other["token"]
    clients.headers.pop("X-Treg-Org", None)
    cross_team = await clients.post(
        "/call/agentline.knowledge.remember?agent_id=agt_team_one", json={"text": "Open 9 to 5."})
    contacts = await clients.get("/call/agentline.contacts.list?agent_id=agt_team_one")
    assert unknown.status_code == cross_team.status_code == contacts.status_code == 403
    assert unknown.json()["detail"] == cross_team.json()["detail"]
    assert called == 0


async def test_owned_agent_knowledge_reaches_agentline_on_treg_key(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _add_agent(clients, "agt_owned")
    seen = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        seen.append((request.method, upstream_url))
        return _response(200, b'{"key":"hours","text":"Open 9 to 5."}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/agentline.knowledge.remember?agent_id=agt_owned", json={"text": "Open 9 to 5."})
    assert response.status_code == 200, response.text
    assert seen == [("POST", "https://api.agentline.cloud/v1/agents/agt_owned/knowledge")]


async def test_calls_are_not_placed_on_treg_key(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _add_agent(clients, "agt_owned")
    called = False

    async def relay(*args, **kwargs):
        nonlocal called
        called = True
        return _response(200, b'{"id":"call_must_not_exist"}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/agentline.calls.create", json={"agent_id": "agt_owned", "to_number": "+15555550100"})
    assert response.status_code >= 400
    assert called is False


async def test_own_key_places_calls_without_ownership_checks(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await clients.post("/secrets", json={"name": "agentline", "value": "OWN-AGENTLINE-KEY"})
    seen = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        seen.append((request.method, upstream_url))
        return _response(200, b'{"id":"call_own","status":"initiated"}')

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post(
        "/call/agentline.calls.create",
        json={"agent_id": "agt_on_my_account", "to_number": "+15555550100"})
    assert response.status_code == 200, response.text
    assert seen == [("POST", "https://api.agentline.cloud/v1/calls")]


async def test_balance_collector_reads_the_wallet(monkeypatch):
    def probe(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.agentline.cloud/v1/billing/balance")
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"account_id": "acct", "balance": 12.5, "currency": "usd"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._agentline(client, "test-key")
    assert row == {"value": 12.5, "unit": "USD", "note": "prepaid wallet balance"}


@pytest.mark.parametrize("payload", [{}, {"balance": None}, {"balance": True}, {"balance": -1},
                                     {"balance": "NaN"}])
async def test_balance_collector_refuses_an_unreadable_balance(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ValueError):
            await collectors._agentline(client, "test-key")
