"""Wiza provider registration, bounded platform pricing, BYOK and routing."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlmodel import select

from treg import api as A
from treg import oauth_providers as P
from treg.application import asynctasks as async_task_app
from treg.application.call import service as call_service
from treg.application.call import route as call_route
from treg.application.call.types import UpstreamResponse
from treg.config import Settings, get_settings
from treg.domain.capacity import collectors, policy
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, Hold


class _JSONStream(httpx.AsyncByteStream):
    def __init__(self, doc):
        self.body = json.dumps(doc).encode()

    async def __aiter__(self):
        yield self.body


def _response(status: int, doc: dict) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        stream=_JSONStream(doc),
    )


async def _balance(clients) -> int:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


async def _entries(clients) -> list[dict]:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    payload = (await clients.get(f"/orgs/{org_id}/balance")).json()
    return payload["entries"]["items"]


@pytest.fixture
def wiza_platform_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_WIZA", "PLATFORM-WIZA")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "wiza")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_wiza_registry_uses_the_free_credit_probe(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_WIZA", "PLATFORM-WIZA")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "wiza")
    settings = Settings(_env_file=None)
    provider = P.get("wiza")
    assert provider.base_url == "https://wiza.co"
    assert provider.probe_path == "/api/meta/credits"
    assert provider.probe_method == "GET"
    assert settings.platform_key_for("wiza") == "PLATFORM-WIZA"
    assert P.platform_bindings(provider) == [{
        "platform_setting": "platform_key_wiza",
        "injector": "env",
        "location": "header",
        "name": "Authorization",
        "format": "Bearer {secret}",
    }]


async def test_wiza_connection_rejects_bogus_and_accepts_valid_key(clients, monkeypatch):
    def probe(request):
        assert request.url.host == "wiza.co"
        assert request.url.path == "/api/meta/credits"
        token = request.headers["authorization"]
        if token == "Bearer bad":
            return httpx.Response(401, json={"status": {"code": 401, "message": "Invalid API key."}})
        return httpx.Response(200, json={"credits": {"api_credits": 10}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        bad = await clients.post("/connections/token", json={"provider": "wiza", "token": "bad"})
        assert bad.status_code == 422
        good = await clients.post("/connections/token", json={"provider": "wiza", "token": "own-key"})
        assert good.status_code == 200, good.text

    tools = {tool["name"]: tool for tool in (await clients.get("/tools")).json()}
    assert set(tools) == {"wiza"}
    assert tools["wiza"]["base_url"] == "https://wiza.co"


@pytest.mark.parametrize("remaining", [5000, 0, 12.5])
async def test_wiza_capacity_reads_finite_api_credits(remaining):
    def serve(request):
        assert request.url.path == "/api/meta/credits"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"credits": {"api_credits": remaining}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        row = await collectors._wiza(upstream, "test-key")
    assert row["value"] == remaining
    assert row["unit"] == "API credits"
    assert "auto-top-up is not enabled" in row["note"]
    configured = policy.default_policy("wiza", has_key=True)
    assert configured.capacity_type == "credits"
    assert configured.funding_mode == "manual"
    assert configured.auto_funding_enabled is False
    assert configured.rate_limit == {"limit": 30, "window_s": 60, "source": "docs"}


@pytest.mark.parametrize("remaining", [None, True, -1, "unlimited"])
async def test_wiza_capacity_rejects_unclear_balances(remaining):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"credits": {"api_credits": remaining}}))) as upstream:
        with pytest.raises(ValueError, match="valid API credit balance"):
            await collectors._wiza(upstream, "test-key")


async def test_wiza_capacity_rejects_non_finite_balance():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"credits": {"api_credits": float("inf")}}

    class Client:
        async def get(self, *args, **kwargs):
            return Response()

    with pytest.raises(ValueError, match="valid API credit balance"):
        await collectors._wiza(Client(), "test-key")


def test_wiza_catalog_covers_every_official_path_and_bounds_async_platform_use():
    catalog = catalog_store.load()
    rows = [ep for ep in catalog.endpoints if ep["provider"] == "wiza"]
    assert len(rows) == 14
    assert {ep["path"] for ep in rows} == {
        "/api/lists",
        "/api/lists/{id}",
        "/api/lists/{id}/contacts",
        "/api/individual_reveals",
        "/api/individual_reveals/{id}",
        "/api/prospects/search",
        "/api/prospects/create_prospect_list",
        "/api/prospects/continue_search",
        "/api/accounts/search",
        "/api/meta/location_autocomplete",
        "/api/meta/technology_autocomplete",
        "/api/company_enrichments",
    }
    assert "/api/meta/credits" not in {ep["path"] for ep in rows}
    platform = {ep["id"] for ep in rows if catalog.platform_eligible(ep)}
    assert platform == {
        "wiza.people.search",
        "wiza.companies.search",
        "wiza.companies.enrich",
        "wiza.meta.locations.search",
        "wiza.meta.technologies.search",
        "wiza.people.email.find",
        "wiza.people.phone.find",
        "wiza.people.reveal.get",
    }
    assert {eid for eid, adapter in catalog.adapters.items()
            if eid.startswith("wiza.") and adapter.verified} == {
        "wiza.people.search", "wiza.companies.search", "wiza.companies.enrich",
        "wiza.people.email.find", "wiza.people.phone.find",
    }
    assert catalog.by_id["wiza.people.reveal.start"]["cost"]["value"] == 8
    assert catalog.by_id["wiza.people.email.find"]["terminal_example_file"]
    assert catalog.by_id["wiza.people.phone.find"]["terminal_example_file"]
    for endpoint_id in ("wiza.people.email.find", "wiza.people.phone.find"):
        _, body = catalog.adapters[endpoint_id].to_upstream({
            "full_name": "Jane Example", "domain": "example.com",
        })
        assert body["individual_reveal"] == {
            "full_name": "Jane Example", "domain": "example.com",
        }
    assert all(catalog.by_id[eid]["input"]["body"]["size"]["enum"] == [1]
               for eid in ("wiza.people.search", "wiza.companies.search"))
    assert all(catalog.by_id[eid]["input"]["body"]["size"]["required"] is True
               for eid in ("wiza.people.search", "wiza.companies.search"))
    assert all(catalog.by_id[eid]["input"]["body"]["filters"]["required"] is False
               for eid in ("wiza.people.search", "wiza.companies.search"))


async def test_wiza_routed_email_waits_for_terminal_result_and_settles_exact_usage(
    clients, monkeypatch, wiza_platform_on,
):
    calls = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        calls.append((request.method, upstream_url, json.loads(body) if body else None))
        doc = ({"data": {"id": 321, "status": "queued"}}
               if request.method == "POST" else
               {"data": {"id": 321, "status": "finished", "name": "Jane Example",
                         "email": "jane@example.com", "email_status": "valid",
                         "credits": {"api_credits": {"total": 2}}}})
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    result = await clients.post(
        "/call/treg.people.email.find",
        headers={"X-Treg-Route-Prefer": "wiza"},
        json={"full_name": "Jane Example", "domain": "example.com"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["output"] == {
        "email": "jane@example.com", "first_name": "Jane", "last_name": "Example",
        "verified": True,
    }
    assert result.json()["_treg"]["served_by"] == "wiza.people.email.find"
    assert result.headers["x-treg-cost-micro"] == "50000"
    assert await _balance(clients) == before - 50_000
    assert calls[0][2] == {
        "individual_reveal": {"full_name": "Jane Example", "domain": "example.com"},
        "enrichment_level": "partial",
        "email_options": {"accept_work": True, "accept_personal": False, "accept_generic": False},
    }
    assert calls[1][0] == "GET" and calls[1][1].endswith("/api/individual_reveals/321")
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().first()
        assert task.status == "settled" and task.settled_micro == 50_000
        assert await db.get(Hold, task.call_id) is None


async def test_wiza_routed_timeout_is_pending_keeps_hold_and_does_not_resubmit(
    clients, monkeypatch, wiza_platform_on,
):
    submissions = 0

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        nonlocal submissions
        if request.method == "POST":
            submissions += 1
        payload = json.dumps({"data": {"id": 654, "status": "queued"}}).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.01)
    headers = {"X-Treg-Route-Prefer": "wiza", "Idempotency-Key": "wiza-pending-once"}
    first = await clients.post(
        "/call/treg.people.phone.find", headers=headers,
        json={"linkedin_url": "https://www.linkedin.com/in/example"},
    )
    second = await clients.post(
        "/call/treg.people.phone.find", headers=headers,
        json={"linkedin_url": "https://www.linkedin.com/in/example"},
    )
    assert first.status_code == second.status_code == 202
    assert submissions == 1
    meta = first.json()["_treg"]
    assert meta["outcome"] == "pending" and meta["charged_micro"] is None
    assert meta["reserved_micro"] == 150_000 and meta["call_ref"]
    assert "x-treg-cost-micro" not in first.headers
    assert first.headers["x-treg-reserved-micro"] == "150000"
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().first()
        assert task.status == "pending" and task.reserved_micro == 150_000
        assert await db.get(Hold, task.call_id) is not None


async def test_wiza_routed_poll_404_stays_pending_without_paid_fallback(
    clients, monkeypatch, wiza_platform_on,
):
    monkeypatch.setenv("TREG_PLATFORM_KEY_HUNTER", "PLATFORM-HUNTER")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "wiza,hunter")
    get_settings.cache_clear()
    endpoint = catalog_store.load().by_id["wiza.people.email.find"]
    monkeypatch.setitem(endpoint["async"], "interval", 0.01)
    calls = []

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        def response(status, doc):
            payload = json.dumps(doc).encode()

            async def stream():
                yield payload

            async def close():
                return None

            return UpstreamResponse(
                status, ((b"content-type", b"application/json"),), stream(), close)

        provider = "wiza" if "wiza.co" in upstream_url else "hunter"
        calls.append((provider, request.method))
        if provider == "hunter":
            return response(200, {"data": {"email": "fallback@example.com", "score": 90}})
        if request.method == "POST":
            return response(200, {"data": {"id": 655, "status": "queued"}})
        return response(404, {"status": {"code": 404, "message": "Not ready"}})

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    result = await clients.post(
        "/call/treg.people.email.find",
        headers={"X-Treg-Route-Prefer": "wiza"},
        json={"full_name": "Jane Example", "domain": "example.com"},
    )
    assert result.status_code == 202, result.text
    assert result.json()["_treg"]["outcome"] == "pending"
    assert result.json()["_treg"]["charged_micro"] is None
    assert calls == [("wiza", "POST"), ("wiza", "GET")]
    assert await _balance(clients) == before - 75_000

    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().one()
        call_id = task.call_id
        task.next_check_at = task.created_at
        db.add(task)
        await db.commit()
        assert task.status == "pending" and await db.get(Hold, call_id) is not None

    async def terminal_poll(row, client):
        return 200, json.dumps({
            "data": {"id": 655, "status": "finished", "name": "Jane Example",
                     "email": "jane@example.com", "email_status": "valid",
                     "credits": {"api_credits": {"total": 2}}},
        }).encode()

    monkeypatch.setattr(async_task_app, "_poll", terminal_poll)
    settled = await async_task_app.settle_due()
    assert settled.settled == 1
    assert await _balance(clients) == before - 50_000
    async with session_maker() as db:
        task = await db.get(AsyncTaskRecord, call_id)
        assert task.status == "settled" and task.settled_micro == 50_000
        assert await db.get(Hold, call_id) is None


async def test_wiza_failed_reveal_releases_hold_and_is_a_waterfall_miss(
    clients, monkeypatch, wiza_platform_on,
):
    endpoint = catalog_store.load().by_id["wiza.people.email.find"]
    monkeypatch.setitem(endpoint["async"], "interval", 0.01)
    answers = [
        {"data": {"id": 987, "status": "queued"}},
        {"data": {"id": 987, "status": "failed",
                  "credits": {"api_credits": {"total": 0}}}},
    ]

    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        payload = json.dumps(answers.pop(0)).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    before = await _balance(clients)
    result = await clients.post(
        "/call/treg.people.email.find", headers={"X-Treg-Route-Prefer": "wiza"},
        json={"full_name": "Nobody Here", "domain": "example.com"},
    )
    assert result.status_code == 200
    assert result.json()["_treg"]["outcome"] == "miss"
    assert result.headers["x-treg-cost-micro"] == "0"
    assert await _balance(clients) == before
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().first()
        assert task.status == "released" and task.settled_micro == 0
        assert await db.get(Hold, task.call_id) is None


@pytest.mark.parametrize(
    "endpoint,body",
    [
        ("wiza.people.search", {"filters": {"job_title": [{"v": "Founder", "s": "i"}]}}),
        ("wiza.companies.search", {
            "filters": {"company_industry": [{"v": "Software", "s": "i"}]},
        }),
    ],
)
async def test_wiza_platform_search_rejects_an_omitted_required_size(
    clients, wiza_platform_on, endpoint, body,
):
    result = await clients.post(f"/call/{endpoint}", json=body)
    assert result.status_code == 400
    assert result.json()["detail"]["error"] == "catalog_parameter_invalid"


@pytest.mark.parametrize(
    "endpoint,request_body,response_body,expected_micro",
    [
        (
            "wiza.companies.enrich",
            {"company_domain": "example.com"},
            {"type": "company_enrichment", "data": {
                "company_name": "Example", "company_domain": "example.com",
                "credits": {"api_credits": {"total": 2, "company_credits": 2}},
            }},
            50_000,
        ),
        (
            "wiza.people.search",
            {"filters": {"job_title": [{"v": "Founder", "s": "i"}]}, "size": 1},
            {"status": {"code": 200}, "data": {
                "total": 1, "profiles": [{"full_name": "Jane Doe"}],
                "next_page_token": "next",
            }},
            12_500,
        ),
        (
            "wiza.companies.search",
            {"filters": {"company_industry": [{"v": "Software", "s": "i"}]}, "size": 1},
            {"status": {"code": 200}, "data": {
                "total": 1, "companies": [{"name": "Example"}],
                "next_page_token": "next",
            }},
            12_500,
        ),
    ],
)
async def test_wiza_platform_success_settles_the_bounded_price(
    clients, monkeypatch, wiza_platform_on, endpoint, request_body, response_body, expected_micro,
):
    def serve(request):
        assert request.headers["authorization"] == "Bearer PLATFORM-WIZA"
        sent = json.loads(request.content)
        if endpoint.endswith("search"):
            assert sent["size"] == 1
        return _response(200, response_body)

    before = await _balance(clients)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        result = await clients.post(f"/call/{endpoint}", json=request_body)
    assert result.status_code == 200, result.text
    assert result.headers["x-treg-cost-micro"] == str(expected_micro)
    assert before - await _balance(clients) == expected_micro
    entries = await _entries(clients)
    assert [entry["kind"] for entry in entries[:2]] == ["settle", "reserve"]


@pytest.mark.parametrize(
    "endpoint,request_body,status,response_body",
    [
        ("wiza.companies.enrich", {}, 400,
         {"status": {"code": 400, "message": "Company identifier required"}}),
        ("wiza.companies.enrich", {"company_domain": "missing.example"}, 404,
         {"status": {"code": 404, "message": "Company not found"}}),
        ("wiza.people.search", {"filters": {"job_title": [{"v": "Missing", "s": "i"}]}, "size": 1}, 200,
         {"status": {"code": 200}, "data": {"total": 0, "profiles": [], "next_page_token": None}}),
        ("wiza.companies.search", {"filters": {"company_industry": [{"v": "Missing", "s": "i"}]}, "size": 1}, 200,
         {"status": {"code": 200}, "data": {"total": 0, "companies": [], "next_page_token": None}}),
    ],
)
async def test_wiza_platform_misses_and_errors_release_the_hold(
    clients, monkeypatch, wiza_platform_on, endpoint, request_body, status, response_body,
):
    before = await _balance(clients)
    monkeypatch.setattr(call_service, "relay", _fake_relay(status, response_body))
    result = await clients.post(f"/call/{endpoint}", json=request_body)
    assert result.status_code == status
    assert result.headers["x-treg-cost-micro"] == "0"
    assert await _balance(clients) == before
    entries = await _entries(clients)
    expected_close = "settle" if status == 200 else "release"
    assert [entry["kind"] for entry in entries[:2]] == [expected_close, "reserve"]


def _fake_relay(status: int, doc: dict):
    async def relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        async def stream():
            yield json.dumps(doc).encode()

        async def close():
            return None

        return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)

    return relay


async def test_wiza_byok_wins_and_is_not_metered(clients, monkeypatch, wiza_platform_on):
    await clients.post("/secrets", json={"name": "wiza", "value": "OWN-WIZA"})
    seen = []
    polls = 0

    def serve(request):
        nonlocal polls
        seen.append(request.headers["authorization"])
        if request.url.path == "/api/individual_reveals":
            return _response(200, {"data": {"id": 741, "status": "queued"}})
        if request.url.path == "/api/individual_reveals/741":
            polls += 1
            return _response(200, {"data": {"id": 741, "status": "finished",
                "name": "Jane Example", "email": "jane@example.com", "email_status": "valid",
                "credits": {"api_credits": {"total": 3}}}})
        return _response(200, {"type": "company_enrichment", "data": {
            "company_name": "Example", "company_domain": "example.com",
            "credits": {"api_credits": {"total": 2}},
        }})

    before = await _balance(clients)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        result = await clients.post(
            "/call/wiza.companies.enrich", json={"company_domain": "example.com"}
        )
    assert result.status_code == 200, result.text
    assert seen == ["Bearer OWN-WIZA"]
    assert "x-treg-cost-micro" not in result.headers
    assert await _balance(clients) == before

    endpoint = catalog_store.load().by_id["wiza.people.email.find"]
    monkeypatch.setitem(endpoint["async"], "interval", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        routed = await clients.post(
            "/call/treg.people.email.find", headers={"X-Treg-Route-Prefer": "wiza"},
            json={"full_name": "Jane Example", "domain": "example.com"},
        )
    assert routed.status_code == 200 and routed.json()["output"]["email"] == "jane@example.com"
    assert seen == ["Bearer OWN-WIZA"] * 3 and polls == 1
    assert routed.json()["_treg"]["tier"] == "credential"
    assert routed.headers["x-treg-cost-micro"] == "0"
    assert await _balance(clients) == before
    async with session_maker() as db:
        assert not (await db.execute(select(AsyncTaskRecord))).scalars().all()
