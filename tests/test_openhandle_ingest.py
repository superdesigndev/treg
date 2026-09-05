import json

import pytest
import yaml

from scripts import catalog_ingest as ingest

SPEC = {"paths": {
    "/v1/instagram/profiles/{identifier}": {"get": {"summary": "Get a profile"}},
    "/v1/instagram/posts/{identifier}/comments/{comment_id}/replies": {"get": {
        "summary": "List replies", "parameters": [
            {"name": "identifier", "in": "path", "required": True, "schema": {"type": "string"},
             "example": "910100000001"},
            {"name": "comment_id", "in": "path", "required": True, "schema": {"type": "string"},
             "example": "910200000001"},
            {"name": "freshness", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "cursor", "in": "query", "required": False, "schema": {"type": "string"}},
        ]}},
    "/v1/tiktok/live/rooms/{identifier}": {"get": {
        "summary": "Get a live room", "parameters": [
            {"name": "identifier", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "user_id", "in": "query", "required": True, "schema": {"type": "string"}},
        ]}},
    "/v1/urls/fetch": {
        "get": {"summary": "Resolve a URL", "parameters": [
            {"name": "url", "in": "query", "required": True, "schema": {"type": "string"}},
        ]},
        "post": {"summary": "Fetch a URL", "requestBody": {"content": {
            "application/json": {"schema": {"type": "object", "required": ["url"],
                "properties": {"url": {"type": "string"},
                               "freshness": {"type": "string", "enum": ["live", "30d"]}}}},
        }}},
    },
    "/v1/test-data": {"get": {"summary": "Search synthetic fixtures"}},
}}


@pytest.fixture
def endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CATALOG", tmp_path)
    (tmp_path / "openhandle.yaml").write_text(yaml.safe_dump({"endpoints": [
        {"method": "GET", "path": "/v1/instagram/profiles/{identifier}"},
    ]}))
    monkeypatch.setattr(ingest, "fetch", lambda *args, **kwargs: json.dumps(SPEC).encode())
    path, _ = ingest.ingest_openhandle(False)
    return {(ep["method"], ep["path"]): ep for ep in yaml.safe_load(path.read_text())["endpoints"]}


def test_openhandle_ingest_keeps_all_methods_and_request_fields_without_duplicating_core(endpoints):
    assert set(endpoints) == {
        ("GET", "/v1/instagram/posts/{identifier}/comments/{comment_id}/replies"),
        ("GET", "/v1/tiktok/live/rooms/{identifier}"),
        ("GET", "/v1/urls/fetch"), ("POST", "/v1/urls/fetch"), ("GET", "/v1/test-data"),
    }
    assert len({ep["id"] for ep in endpoints.values()}) == 5
    post = endpoints[("POST", "/v1/urls/fetch")]
    assert post["input"]["body"]["url"] == {"type": "string", "required": True}
    assert post["input"]["body"]["freshness"]["enum"] == ["live", "30d"]
    assert all("verified" not in ep for ep in endpoints.values())


def test_openhandle_ingest_prices_paid_routes_at_the_published_entry_rate(endpoints):
    paid = endpoints[("POST", "/v1/urls/fetch")]["cost"]
    assert paid["type"] == "per_call"
    assert paid["value"] == 0.003
    assert paid["confidence"] == "documented"
    assert paid["source_url"] == "https://openhandle.dev/pricing.md"
    public = endpoints[("GET", "/v1/test-data")]["cost"]
    assert public["type"] == "free"
    assert public["value"] == 0


def test_openhandle_ingest_builds_live_test_requests_from_real_identifiers_not_synthetic_examples(endpoints):
    replies = endpoints[("GET", "/v1/instagram/posts/{identifier}/comments/{comment_id}/replies")]
    assert replies["test_request"]["pathParams"] == {
        "identifier": "Dc30nJeRKKz", "comment_id": "18470057866113934",
    }
    assert replies["test_request"]["queryParams"] == {"freshness": "30d"}
    post = endpoints[("POST", "/v1/urls/fetch")]
    assert post["test_request"]["body"] == {
        "url": "https://www.instagram.com/instagram/", "freshness": "30d",
    }


def test_openhandle_ingest_leaves_routes_without_a_known_identifier_uncallable(endpoints):
    room = endpoints[("GET", "/v1/tiktok/live/rooms/{identifier}")]
    assert "test_request" not in room
    assert room["unverified"]


def test_openhandle_reingest_keeps_a_verified_stamp_and_drops_the_stale_unverified_note(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CATALOG", tmp_path)
    (tmp_path / "openhandle.yaml").write_text(yaml.safe_dump({"endpoints": []}))
    monkeypatch.setattr(ingest, "fetch", lambda *args, **kwargs: json.dumps(SPEC).encode())
    path, _ = ingest.ingest_openhandle(False)
    first = yaml.safe_load(path.read_text())
    replies = next(ep for ep in first["endpoints"] if ep["path"].endswith("/replies"))
    assert replies["unverified"]
    replies.pop("unverified")
    replies["verified"] = "2026-09-05"
    replies["example_response"] = f"examples/{replies['id']}.json"
    path.write_text(yaml.safe_dump(first, sort_keys=False))

    path, _ = ingest.ingest_openhandle(False)
    again = next(ep for ep in yaml.safe_load(path.read_text())["endpoints"] if ep["path"].endswith("/replies"))
    assert again["verified"] == "2026-09-05"
    assert "unverified" not in again
