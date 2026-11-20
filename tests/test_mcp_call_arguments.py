"""The team MCP `call` tool must not lose what the caller sent.

Two ways it did: an argument the tool does not declare (`data`, the CLI's flag name) was silently
dropped by the argument model, so a POST went out with an empty body and the provider answered
"POST Data Is Empty"; and a non-ASCII `idempotency_key` failed the tool before any request,
because the in-process client's ASCII header block refused the value.
"""

from __future__ import annotations


async def _own_echo_tool(clients) -> None:
    sid = (await clients.post("/secrets", json={"name": "echo-k", "value": "SEK"})).json()["id"]
    r = await clients.post("/tools", json={"name": "edge", "base_url": "https://api.edge.example",
                                           "secret_id": sid})
    assert r.status_code == 200, r.text


async def test_unknown_call_argument_is_refused_by_name(clients):
    from test_mcp_directory import _call_tool, paired_mcp_session

    await _own_echo_tool(clients)
    async with paired_mcp_session() as session:
        out = await _call_tool(session, "call",
                               {"endpoint_id": "edge/echo", "data": {"keywords": ["coffee"]}},
                               clients.headers["X-Treg-Token"], path="/mcp/")
    assert "data" in out["error"], out
    assert "body" in out["hint"] and "params" in out["hint"], out
    assert "status" not in out  # nothing was relayed


async def _twice(clients, key: str) -> tuple[dict, dict]:
    from test_mcp_directory import _call_tool, paired_mcp_session

    async with paired_mcp_session() as session:
        first = await _call_tool(session, "call", {"endpoint_id": "edge/echo", "idempotency_key": key},
                                 clients.headers["X-Treg-Token"], path="/mcp/")
        again = await _call_tool(session, "call", {"endpoint_id": "edge/echo", "idempotency_key": key},
                                 clients.headers["X-Treg-Token"], path="/mcp/")
    return first, again


async def test_non_ascii_idempotency_key_behaves_like_an_ascii_one(clients):
    key = "promoção-検索-1"
    await _own_echo_tool(clients)
    first, again = await _twice(clients, key)
    assert first["status"] == 200 and again["status"] == 200, (first, again)
    # A header value is ASCII: the key travels percent-encoded, the same bytes every time.
    sent = first["body"]["headers"]["idempotency-key"]
    assert sent.isascii() and sent == again["body"]["headers"]["idempotency-key"]
    ascii_first, ascii_again = await _twice(clients, "retry-ascii-" + str(len(key)))
    assert ascii_first["body"]["headers"]["idempotency-key"] == "retry-ascii-" + str(len(key))
    assert again.get("replayed") == ascii_again.get("replayed")


def test_header_key_is_unchanged_when_ascii_and_distinct_when_long():
    from treg.mcp import _header_idempotency_key

    assert _header_idempotency_key("retry-1") == "retry-1"
    assert _header_idempotency_key("x" * 300) == "x" * 200
    a, b = _header_idempotency_key("é" * 100 + "a"), _header_idempotency_key("é" * 100 + "b")
    assert a != b and len(a) <= 200 and a.isascii()


async def test_directory_call_tools_refuse_unknown_arguments_too(clients):
    from test_mcp_directory import _call_tool, paired_mcp_session

    async with paired_mcp_session() as session:
        out = await _call_tool(session, "catalog_call_write",
                               {"endpoint_id": "dataforseo.google.keywords.volume",
                                "data": [{"keywords": ["coffee"]}]},
                               clients.headers["X-Treg-Token"], path="/mcp/v2/")
    assert "data" in out.get("error", ""), out
