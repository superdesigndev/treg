"""Each team's daily share of an allowance treg's app holds with the provider.

Every team's YouTube connection made through treg's OAuth client spends the same Cloud project's
daily units, and every Google Ads connection carrying treg's developer token the same daily
operations. With a share configured, a team past it is refused before the call leaves; a
connection on the team's own client or developer token is never counted.
"""

from __future__ import annotations

import json
import time

import pytest
from httpx import AsyncClient

from treg import audit, crypto, oauth_providers
from treg.application.call import service as call_service
from treg.application.call.types import GatewayFailed, UpstreamResponse
from treg.config import SHARED_ALLOWANCE_SERVICES, Settings, get_settings
from treg.infra.db import session_maker
from treg.models import Secret, Tool

YT_HOST = "youtube.googleapis.com"
ADS_HOST = "googleads.googleapis.com"


@pytest.fixture
def shares(monkeypatch):
    monkeypatch.setenv("TREG_GOOGLE_ADS_DEVELOPER_TOKEN", "TREG-DEV-TOKEN")

    def set_shares(value: dict):
        monkeypatch.setenv("TREG_SHARED_ALLOWANCE_SHARES", json.dumps(value))
        get_settings.cache_clear()
    yield set_shares
    get_settings.cache_clear()


class Upstream:
    def __init__(self):
        self.calls: list[str] = []

    async def relay(self, request, upstream_url, tool, secrets, client, **kwargs):
        if before := kwargs.get("before_outbound"):
            await before()
        self.calls.append(upstream_url)

        async def stream():
            yield b'{"items": []}'

        async def close():
            return None
        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)


@pytest.fixture
def upstream(monkeypatch):
    up = Upstream()
    monkeypatch.setattr(call_service, "relay", up.relay)
    return up


async def _connect(clients: AsyncClient, name: str, host: str, *, provider: str,
                   developer_token: str | None = None) -> None:
    """What a connect leaves behind. `provider` is "" for a connection on the team's own client;
    `developer_token` "treg" binds treg's own token, any other value the team's."""
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    blob = {"access_token": "TOK", "refresh_token": "RT", "client_id": "cid",
            "client_secret": "cs", "token_uri": "http://upstream/token",
            "expires_at": time.time() + 3600}
    async with session_maker() as db:
        secret = Secret(org_id=org_id, name=name, owner="tim@superdesign.dev", kind="oauth",
                        value=crypto.encrypt(json.dumps(blob)), provider=provider)
        db.add(secret)
        await db.flush()
        bindings = [{"secret_id": secret.id, "injector": "oauth", "location": "header",
                     "name": "Authorization", "format": "Bearer {secret}",
                     "secret_field": "access_token"}]
        if developer_token == "treg":
            bindings.append({"platform_setting": "google_ads_developer_token", "injector": "env",
                             "location": "header", "name": "developer-token",
                             "format": "{secret}"})
        elif developer_token is not None:
            own = Secret(org_id=org_id, name=f"{name}-token", owner="tim@superdesign.dev",
                         kind="api_key", value=crypto.encrypt(developer_token))
            db.add(own)
            await db.flush()
            bindings.append({"secret_id": own.id, "injector": "env", "location": "header",
                             "name": "developer-token", "format": "{secret}"})
        db.add(Tool(org_id=org_id, name=name, owner="tim@superdesign.dev",
                    base_url=f"https://{host}", host=host, bindings=bindings))
        await db.commit()


async def test_a_team_past_its_daily_share_is_refused_and_its_own_client_is_not(
        clients: AsyncClient, shares, upstream):
    shares({"youtube": {"daily": 60}})
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    await _connect(clients, "youtube-own", YT_HOST, provider="")
    write = "/call/youtube/youtube/v3/videos?part=snippet"

    assert (await clients.put(write, json={})).status_code == 200            # 50 units
    assert (await clients.get("/call/youtube/youtube/v3/videos?id=a&part=id")).status_code == 200
    relayed = len(upstream.calls)
    r = await clients.put(write, json={})                                    # 51 + 50 > 60
    assert r.status_code == 429 and "X-Treg-Cost-Micro" not in r.headers
    assert int(r.headers["Retry-After"]) > 0
    detail = r.json()["detail"]
    assert detail["error"] == "shared_allowance_share_exceeded"
    assert (detail["share"], detail["used"], detail["units"]) == (60, 51, 50)
    assert detail["resets_at"].endswith("Z") and "own OAuth client" in detail["message"]
    assert len(upstream.calls) == relayed, "refused before it left"

    for _ in range(9):  # cheap calls still fit the rest of the share
        assert (await clients.get("/call/youtube/youtube/v3/channels?part=id")).status_code == 200
    assert (await clients.get("/call/youtube/youtube/v3/channels?part=id")).status_code == 429
    assert (await clients.put("/call/youtube-own/youtube/v3/videos?part=snippet",
                              json={})).status_code == 200

    await audit.drain()
    refused = [c for c in (await clients.get("/calls")).json() if c["status_code"] == 429]
    assert {c["refused_by"] for c in refused} == {"cap"}


async def test_google_ads_counts_treg_developer_token_calls_and_an_org_override_raises_one_team(
        clients: AsyncClient, shares, upstream):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    shares({"google-ads": {"daily": 1, "orgs": {str(org_id): 3}}})
    await _connect(clients, "google-ads", ADS_HOST, provider="google-ads", developer_token="treg")
    await _connect(clients, "google-ads-own", ADS_HOST, provider="google-ads",
                   developer_token="TEAM-TOKEN")
    search = "/call/google-ads/v25/customers/1/googleAds:search"
    codes = [(await clients.post(search, json={"query": "q"})).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = await clients.post("/call/google-ads-own/v25/customers/1/googleAds:search",
                           json={"query": "q"})
    assert r.status_code == 200, "the team's own developer token has no share"


async def test_a_call_that_never_leaves_spends_no_share(
        clients: AsyncClient, shares, upstream, monkeypatch):
    shares({"youtube": {"daily": 1}})
    await _connect(clients, "youtube", YT_HOST, provider="youtube")

    async def refused_before_sending(*args, **kwargs):
        raise GatewayFailed("ssrf_refused", status_code=403, detail="blocked")
    monkeypatch.setattr(call_service, "relay", refused_before_sending)
    for _ in range(3):
        assert (await clients.get("/call/youtube/youtube/v3/channels?part=id")).status_code == 403
    monkeypatch.setattr(call_service, "relay", upstream.relay)
    assert (await clients.get("/call/youtube/youtube/v3/channels?part=id")).status_code == 200


async def test_a_call_too_big_for_any_share_says_it_can_never_fit(
        clients: AsyncClient, shares, upstream):
    shares({"youtube": {"daily": 300}})
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    r = await clients.post("/call/youtube/upload/youtube/v3/captions?part=snippet", json={})
    assert r.status_code == 429 and "Retry-After" not in r.headers
    assert "can never fit" in r.json()["detail"]["message"]


async def test_without_shares_nothing_is_counted(clients: AsyncClient, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    for _ in range(3):
        assert (await clients.get("/call/youtube/youtube/v3/channels?part=id")).status_code == 200


def test_youtube_calls_are_priced_in_its_own_units():
    allowance = oauth_providers.get("youtube").shared_allowance
    prices = {call: allowance.units_for(call) for call in (
        "GET /youtube/v3/channels", "GET /youtube/v3/search", "POST /upload/youtube/v3/videos",
        "PUT /youtube/v3/videos", "POST /upload/youtube/v3/captions",
        "PUT /upload/youtube/v3/captions")}
    assert prices == {"GET /youtube/v3/channels": 1, "GET /youtube/v3/search": 1,
                      "POST /upload/youtube/v3/videos": 1, "PUT /youtube/v3/videos": 50,
                      "POST /upload/youtube/v3/captions": 400,
                      "PUT /upload/youtube/v3/captions": 450}


def test_every_shared_allowance_provider_can_be_given_a_share():
    assert SHARED_ALLOWANCE_SERVICES == {p.service for p in oauth_providers.REGISTRY.values()
                                         if p.shared_allowance is not None}


@pytest.mark.parametrize("raw", ['[]', '{"youtube": 5}', '{"youtube": {"daily": 0}}',
                                 '{"youtube": {"daily": 5, "orgs": {"x": 1}}}',
                                 '{"youtube": {"daily": 5, "orgs": {"01": 1}}}',
                                 '{"youtube": {"daily": 5, "extra": 1}}',
                                 '{"googleads": {"daily": 5}}'])
def test_a_malformed_share_setting_fails_at_boot(raw):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(shared_allowance_shares=raw)
