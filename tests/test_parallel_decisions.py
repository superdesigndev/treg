"""Parallel Decisions catalog contract and faithful own-key relay (mocked upstream)."""

import json
from pathlib import Path

import httpx
import pytest

from treg import api as A
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store


ENDPOINT = "parallel.ai-judge.decide"
FIXTURE = (Path(__file__).resolve().parents[1] / "src/treg/catalog/examples"
           / f"{ENDPOINT}.json")


class _JSONStream(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    async def __aiter__(self):
        yield self.payload


def _response(status, payload, *, headers=None):
    return httpx.Response(status, headers={"content-type": "application/json", **(headers or {})},
                          stream=_JSONStream(payload))


async def _balance(clients):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]


def test_decisions_catalog_contract():
    catalog = catalog_store.load()
    endpoint = catalog.by_id[ENDPOINT]
    assert endpoint["capability"] == "ai-judge.decide"
    assert (endpoint["method"], endpoint["path"]) == ("POST", "/v1/decisions")
    assert endpoint["input"]["body"]["state"]["type"] == "any"
    assert endpoint["input"]["body"]["questions"]["type"] == "object"
    assert endpoint["input"]["body"]["include_excerpts"]["default"] is True
    assert endpoint["cost"]["value"] == 0.001
    assert endpoint["cost"]["confidence"] == "documented"
    assert not catalog.platform_eligible(endpoint)
    response = json.loads(FIXTURE.read_text())
    assert {"noul", "choice", "score", "refusal"} == {
        answer["type"] for answer in response["answers"].values()
    }


async def test_decisions_is_discoverable(clients):
    response = await clients.get(f"/catalog/endpoints/{ENDPOINT}")
    assert response.status_code == 200
    assert response.json()["endpoint"]["id"] == ENDPOINT
    assert response.json()["endpoint"]["platform_eligible"] is False
    assert response.json()["example_response"]["decision_id"].startswith("decisions_")


@pytest.mark.parametrize("state", [None, "Parallel", {"company": "Parallel"}])
async def test_decisions_single_question_preserves_json_state_and_response(clients, monkeypatch, state):
    secret = await clients.post("/secrets", json={"name": "parallel", "value": "OWN-PARALLEL"})
    assert secret.status_code == 200, secret.text
    request_body = {
        "state": state,
        "questions": {"has_api": {"type": "noul", "instructions": "Does Parallel offer an API?"}},
        "include_excerpts": False,
    }
    reply = json.loads(FIXTURE.read_text())
    seen = []

    def serve(request):
        seen.append((request.url.path, request.headers.get("x-api-key"), json.loads(request.content)))
        return _response(200, reply)

    before = await _balance(clients)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        response = await clients.post(f"/call/{ENDPOINT}", json=request_body)
    assert response.status_code == 200, response.text
    assert response.json() == reply
    assert seen == [("/v1/decisions", "OWN-PARALLEL", request_body)]
    assert "x-treg-cost-micro" not in response.headers
    assert await _balance(clients) == before


async def test_decisions_multiple_question_types_and_citations_are_relayed(clients, monkeypatch):
    await clients.post("/secrets", json={"name": "parallel", "value": "OWN-PARALLEL"})
    body = {
        "state": {"company": "Parallel", "website": "https://parallel.ai"},
        "mode": "fast",
        "questions": {
            "has_api": {"type": "noul", "instructions": "Does Parallel offer an API?",
                        "criteria": {"true": "There is an API.", "false": "There is no API."}},
            "product_type": {"type": "choice", "instructions": "What kind of product is it?",
                             "criteria": {"web_api": "A web API", "desktop_app": "A desktop app"}},
            "documentation_level": {"type": "score", "instructions": "How detailed are the docs?",
                                    "criteria": ["None", "Partial", "Detailed"]},
        },
    }
    reply = json.loads(FIXTURE.read_text())

    def serve(request):
        assert request.headers["x-api-key"] == "OWN-PARALLEL"
        assert json.loads(request.content) == body
        return _response(200, reply)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        response = await clients.post(f"/call/{ENDPOINT}", json=body)
    assert response.status_code == 200, response.text
    assert response.json() == reply
    assert response.json()["answers"]["documentation_level"]["confidence"] == 0.86
    assert response.json()["citations"][0]["excerpts"] == ["Example excerpt."]


@pytest.mark.parametrize("status", [401, 422, 429, 503])
async def test_decisions_upstream_errors_follow_the_relay(clients, monkeypatch, status):
    await clients.post("/secrets", json={"name": "parallel", "value": "OWN-PARALLEL"})
    error = {"type": "error", "error": {"ref_id": "example", "message": "Example error"}}

    def serve(request):
        return _response(status, error, headers={"retry-after": "3"})

    before = await _balance(clients)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        response = await clients.post(f"/call/{ENDPOINT}", json={
            "state": {}, "questions": {"q": {"type": "noul", "instructions": "Is it true?"}},
        })
    assert response.status_code == status, response.text
    assert response.json() == error
    assert await _balance(clients) == before


async def test_decisions_never_uses_platform_key_until_billing_is_verified(clients, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_PARALLEL", "PLATFORM-PARALLEL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "parallel")
    get_settings.cache_clear()
    seen = []
    body = {"state": {}, "questions": {"q": {"type": "noul", "instructions": "Is it true?"}}}

    def serve(request):
        seen.append(request.headers.get("x-api-key"))
        return _response(200, json.loads(FIXTURE.read_text()))

    try:
        before = await _balance(clients)
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as upstream:
            monkeypatch.setattr(A.app.state, "http", upstream)
            missing = await clients.post(f"/call/{ENDPOINT}", json=body)
            assert missing.status_code == 404
            assert seen == []
            assert await _balance(clients) == before

            await clients.post("/secrets", json={"name": "parallel", "value": "OWN-PARALLEL"})
            own = await clients.post(f"/call/{ENDPOINT}", json=body)
            assert own.status_code == 200, own.text
            assert seen == ["OWN-PARALLEL"]
            assert "x-treg-cost-micro" not in own.headers
            assert await _balance(clients) == before
    finally:
        get_settings.cache_clear()
