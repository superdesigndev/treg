"""Public-address checks at registration and on the actual HTTP call path."""

from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import select

from treg import crypto
from treg.config import get_settings
from treg.health import _notify, _probe
from treg.infra.oauth_exchange import HTTPXOAuthExchangePort
from treg.infra.db import session_maker
from treg.infra.oauth_refresh import HTTPXOAuthRefreshPort
from treg.infra.upstream.ssrf import safe_webhook_url
from treg.models import Membership, Secret, Tool, User


PUBLIC_ADDRESSES = [
    ("100.64.0.1", False),
    ("64:ff9b::7f00:1", False),
    ("224.0.0.1", False),
    ("ff02::1", False),
    ("8.8.8.8", True),
]


@pytest.mark.parametrize("address,public", PUBLIC_ADDRESSES)
def test_literal_target(address, public):
    host = f"[{address}]" if ":" in address else address
    assert safe_webhook_url(f"https://{host}/") is public


@pytest.mark.parametrize("address,public", PUBLIC_ADDRESSES)
async def test_call_rechecks_dns(clients, monkeypatch, fake_getaddrinfo, address, public):
    registered = await clients.post("/tools", json={
        "name": "rebound", "base_url": "https://rebound.example",
    })
    assert registered.status_code == 200
    monkeypatch.setattr(get_settings(), "proxy_ssrf_check", True)
    fake_getaddrinfo({"rebound.example": [address]})
    response = await clients.get("/call/rebound/echo")
    assert response.status_code == (200 if public else 502), response.text
    if not public:
        assert "non-public address" in response.text




def test_registration_refuses_a_trailing_dot_local_name():
    for url in ("http://localhost./", "http://localhost../", "http://metadata.google.internal./"):
        assert safe_webhook_url(url) is False, url


async def test_tool_registration_folds_a_trailing_dot_only_with_the_check_on(clients, monkeypatch):
    body = {"name": "dotted", "base_url": "http://localhost.:9/"}
    monkeypatch.setattr(get_settings(), "proxy_ssrf_check", True)
    assert (await clients.post("/tools", json=body)).status_code == 422
    monkeypatch.setattr(get_settings(), "proxy_ssrf_check", False)
    assert (await clients.post("/tools", json=body)).status_code == 200


def _recording_client(hits: list[str], answer: dict | None = None) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, json=answer if answer is not None else {"access_token": "NEW"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _oauth_blob(token_uri: str) -> dict:
    return {"refresh_token": "RT", "client_id": "cid", "client_secret": "csec", "token_uri": token_uri}


@pytest.fixture
def internal_names(monkeypatch, fake_getaddrinfo):
    monkeypatch.setattr(get_settings(), "proxy_ssrf_check", True)
    fake_getaddrinfo({"inside.example": ["10.0.0.5"], "outside.example": ["8.8.8.8"]})


async def test_health_probe_rechecks_dns(internal_names):
    tool = Tool(name="t", base_url="https://inside.example", host="inside.example",
                health_check={"path": "ping"}, bindings=[])
    hits: list[str] = []
    async with _recording_client(hits) as client:
        status, detail = await _probe(tool, {}, client)
        assert (status, hits) == ("unknown", [])
        assert "non-public address" in detail
        tool.base_url = "https://outside.example"
        status, _ = await _probe(tool, {}, client)
    assert status == "ok"
    assert hits == ["https://outside.example/ping"]


async def test_health_webhook_rechecks_dns(internal_names, clients):
    async with session_maker() as db:
        user = (await db.execute(select(User))).scalars().first()
        membership = (await db.execute(select(Membership).where(Membership.user_id == user.id))).scalars().first()
        membership.webhook_url = "https://inside.example/hook"
        await db.commit()
        invalid = [Secret(id=1, name="s", owner=user.email, org_id=membership.org_id, value="")]
        hits: list[str] = []
        async with _recording_client(hits) as client:
            await _notify(invalid, db, client)
        assert hits == []


async def test_oauth_refresh_rechecks_dns(internal_names):
    hits: list[str] = []
    async with _recording_client(hits) as client:
        with pytest.raises(ValueError, match="non-public address"):
            await HTTPXOAuthRefreshPort(client).exchange(_oauth_blob("https://inside.example/token"))
        assert hits == []
        new = await HTTPXOAuthRefreshPort(client).exchange(_oauth_blob("https://outside.example/token"))
    assert new["access_token"] == "NEW"
    assert hits == ["https://outside.example/token"]


async def test_oauth_code_exchange_rechecks_dns(internal_names):
    pending = SimpleNamespace(
        client_secret=crypto.encrypt("csec"), client_id="cid", redirect_uri="https://treg.test/cb",
        code_verifier="", token_endpoint_auth_method="client_secret_post",
        token_uri="https://inside.example/token",
    )
    hits: list[str] = []
    async with _recording_client(hits) as client:
        with pytest.raises(ValueError, match="non-public address"):
            await HTTPXOAuthExchangePort(client).exchange_code(pending, "CODE")
    assert hits == []


async def test_token_answer_without_access_token_is_not_echoed():
    answer = {"error": "MARKER-ERROR", "other": "MARKER-BODY"}
    pending = SimpleNamespace(
        client_secret=crypto.encrypt("csec"), client_id="cid", redirect_uri="https://treg.test/cb",
        code_verifier="", token_endpoint_auth_method="client_secret_post",
        token_uri="https://outside.example/token",
    )
    async with _recording_client([], answer) as client:
        with pytest.raises(ValueError) as refresh_error:
            await HTTPXOAuthRefreshPort(client).exchange(_oauth_blob("https://outside.example/token"))
        with pytest.raises(ValueError) as exchange_error:
            await HTTPXOAuthExchangePort(client).exchange_code(pending, "CODE")
    for error in (refresh_error, exchange_error):
        assert "no access_token" in str(error.value)
        assert "MARKER" not in str(error.value)
