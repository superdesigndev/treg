import json

import yaml

from scripts import catalog_ingest as ingest


def test_openhandle_ingest_keeps_all_methods_and_request_fields_without_duplicating_core(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CATALOG", tmp_path)
    (tmp_path / "openhandle.yaml").write_text(yaml.safe_dump({"endpoints": [
        {"method": "GET", "path": "/v1/instagram/profiles/{identifier}"},
    ]}))
    spec = {"paths": {
        "/v1/instagram/profiles/{identifier}": {"get": {"summary": "Get a profile"}},
        "/v1/urls/fetch": {
            "get": {"summary": "Resolve a URL", "parameters": [
                {"name": "url", "in": "query", "required": True,
                 "schema": {"type": "string"}},
            ]},
            "post": {"summary": "Fetch a URL", "requestBody": {"content": {
                "application/json": {"schema": {"type": "object", "required": ["url"],
                    "properties": {"url": {"type": "string"},
                                   "freshness": {"type": "string", "enum": ["live", "30d"]}}}},
            }}},
        },
        "/v1/test-data": {"get": {"summary": "Search synthetic fixtures"}},
    }}
    monkeypatch.setattr(ingest, "fetch", lambda *args, **kwargs: json.dumps(spec).encode())

    path, _ = ingest.ingest_openhandle(False)
    endpoints = yaml.safe_load(path.read_text())["endpoints"]

    assert {(ep["method"], ep["path"]) for ep in endpoints} == {
        ("GET", "/v1/urls/fetch"), ("POST", "/v1/urls/fetch"), ("GET", "/v1/test-data"),
    }
    assert len({ep["id"] for ep in endpoints}) == 3
    post = next(ep for ep in endpoints if ep["method"] == "POST")
    assert post["input"]["body"]["url"] == {"type": "string", "required": True}
    assert post["input"]["body"]["freshness"]["enum"] == ["live", "30d"]
    assert post["cost"]["value"] is None
    public = next(ep for ep in endpoints if ep["path"] == "/v1/test-data")
    assert public["cost"]["type"] == "free"
    assert public["cost"]["value"] == 0
    assert all("verified" not in ep and "test_request" not in ep for ep in endpoints)
