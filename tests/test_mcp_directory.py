"""The Claude directory MCP is catalog-only and OAuth-isolated from the team MCP."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.routing import Mount

from treg import mcp
from treg.domain.identity import mcp_oauth
from treg.routers import auth as auth_routes
from treg.bootstrap import create_app
from treg.config import Settings, get_settings

pytestmark = pytest.mark.anyio

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def directory_session():
    fresh = mcp.build_mcp_app(server=mcp.directory_mcp, resource_version="v2")
    host = FastAPI()
    host.mount("/mcp/v2", fresh)
    async with mcp.mcp_lifespan(fresh):
        async with AsyncClient(transport=ASGITransport(app=host),
                               base_url="http://localhost") as client:
            yield client


@asynccontextmanager
async def paired_mcp_session():
    """Serve both public MCP surfaces on one app for direct contract comparisons."""
    team = mcp.build_mcp_app(resource_version="v1")
    directory = mcp.build_mcp_app(server=mcp.directory_mcp, resource_version="v2")
    host = FastAPI()
    host.mount("/mcp/v2", directory)
    host.mount("/mcp", team)
    async with mcp.mcp_lifespan(team):
        async with mcp.mcp_lifespan(directory):
            async with AsyncClient(transport=ASGITransport(app=host),
                                   base_url="http://localhost") as client:
                yield client


async def _rpc(client: AsyncClient, method: str, params=None, token: str | None = None,
               extra_headers: dict | None = None, *, path: str = "/mcp/v2/"):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra_headers or {})
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post(path, json=body, headers=headers)


async def _call_tool(client: AsyncClient, name: str, args: dict, token: str,
                     extra_headers: dict | None = None, *, path: str = "/mcp/v2/") -> dict:
    await _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "directory-test", "version": "1"},
    }, token, extra_headers, path=path)
    response = await _rpc(client, "tools/call", {"name": name, "arguments": args},
                          token, extra_headers, path=path)
    payload = response.json()
    content = (payload.get("result") or {}).get("content") or []
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return payload


async def _modern_rpc(client: AsyncClient, method: str, params=None,
                      token: str = "opaque-test-token"):
    body_params = dict(params or {})
    body_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "directory-test", "version": "1"},
    }
    return await client.post("http://localhost/mcp/v2/", json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": body_params,
    }, headers={
        **MCP_HEADERS,
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": "2026-07-28",
        "MCP-Method": method,
    })


async def test_v2_feature_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("TREG_CLAUDE_CONNECTOR_ENABLED", raising=False)
    assert Settings(_env_file=None).claude_connector_enabled is False


async def test_v2_feature_flag_disables_mount_metadata_grants_and_catalog_route(
    monkeypatch, clients,
):
    monkeypatch.setenv("TREG_CLAUDE_CONNECTOR_ENABLED", "false")
    get_settings.cache_clear()
    try:
        disabled = create_app("all")
        mounts = [route.path for route in disabled.routes if isinstance(route, Mount)]
        assert "/mcp" in mounts
        assert "/mcp/v2" not in mounts

        async with AsyncClient(transport=ASGITransport(app=disabled),
                               base_url="http://registry") as client:
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp/v2")
        assert metadata.status_code == 404
        assert "not enabled" in metadata.json()["detail"]

        resource = mcp_oauth.mcp_resource_url("v2")
        assert "not enabled" in auth_routes._wrong_resource(resource)

        direct = await clients.get("/catalog/call/tikhub.tiktok.video.comments?aweme_id=7")
        assert direct.status_code == 404
        assert "not enabled" in direct.json()["detail"]
    finally:
        get_settings.cache_clear()


async def test_v2_feature_flag_enables_mount_metadata_and_resource(monkeypatch):
    monkeypatch.setenv("TREG_CLAUDE_CONNECTOR_ENABLED", "true")
    get_settings.cache_clear()
    try:
        enabled = create_app("all")
        mounts = [route.path for route in enabled.routes if isinstance(route, Mount)]
        assert mounts.index("/mcp/v2") < mounts.index("/mcp")

        async with AsyncClient(transport=ASGITransport(app=enabled),
                               base_url="http://registry") as client:
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp/v2")
            body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "slash-test", "version": "1"},
            }}
            challenges = [
                await client.post(path, json=body, headers=MCP_HEADERS)
                for path in ("/mcp/v2", "/mcp/v2/")
            ]
        assert metadata.status_code == 200
        assert metadata.json()["resource"].endswith("/mcp/v2/")
        for challenge in challenges:
            assert challenge.status_code == 401
            assert "/.well-known/oauth-protected-resource/mcp/v2" in \
                challenge.headers["www-authenticate"]

        resource = mcp_oauth.mcp_resource_url("v2")
        assert auth_routes._wrong_resource(resource) is None
    finally:
        get_settings.cache_clear()


async def test_v2_no_slash_path_rejects_a_v1_token_with_the_v2_challenge(monkeypatch):
    """Claude strips the final slash. That spelling must never fall through to the V1 mount."""
    monkeypatch.setenv("TREG_CLAUDE_CONNECTOR_ENABLED", "true")
    get_settings.cache_clear()
    try:
        enabled = create_app("all")
        token = mcp_oauth.make_access_token(
            user_id=1, org_id=1, audience=mcp_oauth.mcp_resource_url("v1"))
        async with AsyncClient(transport=ASGITransport(app=enabled),
                               base_url="http://registry") as client:
            response = await client.post("/mcp/v2", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            }, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert 'error="invalid_token"' in response.headers["www-authenticate"]
        assert "/.well-known/oauth-protected-resource/mcp/v2" in \
            response.headers["www-authenticate"]
    finally:
        get_settings.cache_clear()


async def test_v2_declares_exact_directory_contract():
    tools = {tool.name: tool for tool in await mcp.directory_mcp.list_tools()}
    assert list(tools) == [
        "catalog_search", "catalog_get", "catalog_call_read", "catalog_call_write", "balance",
        "catalog_request",
    ]
    expected_titles = {
        "catalog_search": "Search Treg Catalog",
        "catalog_get": "Get Catalog Endpoint",
        "catalog_call_read": "Call a Read Endpoint",
        "catalog_call_write": "Call a Write Endpoint",
        "balance": "Check Treg Balance",
        "catalog_request": "Request a Catalog Capability",
    }
    assert {name: tool.title for name, tool in tools.items()} == expected_titles
    assert {name: tool.annotations.title for name, tool in tools.items()} == expected_titles
    assert "my_tools" not in tools and "call" not in tools
    assert "method" not in tools["catalog_call_read"].input_schema["properties"]
    assert "method" not in tools["catalog_call_write"].input_schema["properties"]
    for tool in tools.values():
        assert tool.output_schema and tool.output_schema.get("properties")
        assert not tool.output_schema.get("required")
    blob = " ".join(tool.description.lower() for tool in tools.values())
    for disallowed in ("use treg first", "official", "anthropic verified", "best provider"):
        assert disallowed not in blob


async def test_v2_does_not_advertise_or_serve_change_subscriptions():
    async with directory_session() as client:
        discovered = await _modern_rpc(client, "server/discover")
        listened = await _modern_rpc(client, "subscriptions/listen", {
            "notifications": {"toolsListChanged": True},
        })

    assert discovered.status_code == 200, discovered.text
    capabilities = discovered.json()["result"]["capabilities"]
    assert capabilities["tools"]["listChanged"] is False
    assert capabilities["prompts"]["listChanged"] is False
    assert capabilities["resources"] == {"listChanged": False, "subscribe": False}
    assert listened.status_code == 404, listened.text
    assert listened.json()["error"] == {
        "code": -32601, "message": "Method not found", "data": "subscriptions/listen",
    }


async def test_v2_annotations_separate_safe_and_unsafe_calls():
    tools = {tool.name: tool for tool in await mcp.directory_mcp.list_tools()}
    read = tools["catalog_call_read"].annotations
    write = tools["catalog_call_write"].annotations
    request = tools["catalog_request"].annotations
    assert read.read_only_hint is True and read.destructive_hint is False and read.open_world_hint is True
    assert write.read_only_hint is False and write.destructive_hint is True and write.open_world_hint is True
    assert request.read_only_hint is False and request.destructive_hint is False


async def test_v2_call_tools_enforce_catalog_method_class_before_calling_api():
    ctx = type("Ctx", (), {"headers": {"authorization": "Bearer test"}})()
    get_id = "tikhub.tiktok.video.comments"
    post_id = "dataforseo.web.page.audit"

    wrong_read = await mcp.directory_catalog_call_read(post_id, ctx=ctx)
    wrong_write = await mcp.directory_catalog_call_write(get_id, ctx=ctx)
    arbitrary = await mcp.directory_catalog_call_read("team-tool/private", ctx=ctx)

    assert "only GET, HEAD or OPTIONS" in wrong_read["error"]
    assert "only POST, PUT, PATCH or DELETE" in wrong_write["error"]
    assert arbitrary["error"].startswith("unknown endpoint")


async def test_v1_and_v2_access_tokens_are_not_interchangeable():
    v1_aud = mcp_oauth.mcp_resource_url("v1")
    v2_aud = mcp_oauth.mcp_resource_url("v2")
    v1 = mcp_oauth.make_access_token(user_id=1, org_id=1, audience=v1_aud)
    v2 = mcp_oauth.make_access_token(user_id=1, org_id=1, audience=v2_aud)

    assert mcp_oauth.read_access_token_any(v1, "v1") is not None
    assert mcp_oauth.read_access_token_any(v1, "v2") is None
    assert mcp_oauth.read_access_token_any(v2, "v2") is not None
    assert mcp_oauth.read_access_token_any(v2, "v1") is None
    assert mcp_oauth.mcp_resource_version(v1_aud.rstrip("/")) == "v1"
    assert mcp_oauth.mcp_resource_version(v2_aud.rstrip("/")) == "v2"


async def test_v2_transport_challenges_with_v2_metadata():
    async with directory_session() as client:
        response = await _rpc(client, "tools/list")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp/v2" in challenge
    assert mcp_oauth.DIRECTORY_SCOPE in challenge
    assert response.headers["cache-control"] == "no-store, no-transform"


async def test_v2_serializes_the_scanner_facing_contract(clients):
    token = (await clients.post("/users", json={"email": "directory-list@superdesign.dev"})).json()["token"]
    async with directory_session() as client:
        initialized = await _rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "scanner", "version": "1"},
        }, token)
        assert initialized.status_code == 200
        listed = await _rpc(client, "tools/list", token=token)
    tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
    expected_titles = {
        "catalog_search": "Search Treg Catalog",
        "catalog_get": "Get Catalog Endpoint",
        "catalog_call_read": "Call a Read Endpoint",
        "catalog_call_write": "Call a Write Endpoint",
        "balance": "Check Treg Balance",
        "catalog_request": "Request a Catalog Capability",
    }
    assert set(tools) == set(expected_titles)
    assert {name: tool["annotations"]["title"] for name, tool in tools.items()} == expected_titles
    assert tools["catalog_call_read"]["annotations"] == {
        "title": "Call a Read Endpoint",
        "readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
        "openWorldHint": True,
    }
    assert tools["catalog_call_write"]["annotations"]["destructiveHint"] is True
    assert "method" not in tools["catalog_call_read"]["inputSchema"]["properties"]
    assert "method" not in tools["catalog_call_write"]["inputSchema"]["properties"]


async def test_v2_shared_catalog_details_balance_and_guidance_work_end_to_end(clients):
    token = clients.headers["X-Treg-Token"]
    async with directory_session() as client:
        search = await _call_tool(client, "catalog_search", {"query": "tiktok comments"}, token)
        endpoint = await _call_tool(client, "catalog_get", {
            "endpoint_id": "tikhub.tiktok.video.comments",
        }, token)
        balance = await _call_tool(client, "balance", {}, token)

    assert search["results"] and search["results"][0].get("usd_per_call") is not None
    assert "catalog_call_read" in search["next"]
    assert "catalog_call_write" in search["next"]
    assert "then call(...)" not in search["next"]
    assert endpoint["endpoint"]["id"] == "tikhub.tiktok.video.comments"
    assert endpoint["endpoint"].get("cost")
    assert balance["team"] and balance["balance_micro"] is not None


async def test_team_and_directory_catalog_search_results_and_prices_match(clients):
    token = clients.headers["X-Treg-Token"]
    args = {"query": "tiktok comments", "limit": 5}
    async with paired_mcp_session() as client:
        team = await _call_tool(client, "catalog_search", args, token, path="/mcp/")
        directory = await _call_tool(client, "catalog_search", args, token)

    team_shared = {key: value for key, value in team.items() if key != "next"}
    directory_shared = {key: value for key, value in directory.items() if key != "next"}
    assert team_shared == directory_shared
    assert team["results"] and all("usd_per_call" in row for row in team["results"])
    assert team["next"].endswith("then call(...)")
    assert "catalog_call_read" in directory["next"]
    assert "catalog_call_write" in directory["next"]


async def test_team_and_directory_endpoint_details_and_balance_match(clients):
    token = clients.headers["X-Treg-Token"]
    endpoint_args = {"endpoint_id": "tikhub.tiktok.video.comments"}
    async with paired_mcp_session() as client:
        # Endpoint stats come through the stale-while-revalidate cache: a COLD read answers
        # `observed: None` and refreshes in the background, so two reads in a row can differ on
        # nothing but timing (CI, 2026-08-28). Warm it, then compare.
        from treg import api as A
        await _call_tool(client, "catalog_get", endpoint_args, token, path="/mcp/")
        await A.app.state.endpoint_observation_reader.wait_for_idle()
        team_endpoint = await _call_tool(
            client, "catalog_get", endpoint_args, token, path="/mcp/",
        )
        directory_endpoint = await _call_tool(client, "catalog_get", endpoint_args, token)
        team_balance = await _call_tool(client, "balance", {}, token, path="/mcp/")
        directory_balance = await _call_tool(client, "balance", {}, token)

    assert team_endpoint == directory_endpoint
    assert team_endpoint["endpoint"]["id"] == endpoint_args["endpoint_id"]
    assert team_endpoint["endpoint"]["cost"] == directory_endpoint["endpoint"]["cost"]
    assert team_balance == directory_balance


async def test_team_and_directory_catalog_call_results_match_except_attribution(clients):
    from treg import audit

    await clients.post("/secrets", json={"name": "tikhub", "value": "PAIRED-KEY"})
    token = clients.headers["X-Treg-Token"]
    args = {
        "endpoint_id": "tikhub.tiktok.video.comments",
        "params": {"aweme_id": "7"},
        "headers": {"X-Paired-Test": "same"},
    }
    async with paired_mcp_session() as client:
        team = await _call_tool(client, "call", args, token, path="/mcp/")
        directory = await _call_tool(client, "catalog_call_read", args, token)
    await audit.drain()

    assert team == directory
    assert team["status"] == 200
    assert team["body"]["auth"] == "Bearer PAIRED-KEY"
    assert team["body"]["headers"]["x-paired-test"] == "same"
    rows = (await clients.get("/calls")).json()
    assert {row["client"] for row in rows} == {"mcp", "claude-connector"}


async def test_team_and_directory_catalog_call_errors_match_or_keep_the_documented_boundary(clients):
    token = clients.headers["X-Treg-Token"]
    ambiguous = {
        "endpoint_id": "tikhub.tiktok.video.comments",
        "params": {"aweme_id": "7"},
        "query": {"aweme_id": "7"},
    }
    unknown = {"endpoint_id": "not.a.catalog.endpoint"}
    async with paired_mcp_session() as client:
        team_ambiguous = await _call_tool(client, "call", ambiguous, token, path="/mcp/")
        directory_ambiguous = await _call_tool(
            client, "catalog_call_read", ambiguous, token,
        )
        team_unknown = await _call_tool(client, "call", unknown, token, path="/mcp/")
        directory_unknown = await _call_tool(client, "catalog_call_read", unknown, token)

    assert team_ambiguous == directory_ambiguous
    assert "query string" in team_ambiguous["error"]
    assert team_unknown["error"] == directory_unknown["error"]
    assert team_unknown["did_you_mean"] == directory_unknown["did_you_mean"]
    assert "my_tools" in team_unknown["hint"]
    assert "my_tools" not in directory_unknown["hint"]
    assert "catalog endpoint id" in directory_unknown["hint"]


async def test_v2_search_and_catalog_request_keep_directory_attribution(clients):
    from sqlmodel import select

    from treg import audit
    from treg.infra.db import session_maker
    from treg.models import SearchMiss, ToolRequest

    token = clients.headers["X-Treg-Token"]
    async with directory_session() as client:
        miss = await _call_tool(client, "catalog_search", {
            "query": "zzzz-directory-only-miss",
        }, token)
        request = await _call_tool(client, "catalog_request", {
            "capability": "directory attribution test",
        }, token)
    await audit.drain()

    assert miss["count"] == 0
    assert request["status"] == "received"
    async with session_maker() as db:
        search_row = (await db.execute(select(SearchMiss))).scalars().one()
        request_row = (await db.execute(select(ToolRequest))).scalars().one()
    assert search_row.source == "claude-connector"
    assert request_row.source == "claude-connector"


async def test_v2_catalog_call_uses_shared_credentials_headers_errors_and_audit(clients):
    from treg import audit

    await clients.post("/secrets", json={"name": "tikhub", "value": "DIRECTORY-KEY"})
    token = clients.headers["X-Treg-Token"]
    async with directory_session() as client:
        result = await _call_tool(client, "catalog_call_read", {
            "endpoint_id": "tikhub.tiktok.video.comments",
            "params": {"aweme_id": "7"},
            "headers": {"X-Test-Header": "kept", "X-Treg-Token": "blocked"},
        }, token, {"X-Treg-Meta": "customer=directory_test"})
    await audit.drain()

    assert result["status"] == 200
    assert result["body"]["auth"] == "Bearer DIRECTORY-KEY"
    assert result["body"]["headers"]["x-test-header"] == "kept"
    assert result["body"]["headers"].get("x-treg-token") != "blocked"
    assert "cost_usd" not in result or result["cost_usd"] is None
    row = (await clients.get("/calls")).json()[0]
    assert row["client"] == "claude-connector"
    assert row["tags"] == {"customer": "directory_test"}


async def test_v2_metering_and_idempotency_use_the_shared_money_path(clients, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "DIRECTORY-PLATFORM-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    try:
        org_id = (await clients.get("/orgs")).json()[0]["org_id"]

        async def current_balance() -> int:
            body = (await clients.get(f"/orgs/{org_id}/balance")).json()
            return body["balance_micro"]

        token = clients.headers["X-Treg-Token"]
        args = {
            "endpoint_id": "tikhub.tiktok.video.comments",
            "params": {"aweme_id": "7"},
            "idempotency_key": "directory-one-call",
        }
        before = await current_balance()
        async with directory_session() as client:
            first = await _call_tool(client, "catalog_call_read", args, token)
            second = await _call_tool(client, "catalog_call_read", args, token)
        after = await current_balance()

        assert first["status"] == 200 and first["cost_usd"] > 0
        assert second["status"] == 200 and second["replayed"] is True
        assert second["body"] == first["body"]
        assert before - after == round(first["cost_usd"] * 1_000_000)
    finally:
        get_settings.cache_clear()


async def test_v2_write_call_and_insufficient_balance_errors_use_shared_behavior(
    clients, monkeypatch,
):
    from sqlmodel import select

    from treg.infra.db import session_maker
    from treg.models import Org

    await clients.post("/secrets", json={"name": "dataforseo", "value": "user:password"})
    token = clients.headers["X-Treg-Token"]
    async with directory_session() as client:
        write = await _call_tool(client, "catalog_call_write", {
            "endpoint_id": "dataforseo.web.page.audit",
            "body": [{"url": "https://example.com"}],
        }, token)
    assert write["status"] == 200
    assert json.loads(write["body"]["body"]) == [{"url": "https://example.com"}]

    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "DIRECTORY-PLATFORM-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    try:
        org_id = (await clients.get("/orgs")).json()[0]["org_id"]
        async with session_maker() as db:
            org = (await db.execute(select(Org).where(Org.id == org_id))).scalar_one()
            org.balance_micro = 0
            db.add(org)
            await db.commit()
        async with directory_session() as client:
            refused = await _call_tool(client, "catalog_call_read", {
                "endpoint_id": "tikhub.tiktok.video.comments",
                "params": {"aweme_id": "7"},
            }, token)
        blob = json.dumps(refused)
        assert refused["status"] == 402
        assert "topup_url" not in blob and "http://" not in blob and "https://" not in blob
        assert "not enough for this call" in refused["hint"]
    finally:
        get_settings.cache_clear()


async def test_transports_reject_the_other_mcp_versions_token():
    v1 = mcp_oauth.make_access_token(user_id=1, org_id=1,
                                     audience=mcp_oauth.mcp_resource_url("v1"))
    v2 = mcp_oauth.make_access_token(user_id=1, org_id=1,
                                     audience=mcp_oauth.mcp_resource_url("v2"))
    async with directory_session() as client:
        assert (await _rpc(client, "tools/list", token=v1)).status_code == 401
        assert (await _rpc(client, "tools/list", token=v2)).status_code == 200

    team_mcp = mcp.build_mcp_app(resource_version="v1")
    host = FastAPI()
    host.mount("/mcp", team_mcp)
    async with mcp.mcp_lifespan(team_mcp):
        async with AsyncClient(transport=ASGITransport(app=host),
                               base_url="http://localhost") as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            wrong = await client.post("http://localhost/mcp/", json=body,
                                      headers={**MCP_HEADERS, "Authorization": f"Bearer {v2}"})
            right = await client.post("http://localhost/mcp/", json=body,
                                      headers={**MCP_HEADERS, "Authorization": f"Bearer {v1}"})
    assert wrong.status_code == 401
    assert right.status_code == 200


async def test_claude_origin_is_explicitly_allowed_and_unknown_origins_are_not():
    v1_origins = mcp._allowed_origins("v1")
    v2_origins = mcp._allowed_origins("v2")
    assert "https://claude.ai" not in v1_origins
    assert v2_origins.count("https://claude.ai") == 1
    assert "https://attacker.example" not in v2_origins

    token = mcp_oauth.make_access_token(user_id=1, org_id=1,
                                        audience=mcp_oauth.mcp_resource_url("v2"))
    async with directory_session() as client:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        allowed = await client.post("http://localhost/mcp/v2/", json=body, headers={
            **MCP_HEADERS, "Authorization": f"Bearer {token}", "Origin": "https://claude.ai",
        })
        blocked = await client.post("http://localhost/mcp/v2/", json=body, headers={
            **MCP_HEADERS, "Authorization": f"Bearer {token}",
            "Origin": "https://attacker.example",
        })
    assert allowed.status_code == 200
    assert blocked.status_code == 403


async def test_team_mcp_does_not_inherit_the_claude_origin_permission():
    token = mcp_oauth.make_access_token(user_id=1, org_id=1,
                                        audience=mcp_oauth.mcp_resource_url("v1"))
    team_mcp = mcp.build_mcp_app(resource_version="v1")
    host = FastAPI()
    host.mount("/mcp", team_mcp)
    async with mcp.mcp_lifespan(team_mcp):
        async with AsyncClient(transport=ASGITransport(app=host),
                               base_url="http://localhost") as client:
            response = await client.post("/mcp/", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            }, headers={
                **MCP_HEADERS, "Authorization": f"Bearer {token}",
                "Origin": "https://claude.ai",
            })
    assert response.status_code == 403


def test_transport_factory_refuses_a_server_audience_mismatch():
    with pytest.raises(ValueError, match="same public surface"):
        mcp.build_mcp_app(server=mcp.directory_mcp, resource_version="v1")
    with pytest.raises(ValueError, match="same public surface"):
        mcp.build_mcp_app(server=mcp.mcp, resource_version="v2")
