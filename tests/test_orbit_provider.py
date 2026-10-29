"""Orbit: key probe, catalog shape (async search/enrich, documented credit prices), and BYOK relay."""

import json
from pathlib import Path

import httpx
import pytest
import yaml

from treg.api import app
from treg import oauth_providers as providers
from treg.domain.catalog import store as catalog_store

PROFILE = "a23ff3b7-b6cc-4ac6-8d4f-0c909cd956f5"  # public figure, level-3 profile used by every test_request


def _orbit_yaml():
    return yaml.safe_load((Path(__file__).parents[1] / "src/treg/catalog/orbit.yaml").read_text())


def test_orbit_catalog_covers_the_v3_surface_with_cheap_test_requests():
    """Cover the four person-context workflows and their two polling utilities."""
    endpoints = _orbit_yaml()["endpoints"]
    assert len(endpoints) == 6
    assert len({(ep["method"], ep["path"]) for ep in endpoints}) == 6
    by_id = {ep["id"]: ep for ep in endpoints}
    assert {ep["id"] for ep in endpoints if ep["kind"] != "utility"} == {
        "orbit.people.search", "orbit.people.profile.read", "orbit.people.enrich", "orbit.people.enrich.batch",
    }
    testable = {eid for eid, ep in by_id.items() if "test_request" in ep}
    assert testable == {
        "orbit.people.search", "orbit.people.profile.read", "orbit.people.enrich",
        "orbit.people.enrich.batch",
    }
    for eid, ep in by_id.items():
        assert "verified" not in ep and "example_response" not in ep, eid  # maintainers stamp those
        assert ("test_request" in ep) != ("untestable" in ep), eid
    # every paid replay targets the same public, already-full profile; the enrich ones are deliberate no-ops
    assert by_id["orbit.people.profile.read"]["test_request"] == {"pathParams": {"profile_id": PROFILE}}
    assert by_id["orbit.people.enrich"]["test_request"]["body"]["operation"] == "partial"
    assert by_id["orbit.people.enrich.batch"]["test_request"]["body"]["profile_ids"] == [PROFILE]
    assert by_id["orbit.people.search"]["test_request"]["body"] == {"query": "Sam Altman", "limit": 1, "include_profile": False}


def test_orbit_search_and_enrich_are_awaitable_through_their_status_utilities():
    cat = catalog_store.load()
    rows = {ep["id"]: ep for ep in cat.for_provider("orbit")}
    search, enrich = rows["orbit.people.search"], rows["orbit.people.enrich"]
    assert search["async"]["poll"]["endpoint"] == "orbit.people.search.status"
    assert search["async"]["id_from"] == "search_id"
    assert set(search["async"]["status"]["success"]) == {"completed", "completed_with_errors"}
    assert enrich["async"]["poll"]["endpoint"] == "orbit.people.enrich.status"
    assert enrich["async"]["status"]["failure"] == ["failed"]
    for status_id, param in (("orbit.people.search.status", "search_id"), ("orbit.people.enrich.status", "request_id")):
        status = rows[status_id]
        assert status["kind"] == "utility" and status["method"] == "GET"
        assert status["resource_ownership"]["requires"] == {"kind": f"poll:{status_id}", "param": param}
        assert status["cost"]["type"] == "free"
    # batch enrich has no parent poll route: children are polled one by one, so no descriptor
    assert not rows["orbit.people.enrich.batch"].get("async")


def test_orbit_prices_come_from_the_rate_card_at_one_cent_per_credit():
    cat = catalog_store.load()
    rows = {ep["id"]: ep for ep in cat.for_provider("orbit")}
    paid = {"orbit.people.search", "orbit.people.profile.read", "orbit.people.enrich", "orbit.people.enrich.batch"}
    for eid, ep in rows.items():
        view = cat.cost_view(ep["cost"], "orbit")
        if eid in paid:
            assert ep["cost"]["source"] == "rate_card_api", eid
            assert ep["cost"]["source_url"] == "https://api.orbitsearch.com/v3/pricing", eid
            assert ep["cost"]["confidence"] == "documented", eid
            assert view["usd"] is not None and view["usd"] > 0, eid
            assert cat.platform_eligible(ep) == (eid == "orbit.people.profile.read"), eid
        else:
            assert ep["cost"]["type"] == "free", eid
            assert view["usd"] == 0, eid
    # 1 credit per 10 cached results -> $0.001 per result; a read is 1 credit -> $0.01
    assert cat.cost_view(rows["orbit.people.search"]["cost"], "orbit")["usd"] == pytest.approx(0.001)
    assert cat.cost_view(rows["orbit.people.profile.read"]["cost"], "orbit")["usd"] == pytest.approx(0.01)
    # Face expansion can reach 100 credits; the base quick/partial/full tiers remain distinct.
    assert cat.cost_view(rows["orbit.people.enrich"]["cost"], "orbit")["usd"] == pytest.approx(1.00)
    assert rows["orbit.people.enrich"]["cost"]["table"][0] == {"when": {"body.face_search": True}, "value": 100}
    assert {item["value"] for item in rows["orbit.people.enrich"]["cost"]["table"]} == {0, 5, 10, 100}
    assert not cat.platform_eligible(rows["orbit.people.search.status"])


async def test_orbit_catalog_serves_the_documented_price_to_clients(clients):
    response = await clients.get("/catalog/endpoints/orbit.people.search")
    assert response.status_code == 200, response.text
    endpoint = response.json()["endpoint"]
    assert endpoint["cost"]["usd"] == pytest.approx(0.001)
    assert endpoint["cost"]["display_usd"] == pytest.approx(0.01)
    assert endpoint["cost"]["display_unit"] == "started 10 cached results"
    assert endpoint["cost"]["confidence"] == "documented"
    assert endpoint["async"]["poll"]["endpoint"] == "orbit.people.search.status"


@pytest.mark.parametrize("endpoint,path,body", [
    ("orbit.people.search", "/v3/search", {
        "intent": {"names": ["Example Person"], "experiences": [{"organization": "Example Company"}]},
        "profile_depth": "quick", "candidate_discovery": True, "candidate_discovery_limit": 1,
        "webhooks": False, "face_search": False,
    }),
    ("orbit.people.enrich", f"/v3/enrich/{PROFILE}", {"operation": "full", "face_search": True}),
    ("orbit.people.enrich.batch", "/v3/enrich", {
        "profile_ids": [PROFILE], "operation": "quick", "include_profile": False,
    }),
])
async def test_orbit_new_request_options_relay_without_modeling_or_platform_charges(clients, monkeypatch, endpoint, path, body):
    """New catalog inputs reach Orbit unchanged; the customer's account owns billing."""
    assert (await clients.post("/secrets", json={"name": "orbit", "value": "orbit-test-placeholder"})).status_code == 200
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer orbit-test-placeholder"
        assert request.url.path == path
        assert json.loads(request.content) == body
        return httpx.Response(200, headers={"Content-Type": "application/json"},
                              stream=httpx.ByteStream(b'{"status":"completed","results":[]}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        params = {"profile_id": PROFILE} if endpoint == "orbit.people.enrich" else {}
        response = await clients.post(f"/call/{endpoint}", params=params, json=body)
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    assert "X-Treg-Cost-Micro" not in response.headers


def test_orbit_variable_cost_work_requires_own_key_and_batch_keeps_child_polling():
    """A four-workflow listing must not advertise unimplemented shared-key settlement or a parent batch poll."""
    cat = catalog_store.load()
    rows = {ep["id"]: ep for ep in cat.for_provider("orbit")}
    for eid, ep in rows.items():
        assert cat.platform_eligible(ep) == (eid == "orbit.people.profile.read"), eid
    assert not rows["orbit.people.enrich.batch"].get("async")
    assert rows["orbit.people.enrich.batch"]["input"]["body"]["profile_ids"]["maxItems"] == 20
    assert not any("watchers" in eid or "webhooks" in eid or "populations" in eid or ".bulk" in eid for eid in rows)


async def test_orbit_own_key_call_relays_the_bearer_and_the_idempotency_key(clients, monkeypatch):
    response = await clients.post("/secrets", json={"name": "orbit", "value": "orbit-test-placeholder"})
    assert response.status_code == 200, response.text
    seen = []

    def upstream(request):
        seen.append(request)
        assert str(request.url) == "https://api.orbitsearch.com/v3/search"
        assert request.headers["Authorization"] == "Bearer orbit-test-placeholder"
        assert json.loads(request.content) == {"query": "Sam Altman", "limit": 1, "include_profile": False}
        return httpx.Response(
            202, headers={"Content-Type": "application/json"},
            stream=httpx.ByteStream(b'{"status":"running","search_id":"example-search","results":[]}'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        response = await clients.post("/call/orbit.people.search",
                                      json={"query": "Sam Altman", "limit": 1, "include_profile": False})
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "running", "search_id": "example-search", "results": []}
    assert len(seen) == 1
    assert "X-Treg-Cost-Micro" not in response.headers  # BYOK: Orbit bills the customer's own account


@pytest.mark.parametrize("status,payload,accepted", [
    (200, {"status": "success", "payload": {"usage_scope": "current_api_key", "credits_used": 0}}, True),
    (403, {"status": "failure", "error": {"code": "invalid_api_key", "message": "Invalid, revoked, expired, or malformed API key"}}, False),
    (401, {"status": "failure", "error": {"code": "missing_api_key"}}, False),
    (200, {"status": "failure", "error": {"code": "something"}}, False),
    (404, {}, False),   # route not deployed yet -> never "connected"
    (429, {"status": "failure"}, False),
    (502, {}, False),
])
async def test_orbit_connect_accepts_only_a_success_envelope_from_the_usage_probe(clients, monkeypatch, status, payload, accepted):
    def upstream(request):
        assert str(request.url) == "https://api.orbitsearch.com/v3/credits/usage"
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer orbit-test-placeholder"
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        response = await clients.post("/connections/token", json={"provider": "orbit", "token": "orbit-test-placeholder"})
    assert response.status_code == (200 if accepted else 422), response.text
    if accepted:
        tools = (await clients.get("/tools")).json()
        orbit = next(tool for tool in tools if tool["name"] == "orbit")
        assert orbit["bindings"][0] == {**orbit["bindings"][0], "name": "Authorization", "format": "Bearer {secret}"}
    assert providers.get("orbit").probe_path == "/v3/credits/usage"


class _FakeClock:
    def __init__(self) -> None:
        self.now, self.sleeps, self.events = 0.0, [], []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def report(self, message: str) -> None:
        self.events.append(message)


def _resp(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://example.test"))


async def test_orbit_search_submission_advertises_its_poll_and_the_status_utility_relays_the_path_param(clients, monkeypatch):
    """The whole BYOK loop an agent runs: submit → X-Treg-Async → poll the status utility with
    `search_id` as a query param that treg renders into `/v3/search/{search_id}` upstream."""
    assert (await clients.post("/secrets", json={"name": "orbit", "value": "orbit-test-placeholder"})).status_code == 200
    upstream_urls = []

    def upstream(request):
        upstream_urls.append((request.method, str(request.url)))
        body = ({"status": "running", "search_id": "srch_123", "results": []} if request.method == "POST"
                else {"status": "completed", "search_id": "srch_123", "results": [{"profile_id": PROFILE, "status": "ready"}]})
        return httpx.Response(202 if request.method == "POST" else 200, headers={"Content-Type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(body).encode()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(app.state, "http", client)
        submitted = await clients.post("/call/orbit.people.search", json={"query": "Sam Altman", "limit": 1, "include_profile": False})
        assert submitted.status_code == 202, submitted.text
        descriptor = json.loads(submitted.headers["X-Treg-Async"])
        assert descriptor["id_from"] == "search_id"
        assert descriptor["poll"] == {"endpoint": "orbit.people.search.status", "param": {"in": "pathParams", "name": "search_id"}}
        polled = await clients.get("/call/orbit.people.search.status?search_id=srch_123")
    assert polled.status_code == 200, polled.text
    assert polled.json()["results"][0]["profile_id"] == PROFILE
    assert upstream_urls == [("POST", "https://api.orbitsearch.com/v3/search"),
                             ("GET", "https://api.orbitsearch.com/v3/search/srch_123")]


def test_treg_await_follows_the_orbit_search_descriptor_to_partial_success():
    """`treg call orbit.people.search --await`: running → completed_with_errors is terminal SUCCESS
    (keep the ready results, inspect the failures), never a re-POST, results returned from `results`."""
    from treg import cli
    cat = catalog_store.load()
    descriptor = next(ep for ep in cat.for_provider("orbit") if ep["id"] == "orbit.people.search")["async"]
    polls = iter([
        _resp(200, {"status": "running", "results": []}),
        _resp(200, {"status": "running", "results": [{"profile_id": PROFILE, "status": "ready"}]}),
        _resp(200, {"status": "completed_with_errors", "results": [{"profile_id": PROFILE, "status": "ready"}],
                    "candidate_discovery_failure": {"code": "clustering_failed"}}),
    ])
    seen = []

    def call_fn(target, params):
        seen.append((target, params))
        return next(polls)

    clock = _FakeClock()
    outcome = cli.await_async_task(descriptor, _resp(202, {"status": "running", "search_id": "srch_9"}), call_fn, clock, 120)
    assert outcome["code"] == 0, outcome
    assert outcome["status"] == "completed_with_errors"
    assert outcome["result"] == [{"profile_id": PROFILE, "status": "ready"}]
    assert outcome["recovery"] == "treg call orbit.people.search.status -p search_id=srch_9"
    assert seen == [("orbit.people.search.status", [("search_id", "srch_9")])] * 3
    assert clock.sleeps == [5.0, 5.0, 5.0]  # the catalog's 5 s interval

    # a failed search is terminal failure, handed back verbatim
    failed = _resp(200, {"status": "failed", "results": []})
    outcome = cli.await_async_task(descriptor, _resp(202, {"status": "running", "search_id": "srch_10"}),
                                   lambda t, p: failed, _FakeClock(), 30)
    assert outcome["code"] == 2 and outcome["response"] is failed
