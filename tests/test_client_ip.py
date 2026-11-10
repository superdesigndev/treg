"""The client address per-IP limits key on cannot be chosen by the client."""
import logging
from unittest.mock import patch

import pytest

from treg import client_ip
from treg import email as email_sender
from treg.config import get_settings


@pytest.fixture
def ip_config(monkeypatch):
    def configure(header="", hops=1):
        monkeypatch.setattr(get_settings(), "client_ip_header", header)
        monkeypatch.setattr(get_settings(), "trusted_proxy_hops", hops)
        monkeypatch.setattr(client_ip, "_last_missing_log", 0.0)
        monkeypatch.setattr(client_ip, "_shape_reported", False)
    return configure


@pytest.mark.parametrize(("headers", "hops", "expected"), [
    ({}, 1, "10.0.0.9"),                                                          # no proxy header: the peer
    ({"X-Forwarded-For": "203.0.113.5"}, 1, "203.0.113.5"),                       # one proxy appended it
    ({"X-Forwarded-For": "6.6.6.6, 203.0.113.5"}, 1, "203.0.113.5"),              # the forged first entry loses
    ({"X-Forwarded-For": "6.6.6.6, 203.0.113.5, 198.51.100.1"}, 2, "203.0.113.5"),  # two trusted proxies
    ({"X-Forwarded-For": "203.0.113.5"}, 2, "10.0.0.9"),                          # too short for two hops
    ({"X-Forwarded-For": "6.6.6.6"}, 0, "10.0.0.9"),                              # direct clients: ignore it
    ({"X-Forwarded-For": "6.6.6.6, not-an-ip"}, 1, "10.0.0.9"),                   # garbage never becomes a key
    ({"x-forwarded-for": "6.6.6.6, 2001:db8::1"}, 1, "2001:db8::1"),              # any casing, IPv6
])
def test_forwarded_for_is_read_from_the_right(ip_config, headers, hops, expected):
    ip_config(hops=hops)
    assert client_ip.from_headers(headers, "10.0.0.9") == expected


def test_the_edge_header_wins_and_is_validated(ip_config, caplog):
    ip_config(header="cf-connecting-ip")
    forged = {"X-Forwarded-For": "6.6.6.6, 203.0.113.5"}
    assert client_ip.from_headers({**forged, "CF-Connecting-IP": "198.51.100.7"}, "10.0.0.9") == "198.51.100.7"
    with caplog.at_level(logging.WARNING, logger="treg"):
        assert client_ip.from_headers({**forged, "CF-Connecting-IP": "garbage"}, "10.0.0.9") == "203.0.113.5"
        assert client_ip.from_headers(forged, "10.0.0.9") == "203.0.113.5"
    # A missing edge header means the edge is not setting it: say so, but at most once a minute.
    assert caplog.text.count("event=client_ip_header_missing header=cf-connecting-ip") == 1


def test_an_unproxied_request_is_not_a_missing_edge_header(ip_config, caplog):
    """Health checks and in-process relays never cross the edge: no warning, the peer stands."""
    ip_config(header="cf-connecting-ip")
    with caplog.at_level(logging.WARNING, logger="treg"):
        assert client_ip.from_headers({"Authorization": "Bearer x"}, "10.0.0.9") == "10.0.0.9"
    assert "client_ip_header_missing" not in caplog.text


def test_no_peer_and_no_headers_is_a_single_unknown_bucket(ip_config):
    ip_config()
    assert client_ip.from_headers({}, None) == "?"


@pytest.mark.parametrize(("header", "hops"), [("", 1), ("", 2), ("cf-connecting-ip", 1)])
def test_an_in_process_relay_resolves_to_the_same_address(ip_config, header, hops):
    ip_config(header=header, hops=hops)
    forwarded = client_ip.forwarding_headers("203.0.113.5")
    assert client_ip.from_headers(forwarded, "127.0.0.1") == "203.0.113.5"
    assert client_ip.forwarding_headers("6.6.6.6, 1.2.3.4") == {}


async def test_rotating_a_forged_first_hop_does_not_escape_the_per_ip_limit(clients, ip_config):
    """The 2026-10 farm's shape: one proxy egress, a fresh client-written address per request."""
    ip_config()

    async def receive(*_, **__):
        return None

    statuses = []
    with patch.object(email_sender, "send_otp", receive):
        for i in range(31):
            r = await clients.post("/auth/email/start", json={"email": f"rotate{i}@example.org"},
                                   headers={"X-Forwarded-For": f"6.6.{i}.{i}, 203.0.113.5"})
            statuses.append(r.status_code)
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429


def test_the_header_shape_is_reported_once_without_values(ip_config, caplog):
    ip_config()
    with caplog.at_level(logging.INFO, logger="treg"):
        client_ip.from_headers({"CF-Connecting-IP": "198.51.100.7", "X-Forwarded-For": "6.6.6.6, 203.0.113.5"}, None)
        client_ip.from_headers({"X-Real-IP": "1.1.1.1"}, None)
    lines = [r.getMessage() for r in caplog.records if "client_ip_sources" in r.getMessage()]
    assert lines == ["event=client_ip_sources headers=cf-connecting-ip forwarded_for_entries=2"]
    assert "198.51.100.7" not in caplog.text and "203.0.113.5" not in caplog.text


@pytest.mark.parametrize("header", ["", "cf-connecting-ip"])
async def test_every_mcp_internal_call_carries_the_callers_address(clients, ip_config, monkeypatch, header):
    """MCP tools call treg's own API in process; each of those calls must resolve to the MCP
    caller's address, not the transport's loopback peer shared by every MCP user."""
    from test_mcp_directory import _call_tool, paired_mcp_session

    ip_config(header=header)
    seen = []
    original = client_ip.of

    def recording(request):
        resolved = original(request)
        if request.url.path != "/mcp/":
            seen.append(resolved)
        return resolved

    monkeypatch.setattr(client_ip, "of", recording)
    caller = {"CF-Connecting-IP": "203.0.113.9"} if header else {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
    async with paired_mcp_session() as session:
        await _call_tool(session, "catalog_request", {"capability": "weather forecasts for farms"},
                         clients.headers["X-Treg-Token"], caller, path="/mcp/")
    assert seen, "the tool made no internal call"
    assert set(seen) == {"203.0.113.9"}


async def test_bearer_records_the_address_every_internal_call_forwards(ip_config):
    """Every tool calls `_bearer(ctx)` first; `_api` then forwards that caller's address on every
    in-process request, not just the one tool that used to forward it by hand."""
    from types import SimpleNamespace

    from treg import mcp

    ip_config(header="cf-connecting-ip")
    ctx = SimpleNamespace(headers={"authorization": "Bearer tok", "cf-connecting-ip": "203.0.113.9"})
    token = mcp._bearer(ctx)
    async with mcp._api(token) as client:
        assert client.headers["cf-connecting-ip"] == "203.0.113.9"
        assert client.headers["x-forwarded-for"] == "203.0.113.9"
    mcp._bearer(SimpleNamespace(headers=None))  # stdio: no address to forward
    async with mcp._api(token) as client:
        assert "cf-connecting-ip" not in client.headers and "x-forwarded-for" not in client.headers


async def test_concurrent_tool_calls_keep_their_own_address(ip_config):
    import asyncio
    from types import SimpleNamespace

    from treg import mcp

    ip_config(header="cf-connecting-ip")

    async def tool(ip, gate):
        token = mcp._bearer(SimpleNamespace(headers={"authorization": "Bearer t", "cf-connecting-ip": ip}))
        await gate.wait()  # both calls have recorded an address before either forwards one
        async with mcp._api(token) as client:
            return client.headers["cf-connecting-ip"]

    gate = asyncio.Event()
    calls = [asyncio.create_task(tool(ip, gate)) for ip in ("203.0.113.1", "203.0.113.2")]
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(*calls) == ["203.0.113.1", "203.0.113.2"]


@pytest.mark.parametrize("header", ["cf-connecting-ip", "fly-client-ip"])
async def test_mcp_call_never_relays_the_edge_address_upstream(clients, ip_config, header):
    """The address `_api` forwards is for treg's own limiters. Relayed upstream, a Cloudflare-fronted
    provider refuses the request (403 error 1000, "DNS points to prohibited IP"), so every MCP call
    to one would fail."""
    from test_mcp_directory import _call_tool, paired_mcp_session

    ip_config(header=header)
    sid = (await clients.post("/secrets", json={"name": "edge-k", "value": "SEK"})).json()["id"]
    r = await clients.post("/tools", json={"name": "edge", "base_url": "https://api.edge.example",
                                           "secret_id": sid})
    assert r.status_code == 200, r.text
    async with paired_mcp_session() as session:
        out = await _call_tool(session, "call", {"endpoint_id": "edge/echo"},
                               clients.headers["X-Treg-Token"], {header: "203.0.113.9"}, path="/mcp/")
    assert out["status"] == 200, out
    seen = out["body"]["headers"]
    assert header not in seen and "x-forwarded-for" not in seen
    assert "203.0.113.9" not in str(seen)
