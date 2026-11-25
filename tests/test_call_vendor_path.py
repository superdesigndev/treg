"""`/call/<provider>/<path>`: a vendor's own path, relative to the provider's base URL, runs the
catalog row on that path exactly as `/call/<id>` does; a team's own tool of that name wins."""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

from treg import api as A
from treg.application.call.resolve import QueryValues, vendor_path_endpoint
from treg.application.call.types import ResolutionFailed
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store

SERP = "dataforseo.x.serp-google-organic-live-advanced"
ANSWER = b'{"status_code":20000,"cost":0.002,"tasks":[{"status_code":20000,"result":[{"items":[{"t":1}]}]}]}'


@pytest.fixture
def dfs(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAFORSEO", "login:password")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dataforseo")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _vendor(monkeypatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(ANSWER), headers={"content-type": "application/json"})

    monkeypatch.setattr(A.app.state, "http", AsyncClient(transport=httpx.MockTransport(upstream)))
    return seen


async def _balance(clients: AsyncClient) -> int:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


async def test_the_vendor_path_runs_the_same_call_as_the_id(clients, dfs, monkeypatch):
    seen = _vendor(monkeypatch)
    body = [{"keyword": "coffee", "location_code": 2840, "language_code": "en"}]
    results = []
    for rest in (SERP, "dataforseo/serp/google/organic/live/advanced"):
        before = await _balance(clients)
        r = await clients.post(f"/call/{rest}", json=body, headers={"Cache-Control": "no-cache"})
        results.append((r.status_code, r.content, r.headers.get("x-treg-cost-micro"),
                        before - await _balance(clients)))
    assert results[0] == results[1]
    assert results[0][0] == 200 and results[0][3] == 2000, results
    assert [str(s.url) for s in seen] == [
        "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"] * 2
    assert seen[0].headers["authorization"] == seen[1].headers["authorization"]


async def test_a_path_segment_fills_the_rows_parameter(clients, dfs, monkeypatch):
    seen = _vendor(monkeypatch)
    r = await clients.get("/call/dataforseo/serp/google/locations/us")
    assert r.status_code == 200, r.text
    assert str(seen[0].url) == "https://api.dataforseo.com/v3/serp/google/locations/us"
    # The path already names the parameter; a second value is refused, not chosen between.
    r = await clients.get("/call/dataforseo/serp/google/locations/us", params={"country": "gb"})
    assert r.status_code == 400 and len(seen) == 1


async def test_no_match_is_a_404_and_reaches_no_vendor(clients, dfs, monkeypatch):
    seen = _vendor(monkeypatch)
    r = await clients.post("/call/dataforseo/v3/serp/google/organic/live/advanced", json=[{}])
    assert r.status_code == 404
    assert "drop the leading '/v3'" in r.json()["detail"]["hint"]
    r = await clients.post("/call/dataforseo/serp/not/a/catalogued/path", json=[{}])
    assert r.status_code == 404 and r.headers.get("x-treg-error") == "1"
    r = await clients.get("/call/dataforseo/serp/google/organic/live/advanced")
    assert r.status_code == 400 and "POST" in r.json()["detail"]
    assert seen == []


async def test_a_teams_own_tool_of_that_name_wins(clients, dfs, monkeypatch):
    seen = _vendor(monkeypatch)
    sid = (await clients.post("/secrets", json={"name": "k", "value": "OWN"})).json()["id"]
    t = await clients.post("/tools", json={"name": "dataforseo", "base_url": "http://upstream/v3",
                                           "secret_id": sid})
    assert t.status_code == 200, t.text
    before = await _balance(clients)
    r = await clients.post("/call/dataforseo/serp/google/organic/live/advanced", json=[{}])
    assert r.status_code == 200, r.text
    assert str(seen[0].url) == "http://upstream/v3/serp/google/organic/live/advanced"
    assert await _balance(clients) == before, "an own tool is never metered"


def _fake_catalog(monkeypatch, *paths: tuple[str, str]) -> None:
    rows = {f"dataforseo.t{i}": {"id": f"dataforseo.t{i}", "provider": "dataforseo",
                                 "method": "GET", "path": path} for i, (_, path) in enumerate(paths)}
    monkeypatch.setattr(catalog_store, "load", lambda **_: catalog_store.Catalog(by_id=rows))


def test_a_literal_segment_beats_a_parameter_and_a_tie_is_refused(monkeypatch):
    _fake_catalog(monkeypatch, ("me", "/users/me"), ("one", "/users/{id}"), ("alt", "/users/{uid}"))
    none = QueryValues(())
    ep, params = vendor_path_endpoint("dataforseo/users/me", "GET", none)
    assert ep["path"] == "/users/me" and params == ()
    with pytest.raises(ResolutionFailed) as refused:
        vendor_path_endpoint("dataforseo/users/42", "GET", none)
    assert refused.value.status_code == 409
    assert refused.value.detail["candidates"] == ["dataforseo.t1", "dataforseo.t2"]
    assert vendor_path_endpoint("not-a-provider/users/me", "GET", none) is None
