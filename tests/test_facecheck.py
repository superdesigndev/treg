"""FaceCheck's native POST protocol stays BYOK, uncached and unmetered."""

import json
import os
import shlex
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from treg.api import app
from treg.domain.catalog import store as catalog_store


@pytest.mark.parametrize("missing_file", [False, True])
def test_catalog_upload_template_builds_real_multipart(tmp_path, monkeypatch, missing_file):
    from treg import cli

    ep = catalog_store.load().by_id["facecheck.web.face.upload"]
    argv = shlex.split(catalog_store.call_template(ep))
    photo = tmp_path / "photo with spaces.jpg"
    if not missing_file:
        photo.write_bytes(b"synthetic-image-bytes\x00\xff")
    argv = [arg.replace("@/path/to/file", f"@{photo}") for arg in argv]
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"id_search": "new-search", "error": None})

    monkeypatch.setattr(cli, "_client", lambda cfg: httpx.Client(
        transport=httpx.MockTransport(handle), base_url="https://registry.example.test"))
    args = cli.build_parser().parse_args(argv[1:])
    if missing_file:
        with pytest.raises(SystemExit, match="--upload file not found"):
            args.fn(args, {})
        assert not seen
        return
    args.fn(args, {})
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/call/facecheck.web.face.upload"
    mime = BytesParser(policy=default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content)
    assert mime.get_content_type() == "multipart/form-data"
    parts = list(mime.iter_parts())
    assert len(parts) == 1
    assert parts[0].get_param("name", header="content-disposition") == "images"
    assert parts[0].get_filename() == photo.name
    assert parts[0].get_payload(decode=True) == photo.read_bytes()


def test_facecheck_reverification_skips_calls_requiring_fresh_inputs(monkeypatch, capsys):
    from scripts import catalog_verify

    monkeypatch.setenv("TREG_CATALOG_CRED", "synthetic-token")
    monkeypatch.setattr("sys.argv", ["catalog_verify.py", "facecheck"])
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.path == "/api/info"
        return httpx.Response(200, json={"remaining_credits": 0, "has_credits_to_search": False,
                                       "is_online": False})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(catalog_verify.httpx, "Client", lambda **kwargs: client)
    assert catalog_verify.main() == 0
    assert len(seen) == 1
    output = capsys.readouterr().out
    for name in ("web.face.upload", "web.face.search", "web.face.image.delete"):
        endpoint = f"facecheck.{name}"
        assert f"SKIP {endpoint}" in output
        assert not catalog_store.load().by_id[endpoint].get("verified")
    assert "PASS facecheck.account.usage" in output


async def test_connect_rejects_http_200_error_and_accepts_zero_balance(clients, monkeypatch):
    def probe(request):
        assert request.method == "POST"
        assert str(request.url) == "https://facecheck.id/api/info"
        if request.headers["authorization"] == "bad-key":
            # Observed on the real API, 2026-09-11. The HTTP status alone is not auth proof.
            return httpx.Response(200, json={"error": "Invalid API token!", "code": "EXCEPTION"})
        assert request.headers["authorization"] == "good-key"
        # Documented schema, not a captured valid-key response. Balance/service state is not auth.
        return httpx.Response(200, json={"remaining_credits": 0, "has_credits_to_search": False,
                                       "is_online": False, "faces": 100000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post("/connections/token", json={"provider": "facecheck", "token": "bad-key"})
        assert bad.status_code == 422, bad.text
        assert "Invalid API token" in bad.text
        assert not (await clients.get("/tools")).json()
        assert not (await clients.get("/connections")).json()
        ok = await clients.post("/connections/token", json={"provider": "facecheck", "token": "good-key"})
        assert ok.status_code == 200, ok.text
        tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "facecheck")
        binding = tool["bindings"][0]
        assert (binding["name"], binding["format"], binding["location"]) == (
            "Authorization", "{secret}", "header")
        # The absolute connection probe must not materialize an incorrect GET /api/info check.
        assert not tool.get("health_check")
        assert tool["examples"][0]["method"] == "POST"
        assert tool["examples"][0]["path"] == "api/info"


def test_catalog_blocks_shared_keys_even_if_pricing_is_later_verified():
    catalog = catalog_store.load()
    endpoints = catalog.for_provider("facecheck")
    assert len(endpoints) == 4
    for endpoint in endpoints:
        assert endpoint["cache"] == "forbidden"
        assert not endpoint.get("async")
        assert endpoint["platform_blocked"]
        assert not catalog.platform_eligible(endpoint)
        priced = {**endpoint, "cost": {"type": "free", "value": 0, "currency": "USD"}}
        assert not catalog.platform_eligible(priced)


@pytest.mark.parametrize("body", [
    b"<html>Maintenance</html>", b"{}", b"[]", b"null",
    b'{"remaining_credits":0,"is_online":false}',
    b'{"remaining_credits":null,"has_credits_to_search":false,"is_online":false}',
])
async def test_incomplete_probe_does_not_create_connection(clients, monkeypatch, body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)
    )) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        result = await clients.post("/connections/token", json={
            "provider": "facecheck", "token": "unverified-test-token"})
        assert result.status_code == 502, result.text
        assert "incomplete verification response" in result.text
        assert "unverified-test-token" not in result.text
        assert not (await clients.get("/connections")).json()
        assert not (await clients.get("/tools")).json()


async def test_failed_reconnect_preserves_working_connection(clients, monkeypatch):
    def reply(request):
        body = (b'{}' if request.headers["authorization"] == "replacement-token" else
                b'{"remaining_credits":0,"has_credits_to_search":false,"is_online":false}')
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        connected = await clients.post("/connections/token", json={
            "provider": "facecheck", "token": "working-token"})
        assert connected.status_code == 200, connected.text
        before = (await clients.get("/connections")).json()
        failed = await clients.post("/connections/token", json={
            "provider": "facecheck", "token": "replacement-token"})
        assert failed.status_code == 502, failed.text
        assert (await clients.get("/connections")).json() == before
        info = await clients.post("/call/facecheck.account.usage")
        assert info.status_code == 200, info.text
        assert info.json()["remaining_credits"] == 0


@pytest.mark.parametrize("via_connection", [False, True])
async def test_byok_upload_search_poll_and_delete_relay_without_metering(clients, monkeypatch, via_connection):
    if via_connection:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "remaining_credits": 0, "has_credits_to_search": False, "is_online": False})
        )) as upstream:
            monkeypatch.setattr(app.state, "http", upstream)
            connected = await clients.post("/connections/token", json={
                "provider": "facecheck", "token": "own-facecheck-token"})
            assert connected.status_code == 200, connected.text
    else:
        await clients.post("/secrets", json={"name": "facecheck", "value": "own-facecheck-token"})
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    before = (await clients.get(f"/orgs/{org_id}/balance")).json()
    seen = []
    responses = [
        {"id_search": "owned-search", "error": None, "input": [{"id_pic": "owned-image"}]},
        {"id_search": "owned-search", "error": None, "progress": 1, "output": None},
        {"id_search": "owned-search", "error": None, "progress": 80, "output": None},
        {"id_search": "owned-search", "error": None, "output": {"items": []}},
        {"error": None, "input": []},
    ]

    def upstream_reply(request):
        assert request.headers["authorization"] == "own-facecheck-token"
        assert "x-treg-token" not in request.headers
        assert request.url.host == "facecheck.id"
        assert request.method == "POST"
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(responses[len(seen) - 1]).encode()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream_reply)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        upload = httpx.Request("POST", "https://example.invalid", files={
            "images": ("photo.jpg", b"synthetic-photo-bytes", "image/jpeg")})
        upload.read()
        result = await clients.post("/call/facecheck.web.face.upload", content=upload.content,
                                    headers={"Content-Type": upload.headers["content-type"]})
        assert result.status_code == 200, result.text
        assert result.json() == responses[0]
        assert seen[0].url.path == "/api/upload_pic"
        assert seen[0].content == upload.content
        assert seen[0].headers["content-type"] == upload.headers["content-type"]

        for i, status_only in enumerate([False, True, True], start=1):
            body = {"id_search": "owned-search", "status_only": status_only,
                    "with_progress": True, "demo": True}
            result = await clients.post("/call/facecheck.web.face.search", json=body)
            assert result.status_code == 200, result.text
            assert result.json() == responses[i]
            assert "X-Treg-Cost-Micro" not in result.headers
            assert seen[i].url.path == "/api/search"
            assert json.loads(seen[i].content) == body

        result = await clients.post("/call/facecheck.web.face.image.delete",
                                    params={"id_search": "owned-search", "id_pic": "owned-image"})
        assert result.status_code == 200, result.text
        assert result.json() == responses[-1]
        assert seen[-1].url.path == "/api/delete_pic"
        assert dict(seen[-1].url.params) == {"id_search": "owned-search", "id_pic": "owned-image"}
    assert len(seen) == 5, "Each status check must reach the provider, not a previous cached reply"
    after = (await clients.get(f"/orgs/{org_id}/balance")).json()
    assert after["balance_micro"] == before["balance_micro"]
    assert after["entries"] == before["entries"], "Own-key calls must create no money entries"


async def test_without_own_key_refuses_before_upstream(clients, monkeypatch):
    def unexpected(request):
        pytest.fail("FaceCheck without an org key must not contact upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        result = await clients.post("/call/facecheck.web.face.search", json={"id_search": "unknown"})
        assert result.status_code == 404, result.text
        assert "no facecheck credential in this org" in result.text


async def test_other_org_cannot_borrow_facecheck_credential(clients, monkeypatch):
    await clients.post("/secrets", json={"name": "facecheck", "value": "first-org-token"})
    other = await clients.post("/orgs", json={"name": "facecheck-other-team"})
    assert other.status_code == 200, other.text

    def unexpected(request):
        pytest.fail("Another org must not call through the first org's FaceCheck credential")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        result = await clients.post("/call/facecheck.web.face.search",
                                    json={"id_search": "first-org-search", "status_only": True},
                                    headers={"X-Treg-Token": other.json()["token"]})
        assert result.status_code == 404, result.text
        assert "no facecheck credential in this org" in result.text


@pytest.mark.skipif(os.environ.get("TREG_FACECHECK_LIVE_PROBE") != "1",
                    reason="Explicit opt-in required for the real FaceCheck bogus-token probe")
async def test_live_bogus_token_is_rejected_by_connection_route(clients, monkeypatch):
    # The registry and throwaway org use the isolated test DB; only the probe reaches the Internet.
    async with httpx.AsyncClient(timeout=30) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        result = await clients.post("/connections/token", json={
            "provider": "facecheck", "token": "treg-deliberately-invalid-probe"})
        assert result.status_code == 422, result.text
        assert "Invalid API token" in result.text
        assert not (await clients.get("/connections")).json()
        assert not (await clients.get("/tools")).json()
