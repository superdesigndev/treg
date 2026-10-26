"""OpenMail on treg's key: pod-per-team provisioning, pod-key relay and the offer's prices."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import crypto
from treg.application.call import openmail as openmail_platform
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import CallRecord, PlatformSubaccount
from tests.conftest import app

ROOT_KEY = "om_root_treg"
POD_KEY = "om_pod_scoped"


@pytest.fixture
def openmail_platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_OPENMAIL", ROOT_KEY)
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "openmail")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOpenMail:
    """treg's root-key calls to OpenMail's pod API: what was asked, answered from a script."""

    def __init__(self, *, create_status: int = 201):
        self.calls: list[tuple[str, str, str | None, dict | None]] = []
        self.create_status = create_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, auth, body))
        if request.url.path == "/v1/pods" and request.method == "POST":
            if self.create_status == 201:
                return httpx.Response(201, json={"id": "pod_1", "clientId": body["clientId"]})
            return httpx.Response(self.create_status, json={"error": "conflict"})
        if request.url.path == "/v1/pods" and request.method == "GET":
            return httpx.Response(200, json={"data": [
                {"id": "pod_other", "clientId": "treg-org-999"},
                {"id": "pod_1", "clientId": "treg-org-1"},
            ], "total": 2})
        if request.url.path == "/v1/pods/pod_1/api-keys":
            return httpx.Response(201, json={"id": "key_1", "token": POD_KEY})
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def openmail_upstream(monkeypatch):
    fake = FakeOpenMail()
    monkeypatch.setattr(app.state, "http", AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake


def _response(status: int, body: bytes = b"{}"):
    async def stream():
        yield body

    async def close():
        return None

    return UpstreamResponse(status, (), stream(), close)


def _capture_relay(seen: list, status: int = 201, body: bytes = b'{"id":"inb_1"}'):
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        [binding] = tool.bindings
        seen.append((request.method, upstream_url, crypto.decrypt(secrets[binding["secret_id"]].value)))
        return _response(status, body)
    return relay


async def _org_id(clients: AsyncClient) -> int:
    return (await clients.get("/orgs")).json()[0]["org_id"]


async def _fund(clients: AsyncClient, micro: int = 10_000_000) -> None:
    from treg.domain import money
    async with session_maker() as db:
        await money.grant(db, await _org_id(clients), amount_micro=micro, kind="topup", once=False)
        await db.commit()


async def _charged(clients: AsyncClient) -> list[int | None]:
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.org_id == await _org_id(clients))
                                 .order_by(CallRecord.id))).scalars().all()
        return [r.cost_charged_micro for r in rows]


async def test_first_call_provisions_a_pod_and_relays_on_its_key(
    clients: AsyncClient, openmail_platform_on, openmail_upstream, monkeypatch,
):
    seen = []
    monkeypatch.setattr(call_service, "relay", _capture_relay(seen))
    r = await clients.post("/call/openmail.inboxes.create", json={"term": "30d", "displayName": "Agent"})
    assert r.status_code == 201, r.text
    org = await _org_id(clients)
    # Root key provisioned exactly once: pod with the org's clientId, then one scoped key.
    assert [(m, p, a) for m, p, a, _ in openmail_upstream.calls] == [
        ("POST", "/v1/pods", f"Bearer {ROOT_KEY}"),
        ("POST", "/v1/pods/pod_1/api-keys", f"Bearer {ROOT_KEY}"),
    ]
    assert openmail_upstream.calls[0][3]["clientId"] == f"treg-org-{org}"
    # The relayed call carried the POD key, never the root key.
    assert seen == [("POST", "https://api.openmail.sh/v1/inboxes", POD_KEY)]
    async with session_maker() as db:
        [row] = (await db.execute(select(PlatformSubaccount))).scalars().all()
        assert (row.org_id, row.provider, row.upstream_id) == (org, "openmail", "pod_1")
        assert crypto.decrypt(row.secret) == POD_KEY
    # Second call: no provisioning, same pod key.
    r = await clients.get("/call/openmail.inboxes.list")
    assert r.status_code == 201
    assert len(openmail_upstream.calls) == 2
    assert seen[-1] == ("GET", "https://api.openmail.sh/v1/inboxes", POD_KEY)


async def test_existing_pod_is_reused_after_a_409(
    clients: AsyncClient, openmail_platform_on, openmail_upstream, monkeypatch,
):
    openmail_upstream.create_status = 409
    seen = []
    monkeypatch.setattr(call_service, "relay", _capture_relay(seen))
    r = await clients.post("/call/openmail.inboxes.create", json={"term": "7d"})
    assert r.status_code == 201, r.text
    assert [(m, p) for m, p, _, _ in openmail_upstream.calls] == [
        ("POST", "/v1/pods"), ("GET", "/v1/pods"), ("POST", "/v1/pods/pod_1/api-keys"),
    ]
    assert seen[0][2] == POD_KEY


async def test_provisioning_failure_is_a_503_and_charges_nothing(
    clients: AsyncClient, openmail_platform_on, openmail_upstream, monkeypatch,
):
    openmail_upstream.create_status = 503
    relayed = []
    monkeypatch.setattr(call_service, "relay", _capture_relay(relayed))
    r = await clients.post("/call/openmail.inboxes.create", json={"term": "30d"})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == "platform_subaccount_unavailable"
    assert relayed == []
    async with session_maker() as db:
        assert (await db.execute(select(PlatformSubaccount))).scalars().all() == []


async def test_pod_id_in_the_body_is_refused_before_the_relay(
    clients: AsyncClient, openmail_platform_on, openmail_upstream, monkeypatch,
):
    relayed = []
    monkeypatch.setattr(call_service, "relay", _capture_relay(relayed))
    r = await clients.post("/call/openmail.inboxes.create", json={"term": "30d", "podId": "pod_other"})
    assert r.status_code == 400, r.text
    assert relayed == []
    assert openmail_upstream.calls == []  # refused before anything was provisioned


async def test_inbox_term_sets_the_price(
    clients: AsyncClient, openmail_platform_on, openmail_upstream, monkeypatch,
):
    await _fund(clients)
    seen = []
    monkeypatch.setattr(call_service, "relay", _capture_relay(seen))
    assert (await clients.post("/call/openmail.inboxes.create", json={"term": "7d"})).status_code == 201
    assert (await clients.post("/call/openmail.inboxes.create", json={"term": "30d"})).status_code == 201
    assert (await clients.post("/call/openmail.inboxes.renew", params={"id": "inb_1"},
                               json={"term": "7d"})).status_code == 201
    assert (await clients.post("/call/openmail.emails.send", params={"id": "inb_1"},
                               json={"to": "a@example.com", "body": "hi"})).status_code == 201
    assert (await clients.get("/call/openmail.threads.list", params={"id": "inb_1"})).status_code == 201
    assert [u for _, u, _ in seen][2:] == [
        "https://api.openmail.sh/v1/inboxes/inb_1/renew",
        "https://api.openmail.sh/v1/inboxes/inb_1/send",
        "https://api.openmail.sh/v1/inboxes/inb_1/threads",
    ]
    assert await _charged(clients) == [300_000, 1_000_000, 300_000, 2_000, 0]


async def test_a_blocked_send_is_free(
    clients: AsyncClient, openmail_platform_on, openmail_upstream, monkeypatch,
):
    monkeypatch.setattr(call_service, "relay",
                        _capture_relay([], status=422, body=b'{"error":"phishing_detected"}'))
    r = await clients.post("/call/openmail.emails.send", params={"id": "inb_1"},
                           json={"to": "a@example.com", "body": "hi"})
    assert r.status_code == 422
    assert await _charged(clients) == [0]


async def test_byok_key_bypasses_the_pod_and_domains_are_byok_only(
    clients: AsyncClient, openmail_platform_on, openmail_upstream, monkeypatch,
):
    # Without a key of their own, custom domains are not offered on treg's key at all.
    r = await clients.post("/call/openmail.domains.add", json={"domain": "mail.example.com"})
    assert r.status_code in (402, 404, 409), r.text
    assert openmail_upstream.calls == []

    seen = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        [binding] = tool.bindings
        seen.append(crypto.decrypt(secrets[binding["secret_id"]].value))
        return _response(201, b'{"id":"dom_1"}')

    monkeypatch.setattr(call_service, "relay", relay)
    r = await clients.post("/secrets", json={"name": "openmail", "value": "om_byok", "provider": "openmail"})
    assert r.status_code in (200, 201), r.text
    r = await clients.post("/call/openmail.domains.add", json={"domain": "mail.example.com"})
    assert r.status_code == 201, r.text
    assert seen == ["om_byok"]
    assert openmail_upstream.calls == []  # a team on its own key never gets a pod
