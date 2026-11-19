"""Official clients surface a resumable upload's Location and never leak cookies."""
import httpx
import pytest

from treg.call_response_headers import (
    attach_selected_upstream_headers,
    selected_upstream_header_lines,
    selected_upstream_headers,
)
from treg.cli import _call_envelope, main

SESSION = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&upload_id=EXAMPLE"
)


def _headers(**extra):
    return httpx.Headers({
        "Location": SESSION,
        "Set-Cookie": "sid=secret",
        "Authorization": "Bearer secret",
        "Cookie": "a=b",
        "WWW-Authenticate": "Bearer realm=x",
        "X-Treg-Call-Id": "abc",
        **extra,
    })


def test_allowlist_keeps_location_and_drops_credentials():
    got = selected_upstream_headers(_headers(**{"Content-Range": "bytes 0-0/*"}))
    assert got == {"location": SESSION, "content-range": "bytes 0-0/*"}
    assert "set-cookie" not in got and "authorization" not in got and "cookie" not in got
    attach = {}
    attach_selected_upstream_headers(attach, _headers())
    assert attach == {"response_headers": {"location": SESSION}}
    attach_selected_upstream_headers(attach, httpx.Headers({"X-Treg-Call-Id": "abc"}))
    assert attach == {"response_headers": {"location": SESSION}}, "absent allowlisted headers leave the dest unchanged"
    assert selected_upstream_headers(httpx.Headers({"Set-Cookie": "sid=secret"})) == {}
    assert selected_upstream_header_lines(_headers()) == [f"treg: Location: {SESSION}"]


def _serve(monkeypatch, response: httpx.Response):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            return response

    monkeypatch.setattr("treg.cli._client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr("treg.cli._load_config", lambda: {"token": "t", "base_url": "http://x"})


def test_json_envelope_includes_location_and_omits_secrets(monkeypatch, capsys):
    response = httpx.Response(200, headers=_headers(), content=b"")
    envelope = _call_envelope(response, "application/json")
    assert envelope["result"] == ""
    assert envelope["_treg"]["response_headers"] == {"location": SESSION}
    assert envelope["_treg"]["call_id"] == "abc"
    blob = str(envelope)
    assert "secret" not in blob and "sid=" not in blob and "Set-Cookie" not in blob

    _serve(monkeypatch, response)
    main(["--json", "call", "youtube.youtube.video.upload"])
    out, err = capsys.readouterr()
    assert err == ""
    import json
    payload = json.loads(out)
    assert payload["_treg"]["response_headers"]["location"] == SESSION
    assert "secret" not in out


def test_plain_call_prints_location_on_stderr_and_keeps_stdout_as_the_body(monkeypatch, capsys):
    _serve(monkeypatch, httpx.Response(200, headers=_headers(), content=b""))
    main(["call", "youtube.youtube.video.upload"])
    out, err = capsys.readouterr()
    assert SESSION not in out
    assert f"treg: Location: {SESSION}" in err
    assert "secret" not in err and "Set-Cookie" not in err and "Authorization" not in err


@pytest.mark.asyncio
async def test_mcp_call_exposes_location_and_drops_credentials(monkeypatch):
    from contextlib import asynccontextmanager

    from treg import mcp as _mcp

    class _Client:
        headers: dict = {}

        async def request(self, method, path, **kwargs):
            return httpx.Response(
                200, headers=_headers(), content=b"",
                request=httpx.Request(method, path),
            )

    @asynccontextmanager
    async def _api(_token, **kwargs):
        yield _Client()

    async def _org(_client):
        return 1, "team", None

    monkeypatch.setattr(_mcp, "_api", _api)
    monkeypatch.setattr(_mcp, "_resolve_org", _org)
    ctx = type("Ctx", (), {"headers": {"authorization": "Bearer test"}})()
    out = await _mcp.call("youtube.youtube.video.upload", params={"uploadType": "resumable"}, ctx=ctx)
    assert out["status"] == 200
    assert out["body"] == ""
    assert out["response_headers"] == {"location": SESSION}
    blob = str(out)
    assert "secret" not in blob and "sid=" not in blob
    # /mcp/v2/ uses the same _call_impl mapper (reviewed against both surfaces).
    directory = await _mcp.directory_catalog_call_write(
        "youtube.youtube.video.upload", params={"uploadType": "resumable"}, ctx=ctx)
    assert directory["response_headers"] == {"location": SESSION}
