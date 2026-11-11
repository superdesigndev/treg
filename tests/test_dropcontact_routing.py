"""Dropcontact's finished jobs through the ordinary routed call path."""

from __future__ import annotations

import json

import pytest
from sqlmodel import select

from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import AsyncTaskRecord, Hold


@pytest.fixture
def dropcontact_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DROPCONTACT", "PLATFORM-DROPCONTACT")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dropcontact")
    get_settings.cache_clear()
    catalog = catalog_store.load()
    for endpoint_id in ("dropcontact.people.email.find", "dropcontact.people.email.find.linkedin",
                        "dropcontact.people.email.verify"):
        monkeypatch.setitem(catalog.by_id[endpoint_id]["async"], "interval", 0.01)
    yield
    get_settings.cache_clear()


def _relay(monkeypatch, terminal, seen):
    async def relay(request, upstream_url, tool, secrets, client, **kwargs):
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        seen.append((request.method, upstream_url, json.loads(body) if body else None))
        doc = ({"request_id": "dropcontact-job", "success": True, "credits_left": 3999}
               if request.method == "POST" else terminal)
        payload = json.dumps(doc).encode()

        async def stream():
            yield payload

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)


@pytest.mark.parametrize("identity,expected_body,served_by", [
    ({"first_name": "Peter", "last_name": "Jackson", "domain": "company.com"},
     {"data": [{"first_name": "Peter", "last_name": "Jackson", "website": "company.com"}]},
     "dropcontact.people.email.find"),
    ({"linkedin_url": "https://www.linkedin.com/in/example"},
     {"data": [{"linkedin": "https://www.linkedin.com/in/example"}]},
     "dropcontact.people.email.find.linkedin"),
])
async def test_routed_finders_wait_and_charge_only_for_found_email(
    clients, monkeypatch, dropcontact_platform, identity, expected_body, served_by,
):
    seen = []
    _relay(monkeypatch, {"success": True, "data": [{"first_name": "Peter", "last_name": "Jackson",
                    "email": [{"email": "peter.jackson@company.com", "qualification": "nominative@pro"}]}]}, seen)
    result = await clients.post("/call/treg.people.email.find",
        headers={"X-Treg-Route-Prefer": "dropcontact", "X-Treg-Route-Waterfall": "0"}, json=identity)
    assert result.status_code == 200, result.text
    assert result.json()["_treg"]["served_by"] == served_by
    assert result.json()["output"]["email"] == "peter.jackson@company.com"
    assert result.headers["x-treg-cost-micro"] == "34000"
    assert seen[0][2] == expected_body
    assert [call[0] for call in seen] == ["POST", "GET"]


@pytest.mark.parametrize("emails", [[], [{"email": ""}]])
async def test_routed_finder_miss_releases_hold(clients, monkeypatch, dropcontact_platform, emails):
    seen = []
    _relay(monkeypatch, {"success": True, "data": [{"email": emails}]}, seen)
    result = await clients.post("/call/treg.people.email.find",
        headers={"X-Treg-Route-Prefer": "dropcontact", "X-Treg-Route-Waterfall": "0"},
        json={"first_name": "Peter", "last_name": "Jackson", "domain": "company.com"})
    assert result.status_code == 200, result.text
    assert result.json()["_treg"]["outcome"] == "miss"
    assert result.headers["x-treg-cost-micro"] == "0"
    assert [call[0] for call in seen] == ["POST", "GET"]


async def test_routed_pending_job_can_be_read_without_resubmission(clients, monkeypatch, dropcontact_platform):
    seen = []
    _relay(monkeypatch, {"success": True, "data": [{"email": [
        {"email": "person@company.com", "qualification": "nominatif@pro"}]}]}, seen)
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.01)
    headers = {"X-Treg-Route-Prefer": "dropcontact", "X-Treg-Route-Waterfall": "0",
               "Idempotency-Key": "dropcontact-pending-once"}
    identity = {"first_name": "Peter", "last_name": "Jackson", "domain": "company.com"}
    first = await clients.post("/call/treg.people.email.find", headers=headers, json=identity)
    second = await clients.post("/call/treg.people.email.find", headers=headers, json=identity)
    assert first.status_code == second.status_code == 202
    assert [call[0] for call in seen] == ["POST"]
    meta = first.json()["_treg"]
    assert meta["outcome"] == "pending" and meta["charged_micro"] is None
    assert meta["reserved_micro"] == 34_000
    assert meta["async"]["task_id"] == "dropcontact-job"
    assert meta["async"]["poll"]["endpoint"] == "dropcontact.jobs.get"
    assert first.headers["x-treg-reserved-micro"] == "34000"
    assert "x-treg-cost-micro" not in first.headers

    result = await clients.get("/call/dropcontact.jobs.get", params={"request_id": meta["async"]["task_id"]})
    assert result.status_code == 200, result.text
    assert [call[0] for call in seen] == ["POST", "GET"]
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalars().one()
        assert task.status == "settled" and task.settled_micro == 34_000
        assert await db.get(Hold, task.call_id) is None


@pytest.mark.parametrize("qualification,result_email,expected_valid,expected_verdict", [
    ("nominative@pro", "support@dropcontact.com", True, "valid"),
    ("generic@pro", "support@dropcontact.com", True, "valid"),
    ("nominatif@pro", "support@dropcontact.com", True, "valid"),
    ("generique@pro", "support@dropcontact.com", True, "valid"),
    ("nominative@perso", "support@dropcontact.com", True, "valid"),
    ("catch_all@pro", "support@dropcontact.com", False, "catch_all"),
    ("invalid@pro", "", False, "invalid"),
    ("invalide@pro", "", False, "invalid"),
    (None, "", False, "unknown"),
])
async def test_routed_verifier_keeps_billable_verdicts(
    clients, monkeypatch, dropcontact_platform, qualification, result_email, expected_valid, expected_verdict,
):
    seen = []
    _relay(monkeypatch, {"success": True, "data": [{"email": [
        {"email": result_email, "qualification": qualification}]}]}, seen)
    result = await clients.post("/call/treg.people.email.verify",
        headers={"X-Treg-Route-Prefer": "dropcontact", "X-Treg-Route-Waterfall": "0"},
        json={"email": "support@dropcontact.com"})
    assert result.status_code == 200, result.text
    assert result.json()["_treg"]["served_by"] == "dropcontact.people.email.verify"
    assert result.json()["output"] == {"valid": expected_valid, "status": qualification or "unknown"}
    assert result.headers["x-treg-cost-micro"] == "34000"
    assert seen[0][2] == {"data": [{"email": "support@dropcontact.com"}]}
    adapter = catalog_store.load().adapters["dropcontact.people.email.verify"]
    assert adapter.verdict_word(adapter.verdict_raw({"data": [{"email": [
        {"qualification": qualification}]}]})) == expected_verdict


async def test_routed_verifier_without_a_qualification_is_billable_unknown(
    clients, monkeypatch, dropcontact_platform,
):
    seen = []
    _relay(monkeypatch, {"success": True, "data": [{"email": []}]}, seen)
    result = await clients.post("/call/treg.people.email.verify",
        headers={"X-Treg-Route-Prefer": "dropcontact"},
        json={"email": "security@google.com"})
    assert result.status_code == 200, result.text
    assert result.json()["_treg"]["served_by"] == "dropcontact.people.email.verify"
    assert result.json()["_treg"]["outcome"] == "hit"
    assert result.json()["output"] == {"valid": False, "status": "unknown"}
    assert result.headers["x-treg-cost-micro"] == "34000"
    assert [call[0] for call in seen] == ["POST", "GET"]
