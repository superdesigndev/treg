"""A connection that spends an allowance treg's app holds with the provider.

Every team's YouTube connection made through treg's OAuth client spends the same Cloud project's
daily units, and every Google Ads connection carrying treg's developer token the same daily
operations. Once the provider says the allowance is spent, the next such call is refused before it
leaves (`enforce`), one probe a minute finds the reset, and a connection on the team's own client
or developer token is never touched.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from treg import audit, crypto, ratestore
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.capacity import marks as capacity_marks
from treg.domain.capacity import signatures
from treg.domain.capacity.marks import LOCK_NS, Lock
from treg.infra.db import session_maker
from treg.models import Secret, Tool
from treg.timeutil import utcnow_naive

YT_HOST = "youtube.googleapis.com"
ADS_HOST = "googleads.googleapis.com"
YT_QUOTA = (b'{"error": {"code": 403, "errors": [{"domain": "youtube.quota", '
            b'"reason": "quotaExceeded"}]}}')
YT_SEARCH_QUOTA = (b'{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [{'
                   b'"metadata": {"quota_limit": "defaultSearchListPerDayPerProject"}}]}}')


def _ads_quota(scope: str, seconds: int) -> bytes:
    return json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [{
        "errors": [{"errorCode": {"quotaError": "RESOURCE_EXHAUSTED"},
                    "details": {"quotaErrorDetails": {"rateScope": scope,
                                                      "retryDelay": f"{seconds}s"}}}]}]}}).encode()


@pytest.fixture
def allowance_mode(monkeypatch):
    def set_mode(mode: str):
        monkeypatch.setenv("TREG_SHARED_ALLOWANCE_MODE", mode)
        monkeypatch.setenv("TREG_GOOGLE_ADS_DEVELOPER_TOKEN", "TREG-DEV-TOKEN")
        get_settings.cache_clear()
    monkeypatch.setattr(capacity_marks, "STRIKE_MIN_GAP", timedelta(0))
    set_mode("enforce")
    yield set_mode
    get_settings.cache_clear()


class Upstream:
    """The provider: answers each call with the status and body set last, and counts the calls."""

    def __init__(self):
        self.calls: list[str] = []
        self.answer = (200, b'{"items": []}')

    async def relay(self, request, upstream_url, tool, secrets, client, **kwargs):
        if before := kwargs.get("before_outbound"):
            await before()
        self.calls.append(upstream_url)
        status, body, *headers = self.answer

        async def stream():
            yield body

        async def close():
            return None
        return UpstreamResponse(status, ((b"content-type", b"application/json"), *headers),
                                stream(), close)


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


async def _lock(key: str) -> Lock | None:
    async with session_maker() as db:
        raw = await ratestore.kv_get(db, LOCK_NS, key)
    return Lock.from_json(raw) if raw else None


async def test_a_spent_youtube_allowance_refuses_treg_app_connections_until_a_probe_succeeds(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    await _connect(clients, "youtube-own", YT_HOST, provider="")
    channels = "/call/youtube/youtube/v3/channels?part=snippet&mine=true"

    upstream.answer = (403, YT_QUOTA)
    for _ in range(2):  # the breaker locks on the second signature
        r = await clients.get(channels)
        assert r.status_code == 403 and r.json()["error"]["errors"][0]["reason"] == "quotaExceeded"
    lock = await _lock("allowance:youtube")
    assert lock is not None and lock.is_active()
    assert lock.until == signatures._next_pacific_midnight(lock.opened_at)

    relayed = len(upstream.calls)
    r = await clients.get(channels)
    assert r.status_code == 503 and "X-Treg-Cost-Micro" not in r.headers
    detail = r.json()["detail"]
    assert detail["error"] == "shared_allowance_exhausted" and detail["provider"] == "youtube"
    assert detail["resets_at"] == lock.until.isoformat() + "Z"
    assert "own OAuth client" in detail["message"]
    assert len(upstream.calls) == relayed, "refused before it left"

    # The team's own client draws on its own project's allowance.
    assert (await clients.get("/call/youtube-own/youtube/v3/channels?part=id")).status_code == 403
    assert len(upstream.calls) == relayed + 1

    # A minute later one call probes; its 2xx lifts the lock for everyone.
    capacity_marks._last_probe.clear()
    upstream.answer = (200, b'{"items": []}')
    assert (await clients.get(channels)).status_code == 200
    assert await _lock("allowance:youtube") is None
    assert (await clients.get(channels)).status_code == 200

    await audit.drain()
    refused = [c for c in (await clients.get("/calls")).json() if c["status_code"] == 503]
    assert [c["refused_by"] for c in refused] == ["capacity"]


async def test_search_running_out_of_its_own_limit_leaves_the_rest_serving(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    upstream.answer = (429, YT_SEARCH_QUOTA)
    for _ in range(2):
        assert (await clients.get("/call/youtube/youtube/v3/search?q=x&part=id")).status_code == 429
    assert (await _lock("allowance:youtube/search")).is_active()
    assert await _lock("allowance:youtube") is None

    upstream.answer = (200, b'{"items": []}')
    r = await clients.get("/call/youtube/youtube/v3/search?q=y&part=id")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "shared_allowance_exhausted"
    assert (await clients.get("/call/youtube/youtube/v3/videos?id=a&part=id")).status_code == 200
    r = await clients.post("/call/youtube/youtube/v3/videos?part=snippet", json={})
    assert r.status_code == 200, "a write is not limited by search's own limit"


async def test_a_costly_call_told_the_units_are_spent_leaves_cheap_calls_serving(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    upstream.answer = (403, YT_QUOTA)
    for _ in range(2):
        assert (await clients.put("/call/youtube/youtube/v3/videos?part=snippet",
                                  json={})).status_code == 403
    assert (await _lock("allowance:youtube/writes")).is_active()
    assert await _lock("allowance:youtube") is None

    upstream.answer = (200, b'{"items": []}')
    assert (await clients.put("/call/youtube/youtube/v3/videos?part=snippet",
                              json={})).status_code == 503
    assert (await clients.get("/call/youtube/youtube/v3/search?q=x&part=id")).status_code == 200


async def test_a_search_told_the_units_are_spent_locks_the_whole_allowance(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    upstream.answer = (403, YT_QUOTA)
    for _ in range(2):
        await clients.get("/call/youtube/youtube/v3/search?q=x&part=id")
    assert (await _lock("allowance:youtube")).is_active()
    assert await _lock("allowance:youtube/search") is None


async def test_a_success_between_two_quota_answers_keeps_the_allowance_open(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    channels = "/call/youtube/youtube/v3/channels?part=id&mine=true"
    for answer in ((403, YT_QUOTA), (200, b'{"items": []}'), (403, YT_QUOTA)):
        upstream.answer = answer
        await clients.get(channels)
    lock = await _lock("allowance:youtube")
    assert lock is not None and not lock.is_active() and lock.strikes == 1


async def test_google_ads_locks_on_the_developer_tokens_daily_operations_only(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "google-ads", ADS_HOST, provider="google-ads", developer_token="treg")
    await _connect(clients, "google-ads-own", ADS_HOST, provider="google-ads",
                   developer_token="TEAM-TOKEN")
    search = "/call/google-ads/v25/customers/123/googleAds:search"

    # One customer account's limit, a short or unstated wait on the token, and a long
    # Retry-After on an answer the provider's rows do not recognise are not a spent day.
    no_delay = _ads_quota("DEVELOPER", 0).replace(b'"retryDelay": "0s"', b'"other": 1')
    for body in (_ads_quota("ACCOUNT", 40000), _ads_quota("DEVELOPER", 30),
                 _ads_quota("DEVELOPER", 30).replace(b'"30s"', b'"30.500s"'), no_delay,
                 b'{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}'):
        upstream.answer = (429, body, (b"retry-after", b"3600"))
        for _ in range(2):
            assert (await clients.post(search, json={"query": "SELECT x"})).status_code == 429
    assert await _lock("allowance:google-ads") is None

    upstream.answer = (429, _ads_quota("DEVELOPER", 43762))
    for _ in range(2):
        assert (await clients.post(search, json={"query": "SELECT x"})).status_code == 429
    lock = await _lock("allowance:google-ads")
    assert lock.is_active()
    assert abs((lock.until - lock.opened_at).total_seconds() - 43762) < 5

    upstream.answer = (200, b'{"results": []}')
    r = await clients.post(search, json={"query": "SELECT x"})
    assert r.status_code == 503 and "developer token" in r.json()["detail"]["message"]
    own = await clients.post("/call/google-ads-own/v25/customers/123/googleAds:search",
                             json={"query": "SELECT x"})
    assert own.status_code == 200, "the team's own developer token is never refused"


async def test_observe_logs_what_it_would_refuse_and_off_tracks_nothing(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    channels = "/call/youtube/youtube/v3/channels?part=id&mine=true"

    allowance_mode("observe")
    upstream.answer = (403, YT_QUOTA)
    for _ in range(3):
        assert (await clients.get(channels)).status_code == 403
    assert (await _lock("allowance:youtube")).is_active(), "observe still learns the lock"
    assert len(upstream.calls) == 3, "but refuses nothing"

    allowance_mode("off")
    upstream.answer = (403, YT_QUOTA)
    assert (await clients.get(channels)).status_code == 403
    assert len(upstream.calls) == 4


def test_youtube_resets_at_midnight_pacific_on_either_side_of_daylight_saving():
    summer = signatures._next_pacific_midnight(datetime(2026, 10, 30, 9, 53))
    winter = signatures._next_pacific_midnight(datetime(2026, 11, 2, 9, 0))
    assert summer == datetime(2026, 10, 31, 7, 0)
    assert winter == datetime(2026, 11, 3, 8, 0)
    now = utcnow_naive()
    assert signatures.classify("youtube", 403, None, YT_QUOTA, now=now).resets_at \
        == signatures._next_pacific_midnight(now)


async def test_a_call_probes_only_a_lock_its_answer_can_prove(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    search = "/call/youtube/youtube/v3/search?q=x&part=id"
    channels = "/call/youtube/youtube/v3/channels?part=id&mine=true"
    upstream.answer = (429, YT_SEARCH_QUOTA)
    for _ in range(2):
        await clients.get(search)
    upstream.answer = (403, YT_QUOTA)
    for _ in range(2):
        await clients.get(channels)
    assert (await _lock("allowance:youtube")).is_active()
    assert (await _lock("allowance:youtube/search")).is_active()

    # A costly write cannot prove the cheap calls serve: with the whole allowance locked it never
    # probes.
    capacity_marks._last_probe.clear()
    upstream.answer = (200, b'{"items": []}')
    relayed = len(upstream.calls)
    assert (await clients.put("/call/youtube/youtube/v3/videos?part=id", json={})).status_code == 503
    assert len(upstream.calls) == relayed

    # A cheap call probes the whole allowance; then a search probes its own scope.
    assert (await clients.get(channels)).status_code == 200
    assert await _lock("allowance:youtube") is None
    assert (await clients.get(search)).status_code == 200
    assert await _lock("allowance:youtube/search") is None


async def test_an_unreadable_capacity_view_refuses_nothing(
        clients: AsyncClient, allowance_mode, upstream, monkeypatch):
    from treg.domain.capacity.view import view as capacity_view

    await _connect(clients, "youtube", YT_HOST, provider="youtube")

    async def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(capacity_view, "load", broken)
    assert (await clients.get("/call/youtube/youtube/v3/channels?part=id")).status_code == 200


async def test_an_upload_running_out_locks_only_uploads(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    upload = "/call/youtube/upload/youtube/v3/videos?part=snippet"
    upstream.answer = (403, YT_QUOTA)
    for _ in range(2):
        assert (await clients.post(upload, json={})).status_code == 403
    assert (await _lock("allowance:youtube/uploads")).is_active()
    assert await _lock("allowance:youtube/writes") is None
    upstream.answer = (200, b'{}')
    assert (await clients.post(upload, json={})).status_code == 503
    assert (await clients.put("/call/youtube/youtube/v3/videos?part=id", json={})).status_code == 200


async def test_a_search_held_by_its_own_limit_never_probes_the_whole_allowance(
        clients: AsyncClient, allowance_mode, upstream):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    search = "/call/youtube/youtube/v3/search?q=x&part=id"
    upstream.answer = (429, YT_SEARCH_QUOTA)
    for _ in range(2):
        await clients.get(search)
    upstream.answer = (403, YT_QUOTA)
    for _ in range(2):
        await clients.get("/call/youtube/youtube/v3/channels?part=id")
    capacity_marks._last_probe.clear()
    relayed = len(upstream.calls)
    assert (await clients.get(search)).status_code == 503
    assert len(upstream.calls) == relayed


async def test_a_probe_leaves_a_lock_that_opened_while_it_was_in_flight(
        clients: AsyncClient, allowance_mode, upstream, monkeypatch):
    await _connect(clients, "youtube", YT_HOST, provider="youtube")
    write = "/call/youtube/youtube/v3/videos?part=id"
    upstream.answer = (403, YT_QUOTA)
    for _ in range(2):
        await clients.put(write, json={})
    assert (await _lock("allowance:youtube/writes")).is_active()

    capacity_marks._last_probe.clear()
    upstream.answer = (200, b'{}')
    plain = upstream.relay

    async def whole_locks_meanwhile(*args, **kwargs):
        await capacity_marks.strike("allowance:youtube", endpoint_id=None, kind="quota",
                                    resets_at=None, immediate=True)
        return await plain(*args, **kwargs)
    monkeypatch.setattr(call_service, "relay", whole_locks_meanwhile)
    assert (await clients.put(write, json={})).status_code == 200
    assert await _lock("allowance:youtube/writes") is None, "the probed lock lifts"
    assert (await _lock("allowance:youtube")).is_active(), "the newer lock stands"
