"""The MCP call tools relay to `/call/` and nowhere else.

They reach treg's own API in-process with the caller's credential, so an `endpoint_id` whose dot
segments climb out of `/call/` would turn a tool call into a management request (minting an API
key) made with that credential, including the identity an OAuth grant is exchanged for.
"""

from __future__ import annotations

import pytest

_ESCAPES = [
    "../orgs/{org}/api-keys",
    "%2e%2e/orgs/{org}/api-keys",
    "%2E%2E/orgs/{org}/api-keys",
    ".%2e/orgs/{org}/api-keys",
    "%252e%252e/orgs/{org}/api-keys",
    "edge/../../orgs/{org}/api-keys",
    "edge/%2e%2e/%2e%2e/orgs/{org}/api-keys",
    "edge/./echo",
    "edge/..%2f..%2forgs/{org}/api-keys",
    "../orgs/{org}/api-keys?x=1",
]

# Own-tool paths that carry a percent-encoded URL in one segment (GSC sitemap submit). The encoded
# `%2F` only becomes an empty segment after unquote; that must not be refused — empty mid-path
# segments do not leave `/call/`.
_ENCODED_URL_SEGMENTS = [
    "no-such-tool/webmasters/v3/sites/https%3A%2F%2Fexample.com%2F/sitemaps/https%3A%2F%2Fexample.com%2Fsitemap.xml",
    "google-search-console/webmasters/v3/sites/sc-domain%3Aexample.com/sitemaps/https%3A%2F%2Fwww.example.com%2Fsitemap.xml",
    "tool/sites/x/sitemaps/https%253A%252F%252Fexample.com%252Fsitemap.xml",
]


async def _keys(clients, org: int) -> int:
    r = await clients.get(f"/orgs/{org}/api-keys")
    assert r.status_code == 200, r.text
    return len(r.json())


@pytest.mark.parametrize("escape", _ESCAPES)
async def test_call_endpoint_id_cannot_leave_call(clients, escape):
    from test_mcp_directory import _call_tool, paired_mcp_session

    org = (await clients.get("/auth/me")).json()["org_id"]
    before = await _keys(clients, org)
    endpoint_id = escape.format(org=org)
    async with paired_mcp_session() as session:
        minted = await _call_tool(session, "call",
                                  {"endpoint_id": endpoint_id, "method": "POST",
                                   "body": {"name": "escaped"}},
                                  clients.headers["X-Treg-Token"], path="/mcp/")
        listed = await _call_tool(session, "call", {"endpoint_id": endpoint_id, "method": "GET"},
                                  clients.headers["X-Treg-Token"], path="/mcp/")
        media = await _call_tool(session, "call_media",
                                 {"endpoint_id": endpoint_id, "body": {"name": "escaped"}},
                                 clients.headers["X-Treg-Token"], path="/mcp/")
    assert await _keys(clients, org) == before
    for out in (minted, listed):
        assert "status" not in out and "endpoint_id" in out.get("error", ""), out
    assert "endpoint_id" in str(media), media


def test_call_url_refuses_a_path_outside_its_route():
    import httpx

    from treg.mcp import _call_url

    for route, endpoint_id in (("/call", "../orgs/1/api-keys"), ("/call", "%2e%2e/orgs/1"),
                               ("/catalog/call", "x/../../../orgs/1"), ("/call", "")):
        with pytest.raises(httpx.InvalidURL):
            _call_url(route, endpoint_id)
    assert _call_url("/call", "edge/echo") == "/call/edge/echo"
    assert _call_url("/catalog/call", "dataforseo.x") == "/catalog/call/dataforseo.x"


def test_leaves_route_allows_encoded_url_segments():
    """A single segment may hold an encoded URL; `%2F` must not read as leaving `/call/`."""
    from treg.mcp import _call_url, _leaves_route

    for endpoint_id in _ENCODED_URL_SEGMENTS:
        assert not _leaves_route(endpoint_id), endpoint_id
        assert _call_url("/call", endpoint_id) == f"/call/{endpoint_id}"
    # Mid-path empty stays under the route (literal `//` or encoded slash as its own segment).
    assert not _leaves_route("edge//echo")
    assert not _leaves_route("a/%2f/b")
    assert _call_url("/call", "edge//echo") == "/call/edge//echo"


@pytest.mark.parametrize("endpoint_id", _ENCODED_URL_SEGMENTS)
async def test_call_accepts_encoded_url_segment_then_404s_unknown_tool(clients, endpoint_id):
    """Path check lets the encoded URL through; a missing tool then 404s (no upstream call)."""
    from test_mcp_directory import _call_tool, paired_mcp_session

    async with paired_mcp_session() as session:
        out = await _call_tool(session, "call",
                               {"endpoint_id": endpoint_id, "method": "PUT"},
                               clients.headers["X-Treg-Token"], path="/mcp/")
        media = await _call_tool(session, "call_media",
                                 {"endpoint_id": endpoint_id, "body": {}},
                                 clients.headers["X-Treg-Token"], path="/mcp/")
    # Not the path-segment refusal — the request reached /call/ (unknown tool / 4xx), so a status
    # is present and the path-check error text is absent.
    path_err = "may not contain a '.' or '..' path segment"
    assert path_err not in out.get("error", ""), out
    assert "status" in out, out
    assert path_err not in str(media), media
