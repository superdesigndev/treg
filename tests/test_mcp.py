"""The MCP front door — that it answers, and that it enforces the same rules as every other door.

Two halves, and the second is the one that matters. A new entrance onto a paid catalog is only safe
if it cannot see another team's data, cannot spend past the caps, and cannot be talked into serving
an endpoint whose price nobody knows. `mcp.py` gets that by routing through treg's own API rather
than reaching into the internals — these tests are what proves the routing is real and not merely
intended.

The transport is exercised as a real MCP client would: JSON-RPC over the mounted ASGI app.
"""

from __future__ import annotations

from conftest import verified_signup, funded_user, mcp_text_result

import json
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient

from treg.api import app


MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


async def _rpc(client: AsyncClient, method: str, params=None, token: str | None = None):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    # An ABSOLUTE url so the Host header is a real one. The transport enforces DNS-rebinding
    # protection, and the suite's base_url ("http://registry") is deliberately not on the
    # allow-list — see test_an_unknown_host_is_refused.
    r = await client.post("http://localhost/mcp/", json=body, headers=headers)
    return r


async def _modern_rpc(client: AsyncClient, method: str, params=None, token: str = "opaque-test-token"):
    body_params = dict(params or {})
    body_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    }
    return await client.post("http://localhost/mcp/", json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": body_params,
    }, headers={
        **MCP_HEADERS,
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": "2026-07-28",
        "MCP-Method": method,
        **({"MCP-Name": body_params["name"]} if "name" in body_params else {}),
    })


async def _call_tool(client: AsyncClient, name: str, args: dict, token: str | None = None) -> dict:
    await _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"}}, token)
    r = await _rpc(client, "tools/call", {"name": name, "arguments": args}, token)
    return mcp_text_result(name, r.json())



@asynccontextmanager
async def mcp_session(client: AsyncClient):
    """Run the MCP lifespan around a block of requests.

    Deliberately NOT a fixture. The lifespan holds an anyio task group, and a fixture enters it in
    one task and exits it in another — anyio refuses that ("Attempted to exit cancel scope in a
    different task"). Entering it inside the test body keeps both ends in the same task.

    That the mounted app needs its lifespan run at all is the trap the module docstring warns about:
    `app.mount()` does not run it, and the transport builds its task group there. It caught me here
    first, which is the cheapest place for it to happen.

    The default `X-Treg-Token` is dropped: MCP carries identity in `Authorization`, and leaving a
    second credential on the client would let a test pass for the wrong reason.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient as _AC

    from treg import mcp as _mcp

    # A FRESH transport per test: `StreamableHTTPSessionManager.run()` may be called once per
    # instance, so the module-level one cannot be restarted between tests. The tools are the same
    # objects either way — they hang off the shared `mcp` server — and they still reach the real
    # `treg.api.app` internally, so the enforcement under test is the production path.
    fresh = _mcp.build_mcp_app()
    host = FastAPI()
    host.mount("/mcp", fresh)
    client.headers.pop("X-Treg-Token", None)
    reader = app.state.endpoint_observation_reader
    _mcp.configure_endpoint_observation_reader(reader)
    try:
        async with _mcp.mcp_lifespan(fresh):
            async with _AC(transport=ASGITransport(app=host), base_url="http://localhost") as mc:
                mc.headers.update({k: v for k, v in client.headers.items() if k.lower() == "cookie"})
                yield mc
    finally:
        _mcp.clear_endpoint_observation_reader(reader)


async def test_the_server_lists_the_shared_tools(clients):
    """The catalog is data reached through a tool, never a tool per endpoint -
    2,600 schemas would bury the model's context and make the catalog unusable."""
    token = (await verified_signup(clients, json={"email": "lister@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}, token)
        r = await _rpc(c, "tools/list", token=token)
        names = {t["name"] for t in r.json()["result"]["tools"]}
    # The hub tools are listed only where the hub is on for the caller (test_hub_tools_are_listed_*).
    assert names == {"catalog_search", "catalog_get", "call", "call_media", "resources_list",
                     "balance", "my_tools", "catalog_request", "feedback", "review", "mark",
                     "compare", "call_result"}


async def test_openai_clients_get_no_review_tool_or_review_instruction(clients):
    """OpenAI's plugin review reads an agent-initiated `review` as analytics collection, so its
    clients (ChatGPT and the reviewer: openai-mcp/, Codex: codex-mcp-client/) get no review tool and
    no review sentence. Every other client keeps both."""
    token = clients.headers["X-Treg-Token"]

    async def surface(ua: str):
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}", "User-Agent": ua}
        async with mcp_session(clients) as c:
            init = await c.post("http://localhost/mcp/", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}})
            listed = await c.post("http://localhost/mcp/", headers=headers,
                                  json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        return (init.json()["result"].get("instructions") or "",
                {t["name"] for t in listed.json()["result"]["tools"]})

    for ua in ("openai-mcp/1.0.0", "codex-mcp-client/0.160.1"):
        instructions, names = await surface(ua)
        assert not {"review", "mark", "compare"} & names and "catalog_search" in names, ua
        assert "review(" not in instructions and "mark(" not in instructions, ua
        assert "catalog_search" in instructions, ua
    instructions, names = await surface("claude-code/2.1.291 (cli)")
    assert {"review", "mark", "compare"} <= names and "review(" in instructions and "mark(" in instructions
    assert "compare(" in instructions


async def _tool_names(clients, token):
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}, token)
        r = await _rpc(c, "tools/list", token=token)
        return {t["name"] for t in r.json()["result"]["tools"]}


async def test_hub_tools_are_listed_only_to_a_team_that_may_use_the_hub(clients, monkeypatch):
    from treg.config import get_settings
    hub = {"hub_create", "hub_update", "hub_mine"}
    token = clients.headers["X-Treg-Token"]
    me = (await clients.get("/orgs")).json()[0]["slug"]
    other = (await verified_signup(clients, json={"email": "hub-outsider@superdesign.dev"})).json()["token"]
    try:
        monkeypatch.setenv("TREG_HUB_ENABLED", "1")
        get_settings.cache_clear()
        assert hub <= await _tool_names(clients, other)                 # open to every team
        monkeypatch.setenv("TREG_HUB_TEAMS", me)
        get_settings.cache_clear()
        assert hub <= await _tool_names(clients, token)                 # a listed team
        assert not hub & await _tool_names(clients, other)              # everyone else
        monkeypatch.setenv("TREG_HUB_TEAMS", "")
        monkeypatch.setenv("TREG_HUB_USERS", "hub-outsider@superdesign.dev")
        get_settings.cache_clear()
        assert hub <= await _tool_names(clients, other)                 # a listed person
        assert not hub & await _tool_names(clients, token)              # not on the person list
    finally:
        monkeypatch.delenv("TREG_HUB_USERS", raising=False)
        monkeypatch.delenv("TREG_HUB_TEAMS", raising=False)
        monkeypatch.delenv("TREG_HUB_ENABLED", raising=False)
        get_settings.cache_clear()


async def test_call_media_returns_native_audio_with_structured_metadata(clients, monkeypatch):
    import base64

    from treg.application.call import service as call_service
    from treg.application.call.types import UpstreamResponse
    from treg.config import get_settings

    monkeypatch.setenv("TREG_PLATFORM_KEY_FISHAUDIO", "PLATFORM-FISH")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "fishaudio")
    get_settings.cache_clear()

    async def relay(*args, **kwargs):
        async def stream():
            yield b"audio-bytes"

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"audio/mpeg"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    token = clients.headers["X-Treg-Token"]
    try:
        async with mcp_session(clients) as c:
            await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "audio", "version": "1"}}, token)
            response = await _rpc(c, "tools/call", {"name": "call_media", "arguments": {
                "endpoint_id": "fishaudio.tts.s2-1-pro",
                "headers": {"model": "s2.1-pro"},
                "body": {"text": "hello"},
            }}, token)
        result = response.json()["result"]
        assert result["content"][0] == {
            "type": "audio", "data": base64.b64encode(b"audio-bytes").decode(),
            "mimeType": "audio/mpeg",
        }
        assert result["structuredContent"]["status"] == 200
        assert result["structuredContent"]["endpoint_id"] == "fishaudio.tts.s2-1-pro"
    finally:
        get_settings.cache_clear()


async def test_call_accepts_base64_multipart_uploads_and_resources_list_reads_the_result(
    clients, monkeypatch,
):
    import base64

    from treg.application.call import service as call_service
    from treg.application.call.types import UpstreamResponse
    from treg.config import get_settings

    monkeypatch.setenv("TREG_PLATFORM_KEY_FISHAUDIO", "PLATFORM-FISH")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "fishaudio")
    get_settings.cache_clear()

    async def relay(*args, **kwargs):
        async def stream():
            yield b'{"_id":"mcp-voice"}'

        async def close():
            return None

        return UpstreamResponse(201, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    token = clients.headers["X-Treg-Token"]
    try:
        async with mcp_session(clients) as c:
            created = await _call_tool(c, "call", {
                "endpoint_id": "fishaudio.voices.create",
                "form": {"type": "tts", "title": "MCP narrator", "train_mode": "fast",
                         "visibility": "private"},
                "uploads": [{"name": "voices", "filename": "voice.wav",
                             "content_type": "audio/wav",
                             "data_base64": base64.b64encode(b"RIFF-test").decode()}],
            }, token=token)
            listed = await _call_tool(c, "resources_list", {
                "provider": "fishaudio", "kind": "voice",
            }, token=token)
        assert created["status"] == 201
        assert listed["count"] == 1
        assert listed["resources"][0]["upstream_id"] == "mcp-voice"
    finally:
        get_settings.cache_clear()


async def test_resources_list_uses_and_normalizes_fish_byok_voices(clients, monkeypatch):
    from treg.application.call import service as call_service
    from treg.application.call.types import UpstreamResponse

    await clients.post("/secrets", json={"name": "fishaudio", "value": "OWN-FISH"})

    async def relay(*args, **kwargs):
        async def stream():
            yield b'{"items":[{"_id":"byok-voice","title":"Account narrator"}],"total":1}'

        async def close():
            return None

        return UpstreamResponse(
            200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        listed = await _call_tool(c, "resources_list", {
            "provider": "fishaudio", "kind": "voice",
        }, token=token)
    assert listed["source"] == "byok"
    assert listed["count"] == 1
    assert listed["resources"][0] == {
        "id": "byok-voice", "provider": "fishaudio", "kind": "voice",
        "upstream_id": "byok-voice", "display_name": "Account narrator",
        "created_by": None, "source_call_id": None,
        "status": "active", "created_at": None, "updated_at": None, "deleted_at": None,
    }


async def test_catalog_search_returns_priced_results(clients):
    """Search is the entry point: an agent asks for a task and gets endpoints with prices. Needs a
    credential now, like every tool - see test_a_protected_tool_answers_401_with_WWW_Authenticate."""
    token = (await verified_signup(clients, json={"email": "searcher@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "backlinks", "limit": 3}, token=token)
    assert out["results"], out
    first = out["results"][0]
    assert first["endpoint_id"] and first["provider"]
    assert "usd_per_call" in first and "no_key_needed" in first


async def test_no_key_needed_is_false_when_the_deploy_holds_no_key(clients):
    """`no_key_needed` must mean "THIS deploy will serve it on treg's key", not "the row is priced".
    The test env configures no platform keys at all, so every result — however eligible its price —
    must say False; anything else re-opens the advertise-then-refuse gap this field had."""
    token = (await verified_signup(clients, json={"email": "keyless@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "backlinks", "limit": 5}, token=token)
    assert out["results"]
    assert all(r["no_key_needed"] is False for r in out["results"]), out["results"]


async def test_search_says_so_when_nothing_matches(clients):
    token = (await verified_signup(clients, json={"email": "nomatch@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "zzzz-no-such-capability"}, token=token)
    assert out["count"] == 0
    assert "catalog_request" in out.get("hint", "")  # the empty result names the way to file the gap

    # And the miss is the record (models.SearchMiss) — this tool reads the catalog in-process, so
    # the HTTP route's logging never sees it; the tool must log its own.
    from sqlmodel import select

    from treg import audit
    from treg.infra.db import session_maker
    from treg.models import SearchMiss

    await audit.drain()
    async with session_maker() as s:
        (row,) = (await s.execute(select(SearchMiss))).scalars()
    assert row.query == "zzzz-no-such-capability"
    assert row.source == "mcp"


async def test_catalog_request_files_the_gap_with_attribution(clients):
    """The zero-result hint's payoff: the agent can file the missing capability in the same session,
    and the stored row says who asked (the bearer), not just that someone did."""
    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import ToolRequest

    token = (await verified_signup(clients, json={"email": "wisher@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_request",
                               {"capability": "Ahrefs backlinks", "note": "for SEO audits"}, token=token)
    assert out.get("status") == "received", out
    async with session_maker() as s:
        (row,) = (await s.execute(select(ToolRequest))).scalars()
    assert row.capability == "Ahrefs backlinks"
    assert row.source == "mcp"
    assert row.user_email == "wisher@superdesign.dev"


# ---- the half that matters: no token means no data, no spending ----------------------------

async def test_a_team_default_token_resolves_its_team(clients):
    """A CLI login with a chosen team receives that team's Default key, so MCP can resolve billing
    without a second X-Treg-Org header."""
    r = await verified_signup(clients, json={"email": "identity-user@superdesign.dev"})
    per_org = r.json()["token"]
    clients.headers["X-Treg-Token"] = per_org
    slug = (await clients.get("/orgs")).json()[0]["slug"]
    identity = (await clients.get(
        "/auth/cli-token", headers={"X-Treg-Org": slug},
    )).json()["token"]
    assert identity == per_org  # deterministic Default key for this membership generation

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=identity)
    assert "balance_usd" in out, out
    assert out.get("team")
    assert out.get("holds_micro") == 0, "summed from the route's holds, which carry no total"



async def test_a_revoked_team_key_names_the_oauth_repair_not_a_missing_team(clients):
    """Pinned dead key: `/orgs` 401 must NOT say the team is gone - the key died. Name the repair.

    `treg mcp install` writes a team-pinned Default key as a static Authorization header. After
    revoke-tokens (or session-secret rotation) that header is still present, so `_resolve_org`
    takes the pinned branch. The old copy answered "team … no longer available"; agents then
    chased the wrong fix.
    """
    token = (await verified_signup(clients, json={"email": "dead-pinned@superdesign.dev"})).json()["token"]
    clients.headers["X-Treg-Token"] = token
    # Prove the live key works first (pinned path succeeds).
    async with mcp_session(clients) as c:
        live = await _call_tool(c, "balance", {}, token=token)
    assert "balance_usd" in live, live

    revoked = await clients.post("/auth/revoke-tokens", headers={"X-Treg-Token": token})
    assert revoked.status_code == 200, revoked.text

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=token)
    assert "no longer available" not in json.dumps(out), out
    assert "not signed in" in out.get("error", ""), out
    assert "treg mcp install --oauth" in out.get("hint", ""), out
    assert "separate from the CLI login" in out.get("hint", ""), out
    assert "treg update" in out.get("hint", ""), out
    assert "TREG_TOKEN" in out.get("hint", ""), out


async def test_one_team_cannot_read_another_teams_tools(clients):
    """Tenant isolation, asserted rather than assumed. Two orgs, two tokens: each sees only its own
    registered tools. This is the property a second front door is most likely to lose."""
    a = (await verified_signup(clients, json={"email": "org-a@superdesign.dev"})).json()["token"]
    b = (await verified_signup(clients, json={"email": "org-b@superdesign.dev"})).json()["token"]
    clients.headers["X-Treg-Token"] = a
    made = await clients.post("/tools", json={"name": "a-only-tool", "base_url": "http://upstream"})
    assert made.status_code == 200, made.text

    async with mcp_session(clients) as c:
        seen_a = await _call_tool(c, "my_tools", {}, token=a)
        seen_b = await _call_tool(c, "my_tools", {}, token=b)
    names_a = {t["name"] for t in seen_a.get("tools", [])}
    names_b = {t["name"] for t in seen_b.get("tools", [])}
    # Not vacuous: org A must genuinely SEE its tool, or "B cannot see it" proves nothing.
    assert names_a, f"org A saw no tools at all — the assertion below would pass for free: {seen_a}"
    assert "a-only-tool" in names_a
    assert "a-only-tool" not in names_b, f"org B saw org A's tool: {seen_b}"
    # Each team also learns its own label for shared provider accounts, and only its own.
    assert seen_a.get("team_label", "").startswith("team-") and seen_b.get("team_label", "").startswith("team-")
    assert seen_a["team_label"] != seen_b["team_label"]


async def test_call_refuses_an_endpoint_that_does_not_exist(clients):
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "nope.not.real"}, token="tok_whatever")
    assert "unknown endpoint" in out.get("error", "")
    assert "catalog_search" in out.get("hint", "")   # names the way out, rather than leaving it guessing


async def test_an_unknown_host_is_refused(clients):
    """The SDK's DNS-rebinding protection, proven live rather than trusted.

    It ships ON with an EMPTY allow-list, which 421s EVERYTHING — a deploy looks healthy until the
    first tool call. `mcp._allowed_hosts()` builds the list from this deployment's `public_url` plus
    the loopback names, so this asserts both directions: a known host works (every other test) and an
    unknown one does not."""
    token = (await verified_signup(clients, json={"email": "host@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        r = await c.post("http://evil.example.com/mcp/", json={"jsonrpc": "2.0", "id": 1,
                         "method": "tools/list"},
                         headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 421, r.text  # a credential passes auth; the HOST guard still refuses


async def test_the_deployments_own_host_is_allowed():
    """The list must contain the host treg actually answers on, or production 421s every call."""
    from urllib.parse import urlsplit

    from treg.config import get_settings
    from treg.mcp import _allowed_hosts

    assert urlsplit(get_settings().public_url).netloc in _allowed_hosts()


# ---- what the plugin directory's review checks ---------------------------------------------

async def test_every_tool_declares_what_it_can_do(clients):
    """The submission portal validates `readOnlyHint`/`openWorldHint`/`destructiveHint` against the
    tool's real behaviour, and a model consults them before acting. The four reading tools change
    nothing; `call` is the honest exception — it relays whatever the caller asks to whichever
    upstream the endpoint names, which can be a POST that publishes, an email that sends or a DELETE.
    treg does not model the upstream, so it cannot promise the call is safe, and saying otherwise
    here would be a false assurance in the exact place it matters."""
    from treg.mcp import mcp as server

    ann = {t.name: t.annotations for t in await server.list_tools()}
    assert set(ann) == {"catalog_search", "catalog_get", "call", "call_media", "resources_list", "balance", "my_tools",
                        "catalog_request", "feedback", "review", "mark", "compare", "hub_create", "hub_update",
                        "hub_mine", "call_result"}
    assert all(a.title is None for a in ann.values())
    for name in ("catalog_search", "catalog_get", "resources_list", "balance", "my_tools", "call_result"):
        a = ann[name]
        assert a and a.read_only_hint is True, name
        assert a.destructive_hint is False and a.open_world_hint is False, name
    for name in ("call", "call_media"):
        a = ann[name]
        assert a.read_only_hint is False
        assert a.destructive_hint is True and a.open_world_hint is True
    # These write nothing upstream and spend nothing, but their text reaches the treg team (and a
    # review's reason may be quoted on a public catalog page), so they are open world.
    for name in ("catalog_request", "feedback", "review", "mark", "compare"):
        a = ann[name]
        assert a.read_only_hint is False, name
        assert a.destructive_hint is False and a.open_world_hint is True, name


async def test_the_domain_challenge_is_404_until_configured(clients):
    """Empty means unset, and unset must 404. An empty 200 would read to the portal as a
    verification that never completes, which is harder to debug than a plain absence."""
    r = await clients.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 404


async def test_the_domain_challenge_returns_the_token_ALONE(clients, monkeypatch):
    """The portal is explicit: that URL must return the token and nothing else — not JSON, not a
    list, not several tokens. Returning a JSON body here is a documented rejection."""
    from treg import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "openai_apps_challenge", "tok-abc-123", raising=False)
    r = await clients.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 200
    assert r.text == "tok-abc-123"
    assert r.headers["content-type"].startswith("text/plain")


async def test_a_BROWSER_origin_is_accepted(clients):
    """`"*"` is not a wildcard in this SDK — origins are compared literally, and only a `:*` port
    suffix is special. Setting `allowed_origins=["*"]` therefore allowed exactly one origin, the
    literal string "*", and refused every browser with "Invalid Origin header".

    Nothing caught it: the suite and every CLI client send NO Origin header, so the check never ran
    until a real web page called /mcp/. This test sends one deliberately."""
    from treg.config import get_settings

    origin = get_settings().public_url.rstrip("/")
    token = (await verified_signup(clients, json={"email": "browser@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "browser", "version": "1"}}},
            headers={**MCP_HEADERS, "Origin": origin, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"a browser at our own origin must be served: {r.text[:120]}"


async def test_an_UNKNOWN_origin_is_still_refused(clients):
    """The protection has to remain real — the fix widens the list, it does not remove the check."""
    token = (await verified_signup(clients, json={"email": "origin@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**MCP_HEADERS, "Origin": "https://attacker.example",
                     "Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text  # a credential passes auth; the ORIGIN guard still refuses


async def test_every_tool_declares_what_it_RETURNS(clients):
    """ChatGPT's connector review flags a tool with no output schema, and a model that has to guess
    at field names guesses. Each tool now declares its shape."""
    from treg.mcp import mcp as server

    for t in await server.list_tools():
        assert t.output_schema, f"{t.name} has no output schema"
        assert t.output_schema.get("properties"), f"{t.name}'s schema is empty"


async def test_the_output_schema_does_not_BREAK_the_error_paths(clients):
    """The load-bearing detail. A strict schema is validated on the way out, so `{"error": "not
    authenticated"}` would RAISE instead of returning — turning a refusal written to tell an agent
    how to recover into an opaque tool failure.

    Every field is optional so both shapes pass. A schema is a hint to the model, not a gate on our
    own error handling."""
    from treg.mcp import mcp as server

    for t in await server.list_tools():
        assert not t.output_schema.get("required"), (
            f"{t.name} has required output fields — the first error response will raise")

    # and prove it end to end, not just in the schema. A BAD token rather than none: a missing
    # credential is now answered at the transport with a 401, so it never reaches the tool — and it
    # is the tool's own error shape that has to survive the schema.
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token="not-a-real-token")
    assert out.get("error"), out
    assert "hint" in out or "detail" in out, "the recovery instruction must survive the schema"


async def test_the_schema_tolerates_NULLS_not_just_missing_keys(clients):
    """`total=False` says a key may be ABSENT; it does not say the value may be null, and nulls reach
    the client from two directions. Real rows carry them — a registered tool with no description, an
    endpoint with no published price. And the SDK serializes every response through the
    TypedDict-derived model, which fills ABSENT keys in as `null` in structuredContent — so a
    response that never mentions `next` still ships `"next": null`, and a strict client validating
    against a `type: string` schema refuses the whole answer with -32602 (issue #93, and a second
    independent report the same day).

    So EVERY field of EVERY tool must allow null, not just the ones known to carry data nulls.
    Asserted on the schema so the next field added is held to the same rule."""
    from treg.mcp import mcp as server

    for t in await server.list_tools():
        schemas = [t.output_schema] + list(t.output_schema.get("$defs", {}).values())
        for schema in schemas:
            for field, spec in schema.get("properties", {}).items():
                # `Any` renders as an unconstrained schema (no type at all) — that accepts null.
                unconstrained = "type" not in spec and "anyOf" not in spec
                allows_null = unconstrained or "null" in str(spec)
                assert allows_null, (f"{t.name}: {schema.get('title')}.{field} does not allow null — "
                                     f"the SDK fills absent keys in as null, so a strict client "
                                     f"will refuse the whole response")


async def test_structured_content_VALIDATES_against_the_advertised_schema(clients):
    """End to end, as the two field reports arrived: a strict client validates structuredContent
    against the outputSchema from tools/list and refuses the response on any mismatch. The schema
    test above checks what we promise; this checks what we actually ship — with jsonschema playing
    the strict client, so a serialization change in the SDK cannot regress this silently."""
    import jsonschema

    from treg.mcp import mcp as server

    schemas = {t.name: t.output_schema for t in await server.list_tools()}
    token = (await verified_signup(clients, json={"email": "strict@superdesign.dev"})).json()["token"]

    async with mcp_session(clients) as c:
        # The exact failing calls from the reports: a search (next set, hint absent) and the two
        # tools whose error shape carries teams/hint — plus a catalog_get error path.
        for tool, args in [("catalog_search", {"query": "backlinks"}),
                           ("catalog_get", {"endpoint_id": "no.such.endpoint"}),
                           # its example_response is an ARRAY of records, which a dict-typed field
                           # refused server-side — the third null/shape mismatch found in the wild
                           ("catalog_get", {"endpoint_id": "brightdata.linkedin.user.profile"}),
                           ("balance", {}),
                           ("my_tools", {})]:
            await _rpc(c, "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "strict", "version": "1"}}, token)
            r = await _rpc(c, "tools/call", {"name": tool, "arguments": args}, token)
            structured = (r.json().get("result") or {}).get("structuredContent")
            assert structured is not None, f"{tool} returned no structuredContent: {r.text[:200]}"
            jsonschema.validate(structured, schemas[tool])  # raises on any mismatch


def test_mcp_gzip_uses_a_fast_level_because_it_runs_on_the_event_loop():
    from starlette.middleware.gzip import GZipMiddleware

    import treg.mcp as mcp_module

    for transport_app in (mcp_module.mcp_app, mcp_module.directory_mcp_app):
        layer = transport_app
        while not isinstance(layer, GZipMiddleware):
            layer = layer.app
        assert layer.compresslevel == mcp_module.MCP_GZIP_LEVEL == 1


async def test_large_mcp_responses_are_gzipped_AT_THE_ORIGIN(clients):
    """The fix that actually works for edge re-compression (issue #100). Render's edge ignored
    `no-transform` and kept Brotli-compressing large responses — which a real client stack
    (httpx + brotlicffi) fails to decode and then hangs on. An edge only compresses what arrives
    UNCOMPRESSED, so the origin gzips its own responses: the edge passes them through, and gzip
    decodes via zlib everywhere.

    Asserted the way the field report arrived: a large catalog_get with `Accept-Encoding: br, gzip`
    (what httpx sends) must come back gzip — OUR encoding — not unencoded (which the edge would
    Brotli) and not br."""
    import json as _json

    token = (await verified_signup(clients, json={"email": "gz@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "gz", "version": "1"}}, token)
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "catalog_get",
                       "arguments": {"endpoint_id": "brightdata.linkedin.user.profile"}}},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}",
                     "Accept-Encoding": "br, gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip", dict(r.headers)
    # httpx transparently gunzips `.content` — that it parses is the end-to-end decode proof,
    # through the exact client library the field reports used.
    body = _json.loads(r.content)
    assert body.get("result"), body

    # And a client that does NOT accept gzip gets identity from us — GZipMiddleware respects
    # Accept-Encoding rather than forcing an encoding the client cannot decode.
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "gz2", "version": "1"}}, token)
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "catalog_get",
                       "arguments": {"endpoint_id": "brightdata.linkedin.user.profile"}}},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}",
                     "Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers, dict(r.headers)


async def test_mcp_responses_forbid_edge_TRANSFORMS(clients):
    """Production sits behind Render's Cloudflare edge, which Brotli-compresses responses unless the
    origin says not to. At least one real client stack (httpx + brotlicffi, issue #93) dies decoding
    large compressed bodies and then hangs to its own timeout. `Cache-Control: no-transform` is the
    origin's standard "do not re-encode" (RFC 9111), and Cloudflare honours it; `no-store` rides
    along because these responses are per-caller and priced.

    Asserted on both an authenticated answer and the 401 challenge — the header wrapper is outermost
    precisely so the challenge path carries it too."""
    token = (await verified_signup(clients, json={"email": "edge@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        challenged = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=MCP_HEADERS)
        answered = await _rpc(c, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "edge", "version": "1"}}, token)
    assert challenged.status_code == 401
    for r in (challenged, answered):
        assert r.headers.get("cache-control") == "no-store, no-transform", dict(r.headers)


# ---- refusing in the right SHAPE, not just refusing ------------------------------------------

@pytest.mark.parametrize("tool", ["call", "balance", "my_tools", "catalog_search", "catalog_get"])
async def test_a_protected_tool_answers_401_with_WWW_Authenticate(clients, tool):
    """The spec has a protected resource reply 401 with `WWW-Authenticate: Bearer
    resource_metadata="…"`, because that header is how a client DISCOVERS it must authenticate and
    where to start. A friendly sentence inside a 200 tells a human what happened and tells a program
    nothing.

    This passed unnoticed because ChatGPT authenticates up front. A client that connects first and
    discovers auth lazily — which the spec allows — would read 200 as success."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": {}}}, headers=MCP_HEADERS)
    assert r.status_code == 401, r.text
    challenge = r.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer "), challenge
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource" in challenge
    assert r.json()["resource_metadata"].endswith("/.well-known/oauth-protected-resource")


async def test_EAGER_auth_challenges_initialize_itself(clients):
    """Eager, not lazy. Every treg tool needs auth, so there is nothing to browse anonymously — and
    the spec's canonical flow challenges the client's FIRST request (`initialize`) so OAuth runs
    before the session proceeds. Leaving it open was why a client showed "Connected" and never
    prompted. So `initialize` and `tools/list` without a credential are 401 + WWW-Authenticate, same
    as a tool call."""
    async with mcp_session(clients) as c:
        for method in ("initialize", "tools/list"):
            params = {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "t", "version": "1"}} if method == "initialize" else None
            body = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params:
                body["params"] = params
            r = await c.post("http://localhost/mcp/", json=body, headers=MCP_HEADERS)
            assert r.status_code == 401, f"{method}: {r.status_code}"
            challenge = r.headers.get("www-authenticate", "")
            assert "resource_metadata=" in challenge and 'scope="' in challenge, challenge


async def test_a_notification_and_ping_pass_without_a_token(clients):
    """Not everything is challenged: a JSON-RPC notification carries no id and expects no response,
    and `ping` is the liveness check — 401ing either would be a challenge nobody can act on. Only
    id-bearing requests (initialize, tools/*, …) need a credential."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", headers=MCP_HEADERS,
                         json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202, r.text
        r = await c.post("http://localhost/mcp/", headers=MCP_HEADERS,
                         json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 200, r.text


async def test_a_non_POST_is_challenged_like_a_POST_then_refused_not_streamed(clients):
    """Issue #1064: a GET probe used to open an SSE stream that never answered."""
    from treg.domain.identity import mcp_oauth
    dead = mcp_oauth.make_access_token(user_id=7, org_id=3, audience=mcp_oauth.mcp_resource_url(),
                                       scope="treg:call", ttl=-60)
    live = mcp_oauth.make_access_token(user_id=7, org_id=3, audience=mcp_oauth.mcp_resource_url(),
                                       scope="treg:call")
    sse = {"Accept": "text/event-stream"}
    async with mcp_session(clients) as c:
        missing = await c.get("http://localhost/mcp/", headers=sse)
        expired = await c.get("http://localhost/mcp/",
                              headers={**sse, "Authorization": f"Bearer {dead}"})
        credentialed = [await c.get("http://localhost/mcp/",
                                    headers={**sse, "Authorization": f"Bearer {token}"})
                        for token in (live, "treg_per_org_token")]
        delete = await c.delete("http://localhost/mcp/")
        head = await c.head("http://localhost/mcp/", headers={"Authorization": f"Bearer {live}"})
    assert missing.status_code == 401, missing.text
    assert "resource_metadata=" in missing.headers["www-authenticate"]
    assert 'error="invalid_token"' not in missing.headers["www-authenticate"]
    assert expired.status_code == 401, expired.text
    assert 'error="invalid_token"' in expired.headers["www-authenticate"]
    for r in credentialed:
        assert r.status_code == 405, r.text
        assert r.headers["allow"] == "POST"
        assert r.headers["content-type"] == "application/json"
    assert delete.status_code == 401
    assert head.status_code == 405 and head.headers["allow"] == "POST"


async def test_auth_body_replay_waits_for_the_real_disconnect_on_a_long_response() -> None:
    """A completed request body is not a disconnect; long responses keep the live receive channel."""
    from treg.mcp import RequireAuthForProtectedTools

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    original_calls = 0
    downstream_received = []
    sent = []

    async def receive():
        nonlocal original_calls
        original_calls += 1
        if original_calls == 1:
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect", "real": True}

    async def send(message):
        sent.append(message)

    async def long_response(scope, receive, send):
        downstream_received.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"still open", "more_body": True})
        downstream_received.append(await receive())

    scope = {"type": "http", "method": "POST", "headers": []}
    await RequireAuthForProtectedTools(long_response)(scope, receive, send)

    assert original_calls == 2
    assert downstream_received == [
        {"type": "http.request", "body": body, "more_body": False},
        {"type": "http.disconnect", "real": True},
    ]
    assert sent[0] == {"type": "http.response.start", "status": 200, "headers": []}


async def test_discover_disables_unused_change_capabilities(clients) -> None:
    """A fixed tool surface must not make clients open an idle subscription stream."""
    async with mcp_session(clients) as c:
        r = await _modern_rpc(c, "server/discover")

    assert r.status_code == 200, r.text
    capabilities = r.json()["result"]["capabilities"]
    assert capabilities["tools"]["listChanged"] is False
    assert capabilities["prompts"]["listChanged"] is False
    assert capabilities["resources"]["listChanged"] is False
    assert capabilities["resources"]["subscribe"] is False


async def test_subscription_listen_is_not_served(clients) -> None:
    """SDK 2.0 registers listen by default; treg refuses it through the public middleware seam."""
    async with mcp_session(clients) as c:
        r = await _modern_rpc(c, "subscriptions/listen", {
            "notifications": {"toolsListChanged": True},
        })

    assert r.status_code == 404, r.text
    assert r.json()["error"] == {
        "code": -32601, "message": "Method not found", "data": "subscriptions/listen",
    }


async def test_auth_middleware_preserves_a_real_mid_body_disconnect() -> None:
    """Authentication inspection replays each partial request message and the real disconnect."""
    from treg.mcp import RequireAuthForProtectedTools

    received = []

    async def downstream(scope, receive, send):
        received.append(await receive())
        received.append(await receive())
        received.append(await receive())

    messages = [
        {"type": "http.request", "body": b'{"jsonrpc":', "more_body": True},
        {"type": "http.request", "body": b'"2.0"', "more_body": True},
        {"type": "http.disconnect", "real": True},
    ]

    async def receive():
        return messages.pop(0)

    scope = {"type": "http", "method": "POST", "headers": []}
    await RequireAuthForProtectedTools(downstream)(scope, receive, lambda message: None)

    assert received == [
        {"type": "http.request", "body": b'{"jsonrpc":', "more_body": True},
        {"type": "http.request", "body": b'"2.0"', "more_body": True},
        {"type": "http.disconnect", "real": True},
    ]


async def test_a_BAD_token_is_the_tool_s_business_not_the_transport_s(clients):
    """The challenge fires only when there is NO credential. Deciding whether a token is valid needs
    the database, and doing that in transport middleware would put a second authentication
    implementation in front of the first. A garbage bearer still answers 200 at the transport and
    the tool names the OAuth repair command (MCP grant ≠ CLI login)."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}},
            headers={**MCP_HEADERS, "Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 200
    body = json.loads(r.json()["result"]["content"][0]["text"])
    assert "error" in body
    assert "treg mcp install --oauth" in body.get("hint", ""), body


async def test_call_accepts_an_ARRAY_body(clients):
    """DataForSEO — the largest provider in the catalog at 217 endpoints — takes an ARRAY of task
    objects on every one of its `live` POST routes. A dict-only `params` made all of them uncallable
    with a pydantic type error, which reads as "you passed it wrong" rather than "this tool cannot
    express that".

    Found by trying a real call for the demo, not by reading the signature."""
    token = (await verified_signup(clients, json={"email": "arraybody@superdesign.dev"})).json()["token"]
    # register the tool AS THAT TOKEN's org — a tool created in another org is correctly invisible
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = token
    made = await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})
    if prev:
        clients.headers["X-Treg-Token"] = prev
    assert made.status_code == 200, made.text

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/anything", "method": "POST",
            "params": [{"keyword": "payment api", "depth": 10}]}, token=token)
    assert out.get("status") == 200, out
    assert "validation error" not in json.dumps(out)


async def test_a_list_is_refused_for_a_GET_with_a_clear_reason(clients):
    """A query string is key/value pairs, so a list has no meaning there. Saying so beats a pydantic
    trace that blames the caller for the tool's own limitation."""
    token = (await verified_signup(clients, json={"email": "listget@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "hunter.people.email.find", "params": [{"x": 1}]}, token=token)
    assert "must be an object, not a list" in json.dumps(out), out


@pytest.mark.parametrize("public_url", ["https://treg.ngrok.app", "http://127.0.0.1:18790",
                                        "https://anything.example"])
async def test_a_402_THROUGH_THE_CALL_TOOL_carries_no_link(clients, monkeypatch, public_url):
    """Drain the balance, call a metered endpoint through the MCP tool, and assert on what comes
    back: no link out, whichever host this deployment runs as, and the diagnosis intact. An agent
    still needs to know it ran out of money, how short it was, and that its own key is an
    alternative; otherwise the refusal is indistinguishable from a broken endpoint."""
    from sqlmodel import select

    from treg.config import get_settings
    from treg.infra.db import session_maker
    from treg.models import Org

    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATKEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    monkeypatch.setenv("TREG_PUBLIC_URL", public_url)
    get_settings.cache_clear()

    token = (await verified_signup(clients, json={"email": "broke402@superdesign.dev"})).json()["token"]
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = token
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    if prev:
        clients.headers["X-Treg-Token"] = prev
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == org_id))).scalar_one()
        org.balance_micro = 0            # spent out
        db.add(org)
        await db.commit()

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "tikhub.tiktok.video.comments",
            "params": {"aweme_id": "7"}}, token=token)
    get_settings.cache_clear()

    assert out.get("status") == 402, out
    blob = json.dumps(out)
    assert "http://" not in blob and "https://" not in blob, f"a link out survived: {blob}"
    assert "topup_url" not in blob, blob
    assert "not enough for this call" in blob, "the diagnosis must survive"
    detail = out["body"]["detail"]
    assert detail["error"] == "insufficient_balance" and "would cost" in detail["message"]
    assert "connections connect" in detail["message"], "the own-key alternative must survive"
    assert detail["balance_micro"] == 0 and detail["estimated_cost_micro"] > 0


async def test_a_402_THROUGH_CALL_MEDIA_carries_no_link_either(clients, monkeypatch):
    """`call_media` relays its own error body; a balance refusal there must not link out either."""
    from sqlmodel import select

    from treg.config import get_settings
    from treg.infra.db import session_maker
    from treg.models import Org

    monkeypatch.setenv("TREG_PLATFORM_KEY_MINIMAX", "PLATKEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "minimax")
    get_settings.cache_clear()
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    token = clients.headers["X-Treg-Token"]
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == org_id))).scalar_one()
        org.balance_micro = 0
        db.add(org)
        await db.commit()
    try:
        async with mcp_session(clients) as c:
            out = await _call_tool(c, "call_media", {
                "endpoint_id": "minimax.voice-gen.speech-2-8-turbo",
                "body": {"model": "speech-2.8-turbo", "text": "hello", "stream": False,
                         "output_format": "url", "voice_setting": {"voice_id": "English_expressive_narrator"}}},
                token=token)
    finally:
        get_settings.cache_clear()
    assert out.get("status") == 402, out
    blob = json.dumps(out)
    assert "http://" not in blob and "https://" not in blob and "topup_url" not in blob, blob


# ---- the expired-token challenge: what makes silent refresh possible -------------------------

async def test_an_EXPIRED_access_token_gets_401_invalid_token(clients):
    """RFC 6750 §3.1. A client whose access token expired presents it anyway; the resource answers
    401 with `error="invalid_token"`, and THAT is the cue on which the client silently runs its
    refresh grant. Our first challenge only covered the missing-header case, so an expired token
    sailed through to the tool's friendly prose in a 200 — and Claude Code, told nothing, gave up
    with "requires re-authorization" instead of refreshing."""
    from treg.domain.identity import mcp_oauth
    dead = mcp_oauth.make_access_token(user_id=7, org_id=3, audience=mcp_oauth.mcp_resource_url(),
                                       scope="treg:call", ttl=-60)  # born expired
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {dead}"})
    assert r.status_code == 401, r.text
    challenge = r.headers.get("www-authenticate", "")
    assert 'error="invalid_token"' in challenge, challenge
    assert "resource_metadata=" in challenge
    assert r.json()["error"] == "invalid_token"


async def test_a_wrong_audience_access_token_gets_401_invalid_token(clients):
    """A grant consented to a DIFFERENT resource must not be honoured here — and the refusal should
    still be the machine-readable 401, not tool prose."""
    from treg.domain.identity import mcp_oauth
    other = mcp_oauth.make_access_token(user_id=7, org_id=3,
                                        audience="https://evil.example/mcp/", scope="treg:call")
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {other}"})
    assert r.status_code == 401, r.text
    assert 'error="invalid_token"' in r.headers.get("www-authenticate", "")


async def test_the_same_key_through_MCP_bills_once(clients, monkeypatch):
    """End to end on the agent path: an agent retries with the same key, the provider is reached
    once, and the balance moves once."""
    from sqlmodel import select

    from treg.config import get_settings
    from treg.infra.db import session_maker
    from treg.models import Org

    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATKEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()

    token = (await verified_signup(clients, json={"email": "mcponce@superdesign.dev"})).json()["token"]
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = token
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    if prev:
        clients.headers["X-Treg-Token"] = prev

    async def balance() -> int:
        async with session_maker() as db:
            org = (await db.execute(select(Org).where(Org.id == org_id))).scalar_one()
            return org.balance_micro or 0

    args = {"endpoint_id": "tikhub.tiktok.video.comments", "params": {"aweme_id": "7"},
            "idempotency_key": "one-piece-of-work"}
    before = await balance()
    async with mcp_session(clients) as c:
        first = await _call_tool(c, "call", args, token=token)
    assert first.get("status") == 200, first
    charged = before - await balance()
    assert charged > 0, "the first call must bill"

    after_first = await balance()
    async with mcp_session(clients) as c:
        second = await _call_tool(c, "call", args, token=token)
    get_settings.cache_clear()

    assert second.get("status") == 200, second
    assert second.get("replayed") is True, "the retry must be marked as a replay"
    assert second.get("body") == first.get("body"), "and return the same answer"
    assert await balance() == after_first, "and bill nothing"


# ---------------------------------------------------------------------------------------------
# `call` request-shape parity with the CLI (`treg call`)
#
# Every flag on `treg call` exists because a real endpoint needed it — --query alongside --data
# (Bright Data's ?dataset_id=… + array body), --header (Google Ads' login-customer-id), raw
# non-JSON bodies, repeated query keys, inline ?a=b in a passthrough URL that httpx silently
# drops. The MCP tool must express the same shapes or a class of endpoints is CLI-only.
# The team-tool "echo" relays to the conftest upstream, which reports exactly what arrived.
# ---------------------------------------------------------------------------------------------

async def _register_echo(clients) -> str:
    made = await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})
    assert made.status_code == 200, made.text
    return clients.headers.get("X-Treg-Token")


async def test_call_sends_query_AND_body_together(clients):
    """The Bright Data shape: a POST whose routing lives in the query string and whose input is
    the body. `params` alone cannot say both — `query` + `body` can."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/trigger", "method": "POST",
            "query": {"dataset_id": "gd_123", "format": "json"},
            "body": [{"url": "https://example.com/in/someone"}]}, token=token)
    assert out.get("status") == 200, out
    seen = json.loads(out["body"]) if isinstance(out.get("body"), str) else out["body"]
    assert dict(seen["query_multi"])["dataset_id"] == "gd_123"
    assert json.loads(seen["body"]) == [{"url": "https://example.com/in/someone"}]


async def test_call_relays_extra_headers_but_never_tregs_own(clients):
    """--header parity (login-customer-id is the canonical need). treg's own auth/routing headers
    are filtered from the relay: the MCP bearer IS the identity and must not be maskable."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/ads",
            "headers": {"login-customer-id": "1234567890", "X-Treg-Token": "evil",
                        "Authorization": "Bearer evil"}}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    assert seen["headers"].get("login-customer-id") == "1234567890"
    assert seen["headers"].get("x-treg-token") != "evil"


async def test_call_sends_a_raw_string_body_with_content_type(clients):
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/xml", "method": "POST",
            "body": "<xml>hi</xml>", "content_type": "application/xml"}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    assert seen["body"] == "<xml>hi</xml>"
    assert seen["headers"].get("content-type") == "application/xml"


async def test_call_string_body_sniffs_json_and_implies_post(clients):
    """A string body that parses as JSON travels as application/json (the CLI's sniff rule), and
    giving a body without a method means POST (curl's convention)."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/j", "body": '{"a": 1}'}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    assert seen["headers"].get("content-type") == "application/json"
    assert json.loads(seen["body"]) == {"a": 1}


async def test_call_repeated_query_keys_survive(clients):
    """?tag=a&tag=b — a dict keeps only the last; a list value must expand to repeated keys."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/multi", "query": {"tag": ["a", "b"], "one": "x"}}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    tags = [v for k, v in seen["query_multi"] if k == "tag"]
    assert tags == ["a", "b"]


async def test_call_inline_query_in_a_passthrough_path_is_not_dropped(clients):
    """httpx DROPS a URL's existing query string whenever params= is passed — the upstream then
    answers with default/wrong data and NO error. The CLI guards this; the MCP tool must too."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/graph/me?fields=id,name", "query": {"limit": "5"}}, token=token)
    assert out.get("status") == 200, out
    seen = out["body"] if isinstance(out["body"], dict) else json.loads(out["body"])
    q = dict(seen["query_multi"])
    assert q.get("fields") == "id,name", "the inline query must reach the upstream"
    assert q.get("limit") == "5"


async def test_call_refuses_ambiguous_params_plus_explicit_slot(clients):
    """`params` claiming the same position as an explicit slot is refused loudly — a silent
    merge is how a wrong request gets sent."""
    token = await _register_echo(clients)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {
            "endpoint_id": "echo/x", "method": "POST",
            "params": {"a": 1}, "body": {"b": 2}}, token=token)
        assert "`body` OR `params`" in out.get("error", ""), out
        out2 = await _call_tool(c, "call", {
            "endpoint_id": "echo/x", "method": "GET",
            "params": {"a": "1"}, "query": {"b": "2"}}, token=token)
        assert "`query` OR `params`" in out2.get("error", ""), out2


# ---- the bug reports of 2026-08-17: a day of real calls, five things that cost the caller -------
async def test_an_id_that_misses_by_one_segment_names_the_real_one(clients):
    """`lusha.companies-signals` for `lusha.x.companies-signals` — what a model produces relaying an
    id through a summary. It broke search → get → call at its FIRST step, and "use catalog_search"
    sends the agent back to the step that produced the wrong id in the first place."""
    token = (await verified_signup(clients, json={"email": "nearmiss@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        got = await _call_tool(c, "catalog_get", {"endpoint_id": "lusha.companies-signals"}, token=token)
        called = await _call_tool(c, "call", {"endpoint_id": "lusha.companies-signals"}, token=token)
    assert got["did_you_mean"] == ["lusha.x.companies-signals"]
    assert called["did_you_mean"] == ["lusha.x.companies-signals"]
    assert "lusha.x.companies-signals" in called["hint"]


async def test_catalog_query_arrays_use_the_endpoints_declared_wire_encoding(monkeypatch):
    """A structured MCP list is not synonymous with repeated query keys.

    Meta declares one compact JSON array value for every array on the endpoint, while an unmodelled
    team tool keeps the longstanding repeated-key default. Comma encoding is covered with a
    synthetic endpoint declaration so the mechanism stays tested without coupling to a shipped row.
    This goes through `call`, not only `_query_values`: the first version stayed green if the MCP
    call site stopped using the helper.
    """
    from treg.domain.catalog import store as cs
    from treg import mcp as _mcp

    cat = cs.load()
    meta = cat.by_id["meta-ad-library.meta-ads.library.search"]
    assert _mcp._query_values(meta, "ad_reached_countries", ["US"]) == ['["US"]']
    comma_ep = {"input": {"queryArrayEncoding": "comma"}}
    assert _mcp._query_values(
        comma_ep, "include_domains", ["docs.python.org", "realpython.com"],
    ) == ["docs.python.org,realpython.com"]
    assert _mcp._query_values(None, "tag", ["a", "b"]) == ["a", "b"]

    captured = {}

    class _Client:
        headers: dict = {}

        async def request(self, method, path, **kwargs):
            import httpx
            captured.update(method=method, path=path, **kwargs)
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, path))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _api(_token):
        yield _Client()

    async def _org(_client):
        return 1, "team", None

    monkeypatch.setattr(_mcp, "_api", _api)
    monkeypatch.setattr(_mcp, "_resolve_org", _org)
    ctx = type("Ctx", (), {"headers": {"authorization": "Bearer test"}})()
    await _mcp.call("meta-ad-library.meta-ads.library.search", params={
        "ad_reached_countries": ["US"],
    }, ctx=ctx)
    assert captured["params"] == [("ad_reached_countries", '["US"]')]


async def test_call_sends_form_urlencoded_body_when_endpoint_specifies_bodytype_form(monkeypatch):
    """Endpoints with `input.bodyType: form` must send the body as application/x-www-form-urlencoded,
    not JSON. JustOneAPI's weixin-search-article endpoints failed with HTTP 415 "Unsupported media
    type" because treg sent JSON bodies to an upstream that required form-urlencoded."""
    from treg.domain.catalog import store as cs
    from treg import mcp as _mcp

    cat = cs.load()
    ep = cat.by_id.get("justoneapi.x.weixin-search-article-v1")
    assert ep is not None, "test endpoint must exist in catalog"
    assert (ep.get("input") or {}).get("bodyType") == "form", "test relies on bodyType: form"

    captured = {}

    class _Client:
        headers: dict = {}

        async def request(self, method, path, **kwargs):
            import httpx
            captured.update(method=method, path=path, **kwargs)
            return httpx.Response(200, json={"code": 0, "data": []}, request=httpx.Request(method, path))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _api(_token):
        yield _Client()

    async def _org(_client):
        return 1, "team", None

    monkeypatch.setattr(_mcp, "_api", _api)
    monkeypatch.setattr(_mcp, "_resolve_org", _org)
    ctx = type("Ctx", (), {"headers": {"authorization": "Bearer test"}})()
    await _mcp.call("justoneapi.x.weixin-search-article-v1", params={
        "keyword": "人工智能",
        "publishTimeType": "ALL",
        "sortType": "COMPREHENSIVE",
        "currentPage": "1",
        "offset": "0",
    }, ctx=ctx)

    # The body must be form-urlencoded, not JSON
    assert "content" in captured, "body must be sent as raw content, not kw['json']"
    assert "json" not in captured, "must not send as JSON when bodyType is form"

    # Verify the Content-Type header
    headers = captured.get("headers", {})
    ct = headers.get("content-type", headers.get("Content-Type", ""))
    assert ct == "application/x-www-form-urlencoded", f"wrong Content-Type: {ct}"

    # Verify the body is properly form-encoded
    from urllib.parse import parse_qs
    body = captured["content"].decode()
    parsed = parse_qs(body)
    assert parsed.get("keyword") == ["人工智能"]
    assert parsed.get("publishTimeType") == ["ALL"]
    assert parsed.get("currentPage") == ["1"]


async def test_an_unset_query_param_is_omitted_rather_than_sent_as_None(clients):
    """`None` means "no value", and `?limit=None` is not that — it is a string an upstream parses."""
    assert (await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})).status_code == 200
    token = clients.headers.get("X-Treg-Token")
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "echo/anything",
                                           "params": {"kept": True, "off": False,
                                                      "dropped": None, "obj": {"a": 1}}},
                               token=token)
    sent = (out.get("body") or {}).get("query") or {}
    # `str(True)` is "True" and `str({...})` is Python's single-quoted repr; neither is on the wire
    assert sent == {"kept": "true", "off": "false", "obj": '{"a":1}'}


async def test_search_survives_missing_a_few_words_of_an_agent_sentence(clients):
    """Agents query in sentences, and demanding every word zeroed real ones (models.SearchMiss,
    2026-08-19): "company job postings hiring open jobs linkedin" found nothing while three
    endpoints matched 6 of its 7 words — the only miss was "linkedin" on rows shelved under
    `companies`, or "open" on the one shelved under `linkedin`. A query may now miss one word in
    three, and idf weighting keeps the order on the rare words rather than the filler."""
    from treg.domain.catalog import store as cs
    cat = cs.load()
    # the two logged SearchMiss queries, verbatim
    # Enrichlayer, then a Crawl4AI recipe, add relevant job-search results ahead of the older
    # ninth-place match.
    rows, total = cs.search("company job postings hiring open jobs linkedin", cat, 10)
    assert total > 0
    assert {"apollo.companies.jobs", "apify.linkedin.search.jobs", "enrichlayer.jobs.search",
            "leadmagic.x.jobs-search-v3"} <= {ep["id"] for ep, _ in rows}
    # rank 1 must be the JOB (a companies.search row), never pinned to one provider — any newly
    # added provider of the same capability may legitimately outscore the incumbents
    rows, _ = cs.search("company search filter by location industry headcount growth", cat, 8)
    assert rows and rows[0][0]["capability"] == "companies.search"
    # the refinement property the old rule protected still holds: with one or two words there is no
    # miss allowance, so "tiktok comments" must not return every tiktok endpoint
    _, both = cs.search("tiktok comments", cat, 1)
    _, all_tiktok = cs.search("tiktok", cat, 1)
    assert 0 < both < all_tiktok
    for ep, _ in cs.search("tiktok comments", cat, 10**6)[0]:
        if ep.get("kind") == "routed":
            continue  # a routed parent rides in on a matched child; its own text need not match
        fields = cs._haystacks(ep, cat)
        assert any("tiktok" in t for _, t in fields) and any("comment" in t for _, t in fields)
    # aliases.yaml bridges the agent's word into the catalog's word at the same weight: the catalog
    # says "crypto", nobody's endpoint text says "cryptocurrency", and substring containment only
    # works in one direction
    rows, total = cs.search("current price of a cryptocurrency", cat, 3)
    assert total and rows[0][0]["id"].startswith("coingecko."), [ep["id"] for ep, _ in rows]
    # function words are dropped before the miss allowance is computed, so they cannot crowd out
    # the words that select ("on", "this" are not evidence about any endpoint)
    rows, _ = cs.search("trending repositories on github this week", cat, 3)
    assert rows and rows[0][0]["capability"] == "github.trending.repositories"
    # single letters can never select: "K&L" must not let k + l decide admission, and the company
    # job ("enrich by name") must lead instead of 67 rows of noise (logged miss, 2026-08-20)
    rows, total = cs.search("K&L Gates company lookup", cat, 8)
    # Person-by-company lookup also matches this sentence. Keep a company lookup in the first
    # three while rejecting the broad single-letter noise that caused the original miss.
    assert 0 < total < 45
    assert any(ep["capability"].startswith("companies.") for ep, _ in rows[:3])
    # the jobs rows must survive an industry qualifier the catalog never says ("law firm"), via
    # the openings->postings and firm->company aliases (logged miss, 2026-08-20)
    rows, total = cs.search("law firm job openings hiring signal", cat, 8)
    assert total and {"apollo.companies.jobs", "apify.linkedin.search.jobs"} <= {ep["id"] for ep, _ in rows}
    # a zero-result answer names the rows just under the gate and the exact unmatched words
    near = cs.near_misses("law firm dinosaur excavation permits", cat)
    assert all(n["missing"] for n in near) if near else True
    zero_q = "resolve company name to linkedin slug"
    if cs.search(zero_q, cat, 1)[1] == 0:
        assert cs.near_misses(zero_q, cat), "a zero result must surface its near-misses"


async def test_search_breaks_ties_on_what_treg_has_MEASURED(clients):
    """Token scoring ties by the dozen — every "ad library" match scores 6 — so with a default limit
    of 8 the rows an agent saw were decided by file order: seven tikhub rows, one of them
    uncallable, and the cheapest endpoint with a perfect record cut off below the fold."""
    from treg.domain.catalog import store as cs
    cat = cs.load()
    ranked, _, truncated = cs.rank_band("ad library", cat, 8)
    assert not truncated, "24 matches sit well inside the band"
    ids = [ep["id"] for ep, _ in ranked]
    good, broken = "scrapecreators.x.v1-tiktok-ad-library-search", "tikhub.x.tiktok-ads-search-ads"
    assert {good, broken} <= set(ids), "both are in the band before any evidence is applied"

    stats = {good: {"samples": 16, "ok_rate": 1.0},
             broken: {"samples": 12, "ok_rate": 0.0}}
    reranked = cs.rerank(ranked, stats, cat)
    out = [ep["id"] for ep, _ in reranked]
    assert out[0] == good, "a perfect measured record wins its score group outright"
    # …and the one that has never once answered sinks to the bottom of that group. Not to the bottom
    # of the whole list: relevance still ranks above evidence, so a less relevant row stays below a
    # broken-but-more-relevant one rather than being promoted past it by a failure count.
    broken_score = next(s for ep, s in reranked if ep["id"] == broken)
    assert [ep["id"] for ep, s in reranked if s == broken_score][-1] == broken


async def test_balance_says_WHOSE_grant_and_which_team_by_name(clients):
    """A slug alone cannot be sanity-checked: `superdesign-7` looks plausible to the agent and to
    the human reading over its shoulder. The display name and the account that authorised the
    connection are what make a wrong team legible before the spending is noticed."""
    token = (await verified_signup(clients, json={"email": "whose@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=token)
    assert out["team"] and out["team_name"]
    assert out["identity"] == "whose@superdesign.dev"
    # This one is a HEADER token, whose team is baked in — so it is labelled, but not sent to
    # `treg mcp grants`, which would list nothing for it. The OAuth half is asserted in
    # test_mcp_oauth.py::test_balance_tells_an_oauth_caller_how_to_move_the_team.
    assert "use-team" not in (out.get("hint") or "")


async def test_the_tie_band_covers_the_WHOLE_equal_scoring_group(clients, monkeypatch):
    """Reranking a slice that was already cut mid-tie cannot put back the row the cut dropped. The
    band therefore keeps taking while the score stays equal — and when a query ties so broadly that
    even the ceiling can't hold the group, it SAYS so rather than presenting an unranked tail as a
    ranked answer."""
    from treg.domain.catalog import store as cs
    cat = cs.load()
    rows, total, truncated = cs.rank_band("ad library", cat, 8)
    scores = [s for _, s in rows]
    assert len(rows) > 8 and not truncated
    everything, _ = cs.search("ad library", cat, 10**6)
    assert scores.count(scores[-1]) == sum(1 for _, s in everything if s == scores[-1]), \
        "the group straddling the cut is taken whole, or the cut is still arbitrary"

    # The boundary the "observe, don't infer" fix exists for: a tie group that EXACTLY fills the
    # ceiling is NOT truncated. Inferring it from `len(kept) >= RERANK_BAND` answered True here and
    # told the caller to narrow a query that had in fact been ranked in full. "ad library" has a
    # 17-row group at limit=8, so a ceiling of 17 sits exactly on it and 16 cuts it.
    band_at = len(rows)
    monkeypatch.setattr(cs, "RERANK_BAND", band_at)
    assert cs.rank_band("ad library", cat, 8)[2] is False, "a group that exactly fills is not cut"
    monkeypatch.setattr(cs, "RERANK_BAND", band_at - 1)
    assert cs.rank_band("ad library", cat, 8)[2] is True, "one row short of the group IS a cut"
    monkeypatch.undo()

    # a single word ties across hundreds — bounded, and the bound is announced
    wide, wide_total, wide_trunc = cs.rank_band("tiktok", cat, 8)
    assert wide_total > cs.RERANK_BAND and wide_trunc
    assert len(wide) <= cs.RERANK_BAND
    token = (await verified_signup(clients, json={"email": "wide@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "tiktok", "limit": 8}, token=token)
    assert "narrow" in (out.get("ranking_note") or ""), out.get("ranking_note")


async def test_a_near_miss_never_suggests_a_DIFFERENT_provider(clients):
    """A suggestion claims the caller mistyped. `apollo.people.email.find` is not a typo for
    `hunter.people.email.find` — it is another vendor, another price and another credential, so on a
    path that spends money that is provider routing wearing a spellcheck's clothes. treg compares
    providers and the caller chooses."""
    from treg.domain.catalog import store as cs
    cat = cs.load()
    assert cs.near_ids("lusha.companies-signals", cat) == ["lusha.x.companies-signals"]
    for crossing in ("apollo.people.email.find", "fake.companies-signals", "hunter.tiktok.video.comments"):
        for suggested in cs.near_ids(crossing, cat):
            assert cat.by_id[suggested]["provider"] == crossing.split(".")[0], suggested

    # POSITIVE cases for the two id shapes the same-provider rule silently killed. Without these the
    # assertion above is vacuous for them — "suggests nothing" trivially satisfies "suggests nothing
    # cross-provider", which is how a fix that helped no hyphenated provider kept a green test.
    hyphenated = next(e for e in cat.by_id if e.startswith("google-ads.x."))
    assert cs.near_ids(hyphenated.replace(".x.", ".", 1), cat) == [hyphenated], \
        "a provider whose name contains a hyphen must still resolve"
    x_owned = next(e for e in cat.by_id if e.split(".")[0] == "x")
    assert x_owned in cs.near_ids(x_owned, cat), \
        "the provider literally named 'x' must not be erased by stripping the 'x' tier marker"


async def test_the_SEARCH_TOOL_itself_ranks_on_evidence_not_just_the_helper(clients):
    """The helpers were tested; the wiring was not. `rerank()` could have been dropped from both
    call sites and every ranking test would still have passed, because they call the helper
    directly. This one goes through the MCP tool with real rows in the database."""
    from treg.domain.catalog import stats as endpoint_stats
    from treg.application import catalog_stats
    from treg.infra.db import session_maker
    from treg.models import CallRecord
    from treg.timeutil import utcnow_naive
    from datetime import timedelta

    broken = "apify.tiktok-ads.library.search"  # earlier in file order: rerank must move it
    good = "tikhub.x.tiktok-ads-search-ads"
    async with session_maker() as db:
        for status in (200, 200, 200, 200, 503):
            db.add(CallRecord(org_id=1, user_email="a@b.c", tool_name=good, method="GET", path="/x",
                              status_code=status, endpoint_id=good, duration_ms=100))
        for _ in range(5):      # the uncallable one, failing the way the report saw it
            db.add(CallRecord(org_id=1, user_email="a@b.c", tool_name=broken, method="POST", path="/x",
                              status_code=405, endpoint_id=broken, duration_ms=100))
        await db.commit()
    assert endpoint_stats.MIN_SAMPLES <= 5
    assert (await catalog_stats.refresh(session_maker, now=utcnow_naive() + timedelta(minutes=2)))["caught_up"]

    token = (await verified_signup(clients, json={"email": "ranker@superdesign.dev"})).json()["token"]
    async with mcp_session(clients) as c:
        await _call_tool(c, "catalog_search", {"query": "tiktok ads", "limit": 25}, token=token)
        await app.state.endpoint_observation_reader.wait_for_idle()
        out = await _call_tool(c, "catalog_search", {"query": "tiktok ads", "limit": 25}, token=token)
    ids = [r["endpoint_id"] for r in out["results"]]
    assert ids.index(good) < ids.index(broken), ids
    good_row = next(r for r in out["results"] if r["endpoint_id"] == good)
    broken_row = next(r for r in out["results"] if r["endpoint_id"] == broken)
    assert good_row["works"] == 0.8 and broken_row["works"] == 0.0



from test_marketplace_call import platform_on  # noqa: E402 — tier 4 on for the check run


# ---------------------------------------------------------------------------------------------
# The hub over MCP (phase 4): a maker's agent publishes a tool with no person in the loop

async def test_hub_create_over_mcp_publishes_and_names_refusals(clients, monkeypatch, platform_on):
    from treg.config import get_settings
    monkeypatch.setenv("TREG_HUB_ENABLED", "1")
    get_settings.cache_clear()
    from treg.application.call import service as call_service
    from test_marketplace_call import _fake_relay
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data": {"domain": "figma.com"}}'))
    token = (await funded_user(clients, "maker@superdesign.dev"))["token"]
    h = {"X-Treg-Token": token}
    sid = (await clients.post("/secrets", json={"name": "sb", "value": "K"}, headers=h)).json()["id"]
    await clients.post("/tools", json={"name": "supabase", "base_url": "https://x.supabase.co", "secret_id": sid}, headers=h)
    manifest = {"name": "leads-db", "summary": "Leads.", "inputs": {"domain": {"type": "string", "example": "figma.com"}},
                "uses": ["tikhub.tiktok.video.comments", "supabase"],
                "steps": [{"name": "people", "call": "tikhub.tiktok.video.comments", "input": {"aweme_id": "$input.domain"}}],
                "output": {"leads": "$people.data"}}
    check = {"inputs": {"domain": "figma.com"}, "fields": ["leads"]}
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "hub_create", {"manifest": manifest, "check": check, "readme": "x"}, token=token)
        assert out["status"] == "live" and out["tool_id"].endswith(".leads-db") and out["call"].startswith("POST /call/")
        mine = await _call_tool(c, "hub_mine", {}, token=token)
        assert [t["tool_id"] for t in mine["tools"]] == [out["tool_id"]]
        bad = await _call_tool(c, "hub_create", {"manifest": {**manifest, "uses": ["ghost"]}, "check": check, "readme": "x"}, token=token)
        assert bad["error"] == "manifest_invalid" and bad["field"] == "uses[0]"
        v2 = await _call_tool(c, "hub_update", {"tool_id": out["tool_id"], "manifest": manifest, "check": check, "readme": "v2"}, token=token)
        assert v2["version"] == 2 and v2["status"] == "live"
        # the agent walk (2026-09-10) found `call` refusing a live hub id before the server could
        # answer: a hub id is neither a catalog id nor <tool>/<path>; it goes through as a POST
        # with the inputs as params
        run = await _call_tool(c, "call", {"endpoint_id": out["tool_id"], "params": {"domain": "figma.com"}}, token=token)
        assert run["status"] == 200 and run["body"]["output"] == {"leads": {"domain": "figma.com"}}, run
        assert run["body"]["recipe"].startswith(out["tool_id"] + "@")
        # with the hub off the same id is refused as before
        monkeypatch.setenv("TREG_HUB_ENABLED", "0"); get_settings.cache_clear()
        off = await _call_tool(c, "call", {"endpoint_id": out["tool_id"], "params": {"domain": "figma.com"}}, token=token)
        assert off.get("error", "").startswith("unknown endpoint")
        monkeypatch.setenv("TREG_HUB_ENABLED", "1"); get_settings.cache_clear()
# ---- the overflow relay is disclosed on THIS surface, not only in a header ------------------
# 2026-09-08: apollo.people.search (catalog cost free) was served through Orthogonal 8,810 times
# at $0.002 while treg's Apollo account was out. `/call/` said so in X-Treg-Served-Via; an MCP
# client never sees headers, so the agent reported a free call that billed. Six reports.

from test_capacity_overflow import VENDOR_BODY, _orthogonal, _route, overflow_on, platform_on  # noqa: E402,F401


async def _overflow_rescued(monkeypatch, price_cents: float = 0.3):
    """Vendor 402 on treg's own key, Orthogonal answers: the shape of every rescued call."""
    from treg.application.call import overflow as O
    from treg.application.call import service as call_service
    from test_marketplace_call import _fake_relay

    await _route(price_micro=3_000)
    monkeypatch.setattr(call_service, "relay", _fake_relay(402, b'{"detail":"Insufficient balance"}'))
    seen: list = []
    monkeypatch.setattr(O, "_send", _orthogonal([(200, {"success": True, "data": VENDOR_BODY,
                                                         "priceCents": price_cents})], seen))
    return seen


async def test_a_call_served_by_the_overflow_relay_says_so_and_prices_it(clients, overflow_on, monkeypatch):
    token = clients.headers["X-Treg-Token"]
    seen = await _overflow_rescued(monkeypatch)
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "tikhub.tiktok.video.comments",
                                           "params": {"aweme_id": "7"}}, token=token)
    assert out.get("status") == 200, out
    assert out["served_via"] == "overflow:orthogonal" and out["cost_usd"] == 0.003
    assert out["body"] == VENDOR_BODY, "the vendor's body, verbatim, through the relay"
    hint = out.get("hint") or ""
    assert "overflow relay (orthogonal)" in hint and "treg's tikhub account is out" in hint, hint
    assert "real price" in hint and len(seen) == 1


async def test_a_direct_call_carries_no_served_via(clients, platform_on):
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "tikhub.tiktok.video.comments",
                                           "params": {"aweme_id": "7"}}, token=token)
    assert out.get("status") == 200, out
    assert "served_via" not in out and "overflow" not in (out.get("hint") or "")


async def test_the_directory_surface_discloses_the_relay_the_same_way(clients, overflow_on, monkeypatch):
    """`/mcp/v2/` builds the same result through `_call_impl`; reviewed against both on purpose."""
    from test_mcp_directory import _call_tool as _directory_call, directory_session

    token = clients.headers["X-Treg-Token"]
    await _overflow_rescued(monkeypatch)
    async with directory_session() as client:
        out = await _directory_call(client, "catalog_call_read",
                                    {"endpoint_id": "tikhub.tiktok.video.comments",
                                     "params": {"aweme_id": "7"}}, token)
    assert out.get("status") == 200, out
    assert out["served_via"] == "overflow:orthogonal" and out["cost_usd"] == 0.003
    assert "overflow relay (orthogonal)" in (out.get("hint") or "")


async def test_catalog_get_shows_what_a_FREE_endpoint_bills_through_the_relay(clients, overflow_on):
    """The price an agent quotes before calling must include the one it may actually pay."""
    from test_capacity_overflow import APOLLO_SEARCH_EP, APOLLO_SEARCH_PATH
    from test_mcp_directory import _call_tool as _directory_call, directory_session

    token = clients.headers["X-Treg-Token"]
    await _route(endpoint_id=APOLLO_SEARCH_EP, provider="apollo", method="POST", path=APOLLO_SEARCH_PATH,
                 price_micro=2_000, ratio=None)
    rest = (await clients.get(f"/catalog/endpoints/{APOLLO_SEARCH_EP}")).json()
    overflow_hint = next(h for h in rest["hints"] if "overflow relay (orthogonal)" in h)
    async with mcp_session(clients) as c:
        team = await _call_tool(c, "catalog_get", {"endpoint_id": APOLLO_SEARCH_EP}, token=token)
    async with directory_session() as client:
        directory = await _directory_call(client, "catalog_get", {"endpoint_id": APOLLO_SEARCH_EP}, token)
    for out in (team, directory):
        assert out["endpoint"]["cost"]["usd"] == 0, out["endpoint"]["cost"]
        assert out["overflow_price_usd"] == 0.002 and out["overflow_via"] == "orthogonal"
        assert out["overflow_price_unit"] == "call"
        assert out["endpoint"]["overflow_price_usd"] == 0.002
        assert overflow_hint in out["hints"]


def test_catalog_get_builds_the_overflow_hint_from_billing_fields():
    from treg import mcp

    ep = {"method": "GET", "provider": "example"}
    billing = {
        "overflow_via": "relay",
        "overflow_price_usd": 0.125,
        "overflow_price_unit": "result",
    }
    unrelated = {"hints": ["old route prose about an overflow relay (wrong)"]}
    for surface in (mcp._TEAM_SURFACE, mcp._DIRECTORY_SURFACE):
        plain_hint, = mcp._mcp_catalog_hints(unrelated, ep, surface=surface)
        primary, overflow = mcp._mcp_catalog_hints(billing | unrelated, ep, surface=surface)
        assert primary == plain_hint
        assert "treg's own example account is out" in overflow
        assert "overflow relay (relay)" in overflow and "$0.125 per result" in overflow
        assert "X-Treg-Served-Via / served_via" in overflow


@pytest.mark.parametrize(("endpoint_id", "auth_note", "other_note"), [
    ("ashby.companies.jobs", "no provider key required", "key injected server-side"),
    ("tikhub.tiktok.video.comments", "key injected server-side", "no provider key required"),
])
async def test_catalog_get_explains_provider_auth_on_both_surfaces(clients, endpoint_id, auth_note, other_note):
    from test_mcp_directory import _call_tool as _directory_call, directory_session

    token = clients.headers["X-Treg-Token"]
    args = {"endpoint_id": endpoint_id}
    async with mcp_session(clients) as client:
        team = await _call_tool(client, "catalog_get", args, token)
    async with directory_session() as client:
        directory = await _directory_call(client, "catalog_get", args, token)
    for out in (team, directory):
        assert auth_note in out["hints"][0] and other_note not in out["hints"][0]


async def test_a_lone_surrogate_in_an_upstream_body_still_returns_the_paid_answer(clients, monkeypatch):
    """A JavaScript scraper that cuts a LinkedIn post mid-pair sends `"\\ud835"`. The SDK's pydantic
    dump refused it, so the agent got "Error serializing to JSON" and lost the body, call_id and cost
    of a call it was charged for. Only the unpaired half becomes U+FFFD; the paired letter survives."""
    from treg.application.call import service as call_service
    from treg.application.call.types import UpstreamResponse
    from treg.config import get_settings

    monkeypatch.setenv("TREG_PLATFORM_KEY_HARVESTAPI", "PLATFORM-HARVESTAPI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "harvestapi")
    get_settings.cache_clear()

    async def relay(*args, **kwargs):
        async def stream():
            yield b'{"elements":[{"content":"\\ud835\\udc01old \\ud835 cut"}]}'

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    token = clients.headers["X-Treg-Token"]
    try:
        async with mcp_session(clients) as c:
            await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}}, token)
            r = await _rpc(c, "tools/call", {"name": "call", "arguments": {
                "endpoint_id": "harvestapi.linkedin.post.search", "params": {"search": "film"}}}, token)
    finally:
        get_settings.cache_clear()
    result = r.json()["result"]
    assert result["isError"] is False, result
    out = result["structuredContent"]
    assert out["status"] == 200
    assert out["body"] == {"elements": [{"content": "\U0001d401old � cut"}]}
    assert out["call_id"] and out["cost_usd"] > 0


async def test_catalog_get_gives_the_caller_its_own_team_values(clients, monkeypatch):
    """MCP catalog_get on a row with team_label fields returns `your_values` for the calling team,
    and names that team on the internal read, so a person in several teams still gets them."""
    from treg.application.call import resolve as call_resolution
    from treg.domain.catalog import store as catalog_store
    cat = catalog_store.load()
    ep = dict(cat.by_id["crawl4ai.watch.list"])
    monkeypatch.setitem(cat.by_id, "crawl4ai.watch.list", ep)
    token = (await verified_signup(clients, json={"email": "watcher@superdesign.dev"})).json()["token"]
    clients.headers["X-Treg-Token"] = token
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_get", {"endpoint_id": "crawl4ai.watch.list"}, token=token)
    assert out["endpoint"]["your_values"] == {"queryParams.agent": call_resolution.team_label(org_id)}


async def test_catalog_get_stays_within_an_mcp_client_output_limit(clients):
    """Hosts cap a tool result (Claude Code at about 25k tokens) and read `structuredContent`; a job
    with dozens of providers returned 150k+ characters, and a routed row lost its plan to the schema."""
    token = (await verified_signup(clients, json={"email": "size@superdesign.dev"})).json()["token"]
    results = {}
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}}, token)
        for endpoint_id in ("treg.people.search", "aiark.people.search"):
            r = await _rpc(c, "tools/call", {"name": "catalog_get", "arguments": {"endpoint_id": endpoint_id}}, token)
            results[endpoint_id] = r.json()["result"]
    routed = results["treg.people.search"]["structuredContent"]
    crowded = results["aiark.people.search"]["structuredContent"]
    assert routed["routing"]["plan"] and routed["related_capabilities"] and not routed["siblings"]
    assert len(crowded["siblings"]) > 20
    assert not any({"reserve", "reported_charge", "settle", "table"} & set(s["cost"] or {})
                   for s in crowded["siblings"])           # a sibling shows its price, not its settle
    for result in results.values():
        assert len(json.dumps(result)) < 100_000


async def test_tool_called_names_the_mcp_host(clients, monkeypatch, posthog_events):
    """An MCP call's `tool_called` names its host: the inbound User-Agent always, and `clientInfo.name`
    when the request carries it (2026-07-28+; an older client reports it at `initialize` alone, which
    the stateless transport does not keep)."""
    from treg.application.call import service as call_service
    from treg.application.call.types import UpstreamResponse
    from treg.config import get_settings

    monkeypatch.setenv("TREG_PLATFORM_KEY_HARVESTAPI", "PLATFORM-HARVESTAPI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "harvestapi")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test_suite", raising=False)

    async def relay(*args, **kwargs):
        async def stream():
            yield b'{"elements":[]}'

        async def close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"),), stream(), close)

    monkeypatch.setattr(call_service, "relay", relay)
    token = clients.headers["X-Treg-Token"]
    args = {"name": "call", "arguments": {
        "endpoint_id": "harvestapi.linkedin.post.search", "params": {"search": "film"}}}
    try:
        async with mcp_session(clients) as c:
            c.headers["User-Agent"] = "claude-code/2.1.0 (cli)"
            await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "claude-code", "version": "2.1.0"}}, token)
            legacy = await _rpc(c, "tools/call", args, token)
            modern = await _modern_rpc(c, "tools/call", args, token)
        events = await posthog_events()
    finally:
        get_settings.cache_clear()
    assert legacy.json()["result"]["isError"] is False, legacy.json()
    assert modern.json()["result"]["isError"] is False, modern.json()
    old, new = [e["properties"] for e in events]
    assert old["client"] == "mcp" and old["user_agent"].startswith("python-httpx/")
    assert old["mcp_user_agent"] == "claude-code/2.1.0 (cli)" and old["mcp_client"] is None
    assert new["mcp_user_agent"] == "claude-code/2.1.0 (cli)" and new["mcp_client"] == "test"


@pytest.mark.parametrize("tool", ["call", "call_media"])
@pytest.mark.parametrize(("mime", "kind"), [("image/png", "image"), ("audio/mpeg", "audio")])
@pytest.mark.parametrize("ua", [
    "", "claude-code/2.1.0", "Cursor", "openai-mcp/1.0.0", "Claude-User/1.0", "codex-mcp-client/0.160.1",
])
async def test_native_media_channels_and_codex_schemas(clients, monkeypatch, tool, mime, kind, ua):
    import base64

    import jsonschema

    from treg import mcp_output
    from treg.application.call import service
    from treg.application.call.types import UpstreamResponse

    chat = mcp_output.is_chat_sandbox({"User-Agent": ua})
    codex = ua.startswith("codex-mcp-client/")
    data = b"\xff" * (600 * 1024 if chat else 1536 * 1024)

    async def upstream(*args, **kwargs):
        async def stream():
            yield data

        async def close():
            pass

        return UpstreamResponse(200, ((b"content-type", mime.encode()),), stream(), close)

    monkeypatch.setattr(service, "relay", upstream)
    token = await _register_echo(clients)
    async with mcp_session(clients) as client:
        client.headers["User-Agent"] = ua
        listed = await _rpc(client, "tools/list", token=token)
        tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
        r = await _rpc(client, "tools/call", {"name": tool, "arguments": {
            "endpoint_id": "echo/media", "body": {}}}, token)

    for name in ("call", "call_media"):
        assert ("outputSchema" in tools[name]) is not codex
    assert tools["catalog_search"]["outputSchema"] and tools["call_result"]["outputSchema"]
    result = r.json()["result"]
    assert result["isError"] is False
    media, *rest = result["content"]
    assert media["type"] == kind and media["mimeType"] == mime
    assert base64.b64decode(media["data"]) == data
    if codex:
        assert "structuredContent" not in result
        structured = json.loads(rest[0]["text"])
    else:
        structured = result["structuredContent"]
        jsonschema.validate(structured, tools[tool]["outputSchema"])
    assert structured["call_id"] and structured["status"] == 200 and "body" not in structured
    if chat:
        assert rest == []
    else:
        assert len(rest) == 1 and rest[0]["type"] == "text"
        assert rest[0]["text"] == json.dumps(structured, separators=(",", ":"))


@pytest.mark.parametrize("tool", ["call", "call_media"])
async def test_svg_stays_text_even_for_codex(clients, monkeypatch, tool):
    from treg.application.call import service
    from treg.application.call.types import UpstreamResponse

    data = b'<svg xmlns="http://www.w3.org/2000/svg"/>'

    async def upstream(*args, **kwargs):
        async def stream():
            yield data

        async def close():
            pass

        return UpstreamResponse(200, ((b"content-type", b"image/svg+xml"),), stream(), close)

    monkeypatch.setattr(service, "relay", upstream)
    token = await _register_echo(clients)
    async with mcp_session(clients) as client:
        client.headers["User-Agent"] = "codex-mcp-client/0.160.1"
        r = await _rpc(client, "tools/call", {"name": tool, "arguments": {
            "endpoint_id": "echo/image", "body": {}}}, token)
    result = r.json()["result"]
    [block] = result["content"]
    assert block["type"] == "text"
    assert json.loads(block["text"]) == result["structuredContent"]
    assert result["structuredContent"]["body"] == data.decode()


@pytest.mark.parametrize(("ua", "size", "omitted"), [
    ("", 100 * 1024, False),
    ("", 1536 * 1024, False),
    ("claude-code/2.1.0", 100 * 1024, False),
    ("claude-code/2.1.0", 1536 * 1024, False),
    ("openai-mcp/1.0.0", 250 * 1024, True),
    ("Claude-User/1.0", 250 * 1024, True),
])
async def test_paid_call_text_uses_the_transport_profile(clients, monkeypatch, platform_on, ua, size, omitted):
    import jsonschema

    from test_marketplace_call import EP, EP_MICRO, _fake_relay
    from treg import mcp_output
    from treg.application.call import service
    from treg.config import get_settings

    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    body = {"omitted": True, "data": "x" * size}
    raw = json.dumps(body, separators=(",", ":")).encode()
    monkeypatch.setattr(service, "relay", _fake_relay(200, raw))

    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as client:
        client.headers["User-Agent"] = ua
        listed = await _rpc(client, "tools/list", token=token)
        schema = next(t["outputSchema"] for t in listed.json()["result"]["tools"] if t["name"] == "call")
        r = await _rpc(client, "tools/call", {"name": "call", "arguments": {
            "endpoint_id": EP, "query": {"aweme_id": "7"}}}, token)

    result = r.json()["result"]
    text = result["content"][0]["text"]
    out = result["structuredContent"]
    jsonschema.validate(out, schema)
    assert json.loads(text) == out
    assert len(text.encode()) <= mcp_output.budget_for({"User-Agent": ua}).envelope_bytes
    assert out["endpoint_id"] == EP and out["call_id"] and out["cost_usd"] == EP_MICRO / 1_000_000
    if omitted:
        receipt = out["body"]
        assert receipt["omitted"] and receipt["size_bytes"] == len(raw)
        assert receipt["head"] == raw[:mcp_output.PREVIEW_BYTES].decode()
        assert receipt["tail"] == raw[-mcp_output.PREVIEW_BYTES:].decode()
        assert "call_result" in receipt["note"]
    else:
        assert out["body"] == body


@pytest.mark.parametrize(("kind", "status", "note"), [
    ("archive", 200, "Call call_result(call_id, offset=0)"),
    ("over_cap", 200, "not archived"),
    ("own_tool", 200, "not archived"),
    ("forbidden", 200, "not archived"),
    ("spooled", 200, "fields parameter"),
    ("error", 400, "The error body exceeds the MCP budget and is not archived."),
])
async def test_standard_budget_omission_reports_the_archive_boundary(
    clients, monkeypatch, platform_on, kind, status, note,
):
    from test_marketplace_call import EP, _fake_relay
    from treg import mcp_output
    from treg.application.call import service
    from treg.config import get_settings
    from treg.domain.catalog import store as catalog_store

    settings = get_settings()
    monkeypatch.setattr(settings, "archive_mode", "shadow")
    if kind != "over_cap":
        monkeypatch.setattr(settings, "archive_max_body_bytes", 3 * 1024 * 1024)
    cat = catalog_store.load()
    row = cat.by_id[EP] | {"cache": "forbidden" if kind == "forbidden" else "transient"}
    if kind == "spooled":
        row["spooled_response"] = True
    monkeypatch.setitem(cat.by_id, EP, row)
    raw = b'{"data":"' + b"x" * mcp_output.MAX_TEXT_BYTES + b'"}'
    monkeypatch.setattr(service, "relay", _fake_relay(status, raw))
    token = clients.headers["X-Treg-Token"]
    args = {"endpoint_id": EP, "query": {"aweme_id": "7"}}
    if kind == "own_tool":
        token = await _register_echo(clients)
        args = {"endpoint_id": "echo/response"}
    async with mcp_session(clients) as client:
        out = await _call_tool(client, "call", args, token)
    assert out["status"] == status and out["call_id"]
    receipt = out["body"]
    assert receipt["omitted"] is True and receipt["size_bytes"] == len(raw)
    assert note in receipt["note"]
    if kind != "archive":
        assert "not archived" in receipt["note"] and "call_result" not in receipt["note"]


async def test_call_media_errors_and_argument_refusals_have_the_same_json_in_both_channels(clients, monkeypatch):
    from test_marketplace_call import _fake_relay
    from treg.application.call import service

    monkeypatch.setattr(service, "relay", _fake_relay(400, b'{"error":"invalid input","detail":"try another value"}'))
    token = await _register_echo(clients)
    async with mcp_session(clients) as client:
        for tool, arguments in (
            ("call_media", {"endpoint_id": "echo/media", "body": {}}),
            ("call", {"endpoint_id": "echo/media", "max_bytes": 16}),
        ):
            r = await _rpc(client, "tools/call", {"name": tool, "arguments": arguments}, token)
            result = r.json()["result"]
            assert result["isError"] is True
            [block] = result["content"]
            assert block["type"] == "text"
            assert block["text"] == json.dumps(result["structuredContent"], separators=(",", ":"))


async def test_oauth_scope_middleware_returns_compact_matching_json():
    from types import SimpleNamespace

    from treg import mcp
    from treg.domain.identity import mcp_oauth

    token = mcp_oauth.make_access_token(user_id=1, org_id=1, audience=mcp_oauth.mcp_resource_url("v1"),
                                        scope="treg:catalog")
    ctx = SimpleNamespace(method="tools/call", params={"name": "call"},
                          request=SimpleNamespace(headers={"Authorization": f"Bearer {token}"}))

    async def not_called(ctx):
        pytest.fail("scope middleware must refuse before calling the tool")

    result = await mcp._OAuthScopeGate(mcp._V1_TOOL_SCOPES, compact_json=True)(ctx, not_called)
    assert result.is_error and result.structured_content["error"] == "insufficient_scope"
    assert result.content[0].text == json.dumps(result.structured_content, separators=(",", ":"))


@pytest.mark.parametrize("wide_projection", [False, True])
async def test_mcp_history_budgets_the_projected_rows_newest_first(clients, monkeypatch, wide_projection):
    import jsonschema

    from treg import mcp_output
    from treg.infra.db import session_maker
    from treg.models import CallRecord
    from treg.routers import activity

    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        db.add_all([
            CallRecord(org_id=org_id, user_email="tester@example.invalid", tool_name="test",
                       method="GET", path="/" + "p" * 900, status_code=200, call_ref=f"call-{n}")
            for n in range(50)
        ])
        await db.commit()
    if wide_projection:
        call_row = activity.call_row

        def wide_row(row, task):
            return call_row(row, task) | {"async_task": {"detail": "x" * (5 * 1024)}}

        monkeypatch.setattr(activity, "call_row", wide_row)
    history = await clients.get("/calls", params={"limit": 50})
    rows = history.json()
    assert len(rows) == 50 and len(history.content) > 32 * 1024
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as client:
        await _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "history", "version": "1"}}, token)
        client.headers["User-Agent"] = "openai-mcp/1.0.0"
        listed = await _rpc(client, "tools/list", token=token)
        schema = next(t["outputSchema"] for t in listed.json()["result"]["tools"] if t["name"] == "call_result")
        r = await _rpc(client, "tools/call", {"name": "call_result", "arguments": {"limit": 50}}, token)
    result = r.json()["result"]
    text = result["content"][0]["text"]
    out = result["structuredContent"]
    jsonschema.validate(out, schema)
    assert json.loads(text) == out and len(text.encode()) <= mcp_output.CHAT_ENVELOPE_BYTES
    if wide_projection:
        assert 0 < len(out["calls"]) < 50 and "pass a smaller limit" in out["detail"]
    else:
        assert len(out["calls"]) == 50 and "detail" not in out
    assert [c["call_id"] for c in out["calls"]] == [c["call_ref"] for c in rows[:len(out["calls"])]]


def test_envelope_fallbacks_keep_receipt_and_small_task():
    from treg import mcp, mcp_output

    receipt = {"status": 200, "call_id": "call-1", "endpoint_id": "test.run", "cost_usd": 0.01,
               "reserved_usd": 0.02, "task": {"kind": "async"}, "replayed": False,
               "served_via": "overflow:test", "upstream_failed": "reported", "retry_after": "5"}
    raw_body = {"data": "x" * mcp_output.CHAT_ENVELOPE_BYTES}
    out = receipt | {"body": raw_body}
    budget = mcp_output.CHAT_SANDBOX_BUDGET
    trimmed = mcp._team_result(out, budget=budget).structured_content
    assert trimmed["body"]["omitted"] and trimmed["body"]["head"].startswith('{"data":"')
    assert {k: trimmed[k] for k in receipt} == receipt and out["body"] == raw_body

    large = out | {"detail": "d" * mcp_output.CHAT_ENVELOPE_BYTES}
    fallback = mcp._team_result(large, budget=budget).structured_content
    assert fallback == receipt | {"detail": "Result exceeds the MCP output budget."}


def test_comparison_and_hint_are_trimmed_before_the_call_body():
    from treg import mcp, mcp_output

    budget = mcp_output.STANDARD_BUDGET
    body = {"data": "x" * (budget.text_bytes - 20)}
    out = {"body": body, "compare": {"answer_1": "x" * (300 * 1024)}, "hint": "answer this comparison"}
    result = mcp._team_result(out, budget=budget)
    assert result.structured_content == {"body": body}


@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
async def test_codex_gate_also_accepts_dictionary_results(method):
    from types import SimpleNamespace

    from treg import mcp

    tool_list = {"tools": [
        {"name": "call", "output_schema": {}},
        {"name": "call_media", "outputSchema": {}},
        {"name": "call_result", "outputSchema": {}},
        {"name": "compare"},
    ]}
    media_result = {"content": [{"type": "audio"}], "structuredContent": {"call_id": "stored"}}
    original, expected = (
        (tool_list, {"tools": [{"name": "call"}, {"name": "call_media"},
                              {"name": "call_result", "outputSchema": {}}]})
        if method == "tools/list" else (media_result, {"content": [{"type": "audio"}]})
    )

    async def next_handler(ctx):
        return original

    ctx = SimpleNamespace(method=method, request=SimpleNamespace(headers={"User-Agent": "codex-mcp-client/0.160.1"}))
    assert await mcp._OpenAIClientGate()(ctx, next_handler) == expected


async def test_catalog_get_preserves_sibling_order_and_comparison_evidence(clients, monkeypatch):
    from treg.routers import catalog
    from treg.routers.views import catalog as cli_catalog

    observations = {
        "anyapi.web.scrape": {"samples": 10_000, "ok_rate": 0.4, "p50_ms": 100},
        "branddev.web.scrape": {"samples": 10_000, "decided": 1, "ok_rate": 1.0, "p50_ms": 100},
        "exa.web.contents.get": {"samples": 10_000, "ok_rate": 0.999, "p50_ms": 1400},
        "olostep.web.scrape": {"samples": 10_000, "ok_rate": 0.998, "p50_ms": 1400},
        "linkup.web.fetch": {
            "samples": 10_000, "decided": 9_990, "ok_rate": 0.985, "p50_ms": 1100, "p95_ms": 1900,
            "last_ok_days": 0, "hit_rate": 0, "hit_samples": 80, "optional": None,
        },
    }
    reviews = {"teams": 20, "share": {"useful": 1.0}}

    async def get_many(ids):
        return {key: observations[key] for key in ids if key in observations}

    async def verdicts():
        return {"olostep.web.scrape": reviews, "anyapi.web.scrape": reviews,
                "exa.web.contents.get": {"teams": 0, "samples": [{"reason": "early quote"}]}}

    monkeypatch.setattr(app.state.endpoint_observation_reader, "get_many", get_many)
    monkeypatch.setattr(catalog.feedback_app, "endpoint_verdicts", verdicts)
    endpoint_id = "crawl4ai.web.scrape"
    rest = (await clients.get(f"/catalog/endpoints/{endpoint_id}")).json()
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_get", {"endpoint_id": endpoint_id}, token=token)

    assert "call_template" not in out["endpoint"]
    assert json.loads(out["call_template"]) == {
        "endpoint_id": endpoint_id, "params": {"url": "https://example.com", "format": "md"},
    }
    assert all("treg call" not in h and "treg catalog" not in h for h in out["hints"])
    ids = [row["id"] for row in out["siblings"]]
    assert ids == [row["id"] for row in rest["siblings"]]
    assert ids[:3] == ["linkup.web.fetch", "exa.web.contents.get", "olostep.web.scrape"]
    assert ids.index("branddev.web.scrape") < ids.index("anyapi.web.scrape")
    assert "linkup.web.fetch" in rest["hints"][-1] and "linkup.web.fetch" in out["hints"][-1]
    cli, hints = cli_catalog.endpoint(rest, access=None, pinned=lambda _: None, limit=None, base_url="http://registry")
    # The CLI keeps the routed option in its own slot, ahead of the ordered direct alternatives.
    assert [row["id"] for row in cli["compare"]][:5] == [endpoint_id, "treg.web.extract", *ids[:3]]
    assert all("anyapi.web.scrape" not in hint for hint in hints)
    top = out["siblings"][0]
    assert "params" not in top
    assert top["observed"] == {key: value for key, value in observations[top["id"]].items()
                               if value is not None}
    assert out["siblings"][1]["reviews"] == {"teams": 0}
    assert out["siblings"][2]["reviews"] == reviews


async def test_catalog_get_siblings_quote_grouped_prices_and_trial_limits(clients):
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_get", {"endpoint_id": "apollo.people.search"}, token=token)
    siblings = {row["id"]: row for row in out["siblings"]}
    hunter = siblings["hunter.companies.emails"]
    assert hunter["usd"] == hunter["cost"]["display_usd"] == 0.0245
    assert hunter["cost"]["usd"] == 0.00245
    assert hunter["cost"]["display_unit"] == "started 10 emails"
    assert hunter["cost"]["per"] == 10 and "reported_charge" not in hunter["cost"]
    trial = siblings["getleadsio.people.search"]
    assert trial["usd"] == 0 and trial["cost"]["trial_calls_per_team_day"] == 5


async def test_catalog_get_uses_personal_values_in_query_and_nested_body(clients):
    from treg.application.call.resolve import team_label

    email = "ax-native@superdesign.dev"
    token = (await verified_signup(clients, json={"email": email})).json()["token"]
    clients.headers["X-Treg-Token"] = token
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with mcp_session(clients) as c:
        watch = await _call_tool(c, "catalog_get", {"endpoint_id": "crawl4ai.watch.create"}, token=token)
        budget = await _call_tool(c, "catalog_get", {"endpoint_id": "crawl4ai.watch.budget.add"}, token=token)
    assert json.loads(watch["call_template"])["params"] == {
        "url": "https://example.com/pricing", "agent": team_label(org_id), "notify": {"email": email},
    }
    budget_args = json.loads(budget["call_template"])
    assert budget_args["query"] == {"label": team_label(org_id)}
    assert budget_args["body"]["credits"] == 500


@pytest.mark.parametrize(("endpoint_id", "expected"), [
    ("instagram.x.user-messages", {"params": {"ig_user_id": "17841400000000000"}}),
    ("openmart.deny-rules.delete", {"body": {"rules": [{"type": "DOMAIN", "value": "example.invalid"}]}}),
    ("fishaudio.voices.create", {
        "form": {"type": "tts", "title": "treg verification voice", "train_mode": "fast",
                 "visibility": "private"},
        "uploads": [{"name": "voices", "filename": "voice-reference.wav", "content_type": "audio/wav",
                     "data_base64": "<base64 file contents>"}],
    }),
])
async def test_catalog_get_call_template_uses_the_endpoint_wire_contract(clients, endpoint_id, expected):
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_get", {"endpoint_id": endpoint_id}, token=token)
    assert json.loads(out["call_template"]) == {"endpoint_id": endpoint_id, **expected}


def test_cli_and_mcp_call_templates_share_required_examples_and_test_values():
    import shlex

    from treg import mcp
    from treg.domain.catalog import store

    ep = {
        "id": "example.write", "method": "POST", "authorization_methods": ["app", "oauth"],
        "input": {
            "pathParams": {"id": {"required": True, "example": "fallback"}},
            "queryParams": {
                "limit": {"required": True, "example": 10, "authorization_methods": ["app"]},
                "oauth_only": {"required": True, "type": "string", "authorization_methods": ["oauth"]},
            },
            "body": {"payload.name": {"required": True, "example": "example name"}},
        },
        "test_request": {"pathParams": {"id": "actual"}, "queryParams": {"live": False}},
    }
    cli = shlex.split(store.call_template(ep))
    args = json.loads(mcp._mcp_call_template(ep))
    assert cli == [
        "treg", "call", ep["id"], "--method", "POST", "--query", "id=actual",
        "--query", "limit=10", "--query", "live=false", "--data", '{"payload":{"name":"example name"}}',
    ]
    assert args == {
        "endpoint_id": ep["id"], "query": {"id": "actual", "limit": 10, "live": False},
        "body": json.loads(cli[-1]),
    }
    assert json.loads(mcp._mcp_call_template(ep, {
        "pathParams.id": "team", "queryParams.live": True, "body.payload.name": "caller name",
    })) == {
        "endpoint_id": ep["id"], "query": {"id": "team", "limit": 10, "live": True},
        "body": {"payload": {"name": "caller name"}},
    }


@pytest.mark.parametrize("body", [{}, [{"name": "example"}], "raw body"])
def test_cli_and_mcp_call_templates_keep_explicit_bodies(body):
    import shlex

    from treg import mcp
    from treg.domain.catalog import store

    ep = {
        "id": "example.write", "method": "POST", "authorization_method": "oauth",
        "test_request": {"body": body},
    }
    assert shlex.split(store.call_template(ep)) == [
        "treg", "call", ep["id"], "--method", "POST", "--authorization-method", "oauth",
        "--data", json.dumps(body, separators=(",", ":")),
    ]
    assert json.loads(mcp._mcp_call_template(ep)) == {
        "endpoint_id": ep["id"], "authorization_method": "oauth", "params": body,
    }


async def test_call_max_cost_usd_enforces_a_metered_platform_ceiling(clients, platform_on, monkeypatch):
    from treg.application.call import service as call_service
    from test_marketplace_call import EP, EP_MICRO, _fake_relay

    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"data":{"comments":[{"id":"1"}]}}'))
    token = clients.headers["X-Treg-Token"]
    args = {"endpoint_id": EP, "params": {"aweme_id": "7"}}
    async with mcp_session(clients) as c:
        balance = (await _call_tool(c, "balance", {}, token=token))["balance_usd"]
        refused = await _call_tool(c, "call", {**args, "max_cost_usd": 0.0009974997}, token=token)
        assert refused["status"] == 402
        detail = refused["body"]["detail"]
        assert detail["error"] == "route_max_cost"
        assert detail["max_cost_micro"] == 997 and detail["estimated_cost_micro"] == EP_MICRO
        assert (await _call_tool(c, "balance", {}, token=token))["balance_usd"] == balance

        conflict = await _call_tool(c, "call", {
            **args, "max_cost_usd": 0.001, "headers": {"x-TrEg-rOuTe-Max-Cost": "0.0005"},
        }, token=token)
        assert conflict["error"] == "max_cost_usd conflicts with the X-Treg-Route-Max-Cost header"
        assert (await _call_tool(c, "balance", {}, token=token))["balance_usd"] == balance

        allowed = await _call_tool(c, "call", {
            **args, "max_cost_usd": 0.001, "headers": {"x-TrEg-rOuTe-Max-Cost": "1e-3"},
        }, token=token)
    assert allowed["status"] == 200 and allowed["cost_usd"] == EP_MICRO / 1_000_000


@pytest.mark.parametrize("ceiling", [True, 0, -1, float("nan"), float("inf"), -float("inf")])
async def test_call_rejects_invalid_max_cost_usd_before_relay(clients, platform_on, monkeypatch, ceiling):
    from treg.application.call import service as call_service
    from test_marketplace_call import EP

    async def no_relay(*args, **kwargs):
        pytest.fail("an invalid max_cost_usd reached the upstream relay")

    monkeypatch.setattr(call_service, "relay", no_relay)
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}}, token)
        # httpx's JSON encoder refuses nonfinite values, so send the raw payload to exercise MCP.
        r = await c.post("http://localhost/mcp/", headers=MCP_HEADERS | {"Authorization": f"Bearer {token}"},
                         content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "call", "arguments": {
                                                 "endpoint_id": EP, "params": {"aweme_id": "7"},
                                                 "max_cost_usd": ceiling,
                                             }}}))
    out = mcp_text_result("call", r.json())
    if isinstance(ceiling, bool):
        assert r.json()["result"]["isError"] and "max_cost_usd" in out["_text"]
    else:
        assert out["error"] == "max_cost_usd must be a finite positive number"
