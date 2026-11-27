"""AgentLine: shared-key tenancy of agents, the BYOK-only telephony boundary, and the key check."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import oauth_providers
from treg.application import asynctasks as task_app
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain import money
from treg.domain.capacity import collectors
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, ProviderResource
from treg.timeutil import utcnow_naive

ACCOUNT_LISTS = (
    "agentline.agents.list", "agentline.numbers.list", "agentline.calls.list",
    "agentline.messages.list", "agentline.account.balance",
)
AGENT_SCOPED = (
    "agentline.agents.get", "agentline.agents.update", "agentline.agents.delete",
    "agentline.knowledge.remember", "agentline.knowledge.search", "agentline.knowledge.list",
    "agentline.contacts.create", "agentline.contacts.list", "agentline.contact_facts.set",
)
NUMBER_SCOPED = ("agentline.numbers.get", "agentline.numbers.release")
CALL_SCOPED = ("agentline.calls.get", "agentline.calls.billing", "agentline.calls.hangup")
PAID = ("agentline.numbers.buy", "agentline.calls.create", "agentline.messages.send")


@pytest.fixture
async def agentline_platform_on(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_AGENTLINE", "PLATFORM-AGENTLINE-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "agentline")
    get_settings.cache_clear()
    # Enough credit for a number and a call's hold; the signup credit alone is not.
    async with session_maker() as db:
        await money.grant(db, await _org_id(clients), amount_micro=20_000_000, kind="earned", once=False)
        await db.commit()
    yield
    get_settings.cache_clear()


def _response(status: int, body: bytes = b"{}"):
    async def stream():
        yield body

    async def close():
        return None

    return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)


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


def test_every_tool_but_the_account_lists_runs_on_treg_key():
    cat = catalog_store.load()
    rows = {e["id"]: e for e in cat.endpoints if e["provider"] == "agentline"}
    eligible = {i for i, e in rows.items() if cat.platform_eligible(e)}
    assert eligible == set(rows) - set(ACCOUNT_LISTS)
    assert not any(e.get("platform_blocked") for e in rows.values())
    for endpoint_id in PAID:
        cost = cat.cost_view(rows[endpoint_id].get("cost"), "agentline")
        assert cost["usd"] and cost["confidence"] == "documented", endpoint_id


def test_a_call_is_settled_from_agentlines_own_billing_record():
    assert "platform_request" not in {k for k, v in _endpoint("agentline.numbers.buy").items() if v}
    call = _endpoint("agentline.calls.create")
    assert call["async"]["poll"] == {
        "endpoint": "agentline.calls.billing", "param": {"in": "pathParams", "name": "call_id"}}
    assert call["async"]["status"]["path"] == "call_status"
    assert call["async"]["status"]["failure"] == ["failed", "busy", "no-answer", "canceled"]
    assert call["settle"] == {"usage": {"path": "response.expected_cost", "unit": "usd"}, "worst_case": "0.60"}
    assert _endpoint("agentline.calls.billing")["path"] == "/v1/billing/verify/{call_id}"
    for field in ("max_duration_seconds", "auto_renew", "inbound_enabled"):
        assert field not in call["input"]["body"], "only fields AgentLine's API takes today"
        assert field not in _endpoint("agentline.numbers.buy")["input"]["body"]


def test_numbers_and_calls_are_owned_by_the_team_that_made_them():
    assert _endpoint("agentline.numbers.buy")["managed_resource"] == {
        "operation": "create", "kind": "number", "id": {"in": "response", "path": "id"}}
    assert _endpoint("agentline.numbers.buy")["resource_ownership"]["requires"] == {
        "kind": "agent", "param": "agent_id", "in": "body"}
    assert _endpoint("agentline.agents.create")["resource_ownership"]["produces"] == [
        {"kind": "agent", "path": "id"}]
    for endpoint_id in NUMBER_SCOPED:
        rule = _endpoint(endpoint_id)["managed_resource"]
        assert (rule["kind"], rule["id"]) == ("number", {"in": "pathParams", "name": "number_id"}), endpoint_id
    assert _endpoint("agentline.numbers.release")["managed_resource"]["operation"] == "delete"
    for endpoint_id in CALL_SCOPED:
        assert _endpoint(endpoint_id)["resource_ownership"]["requires"] == {
            "kind": "poll:agentline.calls.billing", "param": "call_id"}, endpoint_id
    assert _endpoint("agentline.calls.transcript")["resource_ownership"]["requires"] == {
        "kind": "fetch:agentline.calls.transcript", "param": "call_id"}
    for endpoint_id in ("agentline.calls.create", "agentline.messages.send"):
        assert _endpoint(endpoint_id)["managed_resource"] == {
            "operation": "use", "kind": "agent", "id": {"in": "body", "path": "agent_id"}}, endpoint_id


def test_every_shared_key_agent_row_proves_the_team_owns_the_agent():
    for endpoint_id in AGENT_SCOPED:
        rule = _endpoint(endpoint_id)["managed_resource"]
        assert rule["kind"] == "agent", endpoint_id
        assert rule["id"] == {"in": "pathParams", "name": "agent_id"}, endpoint_id
    create = _endpoint("agentline.agents.create")["managed_resource"]
    assert create["operation"] == "create"
    # Compensation replaces `{id}` in the cleanup path; this provider's is `{agent_id}`, so an
    # agent whose record failed is left for the operator. It costs nothing.
    assert "cleanup_endpoint" not in create


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


async def _balance(clients: AsyncClient) -> int:
    return (await clients.get(f"/orgs/{await _org_id(clients)}/balance")).json()["balance_micro"]


def _with_margin(micro: int) -> int:
    assert get_settings().platform_margin == 0.0
    return micro


async def _create_agent(clients: AsyncClient, monkeypatch, agent_id: str = "agt_mine") -> None:
    """An agent made through treg's key, so the team owns it on every ownership table."""
    async def relay(*args, **kwargs):
        return _response(200, json.dumps({"id": agent_id, "name": "Reception"}).encode())

    monkeypatch.setattr(call_service, "relay", relay)
    response = await clients.post("/call/agentline.agents.create", json={"name": "Reception"})
    assert response.status_code == 200, response.text


def _relay(monkeypatch, status: int, document: dict):
    seen = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        sent = b"".join([chunk async for chunk in request.body_stream()]) if request.has_body else b""
        seen.append((request.method, upstream_url, json.loads(sent or b"null")))
        return _response(status, json.dumps(document).encode())

    monkeypatch.setattr(call_service, "relay", relay)
    return seen


BUY: dict = {}


async def test_buying_a_number_needs_an_agent_the_team_owns(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _create_agent(clients, monkeypatch)
    seen = _relay(monkeypatch, 200, {"id": "num_must_not_exist"})
    foreign = await clients.post("/call/agentline.numbers.buy", json={"agent_id": "agt_someone_else"})
    assert foreign.status_code in (403, 404), foreign.text
    assert seen == []


async def test_a_bought_number_is_charged_once_and_becomes_the_team_resource(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _create_agent(clients, monkeypatch)
    before = await _balance(clients)
    seen = _relay(monkeypatch, 200, {"id": "num_bought", "phone_number": "+14155550123",
                                      "cost": 3.0})
    response = await clients.post("/call/agentline.numbers.buy", json={"agent_id": "agt_mine", **BUY})
    assert response.status_code == 200, response.text
    assert seen == [("POST", "https://api.agentline.cloud/v1/numbers", {"agent_id": "agt_mine", **BUY})]
    assert before - await _balance(clients) == _with_margin(3_000_000)
    async with session_maker() as db:
        row = (await db.execute(select(ProviderResource).where(
            ProviderResource.provider == "agentline",
            ProviderResource.upstream_id == "num_bought"))).scalars().one()
    assert (row.org_id, row.resource_kind, row.status) == (await _org_id(clients), "number", "active")


async def test_a_refused_purchase_costs_nothing(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _create_agent(clients, monkeypatch)
    before = await _balance(clients)
    _relay(monkeypatch, 409, {"detail": "Agent already has an active number."})
    response = await clients.post("/call/agentline.numbers.buy", json={"agent_id": "agt_mine", **BUY})
    assert response.status_code == 409
    assert await _balance(clients) == before


async def _add_number(clients: AsyncClient, number_id: str) -> None:
    async with session_maker() as db:
        db.add(ProviderResource(
            org_id=await _org_id(clients), provider="agentline", resource_kind="number",
            upstream_id=number_id, display_name=None, created_by="tim@superdesign.dev",
            source_call_id="call-test",
        ))
        await db.commit()


async def test_only_the_owning_team_reads_or_releases_a_number(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _add_number(clients, "num_team_one")
    seen = _relay(monkeypatch, 200, {"id": "num_team_one", "status": "active"})
    mine = await clients.get("/call/agentline.numbers.get?number_id=num_team_one")
    assert mine.status_code == 200, mine.text
    assert seen == [("GET", "https://api.agentline.cloud/v1/numbers/num_team_one", None)]

    created = await clients.post("/orgs", json={"name": "Second Phone Team"})
    assert created.status_code < 300, created.text
    clients.headers["X-Treg-Token"] = created.json()["token"]
    clients.headers.pop("X-Treg-Org", None)
    seen.clear()
    answers = [
        await clients.get("/call/agentline.numbers.get?number_id=num_team_one"),
        await clients.post("/call/agentline.numbers.release?number_id=num_team_one"),
        await clients.get("/call/agentline.numbers.get?number_id=num_unknown"),
    ]
    assert [a.status_code for a in answers] == [403, 403, 403]
    assert seen == []


async def test_releasing_a_number_ends_the_teams_hold_on_it(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _add_number(clients, "num_done")
    _relay(monkeypatch, 200, {"number_id": "num_done", "status": "released", "released": True})
    response = await clients.post("/call/agentline.numbers.release?number_id=num_done")
    assert response.status_code == 200, response.text
    async with session_maker() as db:
        row = (await db.execute(select(ProviderResource).where(
            ProviderResource.upstream_id == "num_done"))).scalars().one()
    assert row.status == "deleted"
    again = await clients.get("/call/agentline.numbers.get?number_id=num_done")
    assert again.status_code == 403


async def test_a_call_from_another_teams_agent_is_refused(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _create_agent(clients, monkeypatch)
    seen = _relay(monkeypatch, 200, {"id": "call_must_not_exist", "status": "initiated"})
    foreign = await clients.post(
        "/call/agentline.calls.create", json={"agent_id": "agt_someone_else", "to_number": "+12125550100"})
    assert foreign.status_code == 403, foreign.text
    assert seen == []


@pytest.mark.parametrize(("billing", "task_status", "settled_micro"), [
    ({"call_status": "completed", "duration_seconds": 95, "expected_cost": 0.19, "actual_deducted": 0.19}, "settled", 190_000),
    ({"call_status": "completed", "duration_seconds": 20, "expected_cost": 0.12, "actual_deducted": 0.12}, "settled", 120_000),
    # Longer than the five minutes held: AgentLine's figure is what is charged.
    ({"call_status": "completed", "duration_seconds": 900, "expected_cost": 1.8, "actual_deducted": 1.8}, "settled", 1_800_000),
    ({"call_status": "completed", "duration_seconds": 0, "expected_cost": 0.0, "actual_deducted": 0.0}, "settled", 0),
    ({"call_status": "no-answer", "duration_seconds": 0, "expected_cost": 0.0, "actual_deducted": 0.0}, "released", 0),
    ({"call_status": "busy", "duration_seconds": 0, "expected_cost": 0.0, "actual_deducted": 0.0}, "released", 0),
])
async def test_a_call_holds_five_minutes_then_settles_what_agentline_billed(
    clients: AsyncClient, agentline_platform_on, monkeypatch, billing, task_status, settled_micro,
):
    await _create_agent(clients, monkeypatch)
    _relay(monkeypatch, 200, {"id": "call_live", "status": "initiated"})
    response = await clients.post(
        "/call/agentline.calls.create", json={"agent_id": "agt_mine", "to_number": "+12125550100"})
    assert response.status_code == 200, response.text

    async with session_maker() as db:
        row = (await db.execute(select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_id == "call_live"))).scalar_one()
        assert row.reserved_micro == 600_000, "five minutes at $0.12 a minute"
        row.next_check_at = utcnow_naive() - timedelta(seconds=1)
        call_id = row.call_id
        await db.commit()

    async def poll(_row, _client):
        return 200, json.dumps({"call_id": "call_live", "currency": "USD", **billing}).encode()

    monkeypatch.setattr(task_app, "_poll", poll)
    result = await task_app.settle_due()
    assert result.settled == (task_status == "settled")
    assert result.released == (task_status == "released")
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
    assert (row.status, row.settled_micro) == (task_status, settled_micro)


async def test_only_the_team_that_placed_a_call_reads_or_ends_it(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _create_agent(clients, monkeypatch)
    _relay(monkeypatch, 200, {"id": "call_mine", "status": "initiated"})
    placed = await clients.post(
        "/call/agentline.calls.create", json={"agent_id": "agt_mine", "to_number": "+12125550100"})
    assert placed.status_code == 200, placed.text
    seen = _relay(monkeypatch, 200, {"id": "call_mine", "status": "in-progress", "transcript": []})
    mine = await clients.get("/call/agentline.calls.get?call_id=call_mine")
    assert mine.status_code == 200, mine.text
    assert seen == [("GET", "https://api.agentline.cloud/v1/calls/call_mine", None)]

    created = await clients.post("/orgs", json={"name": "Second Phone Team"})
    clients.headers["X-Treg-Token"] = created.json()["token"]
    clients.headers.pop("X-Treg-Org", None)
    seen.clear()
    answers = [
        await clients.get("/call/agentline.calls.transcript?call_id=call_mine"),
        await clients.get("/call/agentline.calls.get?call_id=call_mine"),
        await clients.get("/call/agentline.calls.billing?call_id=call_mine"),
        await clients.post("/call/agentline.calls.hangup?call_id=call_mine"),
    ]
    assert all(a.status_code in (403, 404) for a in answers), [a.status_code for a in answers]
    assert seen == []


@pytest.mark.parametrize(("answer", "charged_micro"), [
    ({"segments": 1, "cost": 0.02}, 20_000),
    ({"segments": 2, "cost": 0.02}, 20_000),
    ({"segments": 3, "cost": 0.04}, 40_000),
    ({"segments": 4, "cost": 0.06}, 60_000),
    ({"segments": 1, "cost": 0.0, "test": True}, 0),
    # An answer without cost (an AgentLine release older than the field): the listed $0.02.
    ({}, 20_000),
])
async def test_a_text_is_charged_the_cost_agentline_reports(
    clients: AsyncClient, agentline_platform_on, monkeypatch, answer, charged_micro,
):
    await _create_agent(clients, monkeypatch)
    before = await _balance(clients)
    seen = _relay(monkeypatch, 200, {"id": "msg_1", "status": "queued", **answer})
    response = await clients.post(
        "/call/agentline.messages.send",
        json={"agent_id": "agt_mine", "to_number": "+12125550100", "body": "Your table is ready."})
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    assert before - await _balance(clients) == _with_margin(charged_micro)


async def test_a_refused_text_costs_nothing(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    await _create_agent(clients, monkeypatch)
    before = await _balance(clients)
    _relay(monkeypatch, 429, {"detail": "Daily SMS limit reached."})
    refused = await clients.post(
        "/call/agentline.messages.send",
        json={"agent_id": "agt_mine", "to_number": "+12125550100", "body": "Your table is ready."})
    assert refused.status_code == 429
    assert await _balance(clients) == before


async def test_a_text_from_another_teams_agent_is_refused(
    clients: AsyncClient, agentline_platform_on, monkeypatch,
):
    seen = _relay(monkeypatch, 200, {"id": "msg_must_not_exist"})
    response = await clients.post(
        "/call/agentline.messages.send",
        json={"agent_id": "agt_someone_else", "to_number": "+12125550100", "body": "hi"})
    assert response.status_code == 403
    assert seen == []


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
