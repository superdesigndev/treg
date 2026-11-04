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
    "edge//echo",
    "edge/..%2f..%2forgs/{org}/api-keys",
    "../orgs/{org}/api-keys?x=1",
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
