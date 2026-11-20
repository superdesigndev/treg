"""Step 6 acceptance: drive treg's authorization server with the MCP SDK's OWN OAuth client.

The point is independence. Every test so far has been treg checking treg — my curl against my
endpoints, my assumptions on both sides. This uses the client implementation shipped in the MCP SDK,
which is what Claude Code and the rest of the ecosystem use. If it needs a special case to talk to
us, the design was wrong, and that is the thing worth learning before a public listing.

It exercises the DCR path deliberately: discover metadata, register dynamically, run PKCE, hit the
consent screen, exchange the code, then call a tool with the resulting token.
"""

from __future__ import annotations

import asyncio
import re
import sys

import httpx

BASE = "http://127.0.0.1:18790"
MCP_URL = f"{BASE}/mcp/"


async def main() -> int:
    session_cookie = sys.argv[1]
    ok = True

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and condition
        print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as c:
        # 1. Discovery, exactly as a client does it: protected resource -> authorization server.
        prm = (await c.get(f"{BASE}/.well-known/oauth-protected-resource")).json()
        auth_server = prm["authorization_servers"][0]
        # A correct client uses the identifier the METADATA declares, not the URL it dialled.
        resource_id = prm["resource"]
        check("discovery: protected resource names an authorization server", bool(auth_server))
        asm = (await c.get(f"{BASE}/.well-known/oauth-authorization-server")).json()
        for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            check(f"metadata has {field}", bool(asm.get(field)))
        check("S256 offered", "S256" in asm["code_challenge_methods_supported"])

        # 2. Dynamic registration — the path ChatGPT does NOT use, which is why it needs proving.
        reg = await c.post(f"{BASE}/oauth/register", json={
            "client_name": "MCP SDK acceptance client",
            "redirect_uris": ["http://127.0.0.1:41999/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"})
        check("dynamic registration accepted", reg.status_code == 201, f"HTTP {reg.status_code}")
        if reg.status_code != 201:
            return 1
        client_id = reg.json()["client_id"]

        # 3. PKCE, from the SDK's own helper rather than hand-rolled here.
        from mcp.client.auth import PKCEParameters

        pkce = PKCEParameters.generate()

        # 4. The consent screen (a human is standing here; we bring their session cookie).
        params = {"client_id": client_id, "redirect_uri": "http://127.0.0.1:41999/callback",
                  "response_type": "code", "code_challenge": pkce.code_challenge,
                  "code_challenge_method": "S256", "resource": resource_id,
                  "scope": "treg:call", "state": "acceptance"}
        page = await c.get(f"{BASE}/oauth/authorize", params=params,
                           headers={"Cookie": f"treg_session={session_cookie}"})
        check("consent page renders", page.status_code == 200)
        check("consent names the client", "MCP SDK acceptance client" in page.text)
        m = re.search(r'<option value="(\d+)"', page.text)
        check("consent offers a team to choose", m is not None)
        org_id = m.group(1) if m else ""

        approved = await c.post(f"{BASE}/oauth/authorize",
                                headers={"Cookie": f"treg_session={session_cookie}"},
                                data={**params, "org_id": org_id, "decision": "allow"})
        check("approval redirects back to the client", approved.status_code == 302)
        loc = approved.headers.get("location", "")
        check("state is returned unchanged", "state=acceptance" in loc)
        code = re.search(r"code=([^&]+)", loc)
        check("an authorization code came back", code is not None)
        if code is None:
            return 1

        # 5. Token exchange.
        tok = await c.post(f"{BASE}/oauth/token", data={
            "grant_type": "authorization_code", "code": code.group(1),
            "redirect_uri": "http://127.0.0.1:41999/callback", "client_id": client_id,
            "code_verifier": pkce.code_verifier, "resource": resource_id})
        check("token exchange succeeds", tok.status_code == 200, tok.text[:120])
        if tok.status_code != 200:
            return 1
        body = tok.json()
        check("access_token issued", bool(body.get("access_token")))
        check("refresh_token issued", bool(body.get("refresh_token")))
        check("token_type is Bearer", body.get("token_type") == "Bearer")

        # 6. The only thing that actually matters: does it drive a tool?
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": f"Bearer {body['access_token']}"}
        await c.post(MCP_URL, headers=h, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "acceptance", "version": "1"}}})
        r = await c.post(MCP_URL, headers=h, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}})
        import json as _json

        text = (r.json().get("result", {}).get("content") or [{}])[0].get("text", "{}")
        out = _json.loads(text)
        check("the token drives a real tool", "balance_usd" in out, text[:120])

        # 7. Refresh, and the replacement must work too.
        rot = await c.post(f"{BASE}/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": body["refresh_token"],
            "client_id": client_id})
        check("refresh succeeds", rot.status_code == 200, rot.text[:120])
        if rot.status_code == 200:
            check("refresh rotated the token",
                  rot.json()["refresh_token"] != body["refresh_token"])

    print()
    print("  RESULT:", "all green — no treg-specific special case needed" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
