"""HTTP adapter for a provider OAuth token endpoint."""

from __future__ import annotations

import time

import httpx

from ..domain.connections.refresh import _DEFAULT_TOKEN_URI


class HTTPXOAuthRefreshPort:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def exchange(self, blob: dict) -> dict:
        """Exchange the refresh_token for a new access token. Returns an updated blob."""
        rt, cid, csec = blob.get("refresh_token"), blob.get("client_id"), blob.get("client_secret")
        if not (rt and cid and csec):
            raise ValueError("oauth secret missing refresh_token / client_id / client_secret")
        # TikTok's token endpoint reads `client_key`; the blob records that at exchange time so a
        # refresh months later still speaks the dialect the grant was minted with.
        cid_param = blob.get("client_id_param") or "client_id"
        token_uri = blob.get("token_uri", _DEFAULT_TOKEN_URI)
        data = {"grant_type": "refresh_token", "refresh_token": rt, cid_param: cid}

        # Client authentication must match what the provider demands, same as exchange_code: X and
        # Pinterest REQUIRE HTTP Basic and reject a secret in the body. This bit the connect path first
        # and was fixed there — then every X connection died ~2h later anyway, because the REFRESH
        # still posted the secret in the body (the provider answered 401, surfaced to callers as
        # `502 oauth refresh failed`). The method is recorded in the blob at exchange time; blobs
        # minted before that field existed fall back to body auth, then RETRY once with Basic on a
        # 4xx — and stamp the method that worked so the next refresh skips the dance.
        method = blob.get("token_endpoint_auth_method")

        async def _post(basic: bool) -> httpx.Response:
            if basic:
                return await self.client.post(token_uri, data=data, auth=(cid, csec))
            return await self.client.post(token_uri, data={**data, "client_secret": csec})

        tried_basic = method == "client_secret_basic"
        resp = await _post(basic=tried_basic)
        if resp.status_code in (400, 401) and method is None:
            retry = await _post(basic=True)
            if retry.is_success:
                resp, tried_basic = retry, True
        resp.raise_for_status()
        tok = resp.json()
        access = tok.get("access_token")
        if not access:  # a 200 with an error-shaped body ({"error":"invalid_grant"}) — surface it clearly
            raise ValueError(f"token endpoint returned no access_token: {tok.get('error') or tok}")
        new = dict(blob)
        if tried_basic and method is None:  # learned by the retry — remember it for the next refresh
            new["token_endpoint_auth_method"] = "client_secret_basic"
        new["access_token"] = new["token"] = access  # update both common key names
        # Always stamp an expiry (fallback 1h). A provider that omits/nulls expires_in would otherwise
        # leave the token perpetually "unknown expiry" → is_stale True → a live refresh on EVERY call.
        new["expires_at"] = time.time() + float(tok.get("expires_in") or 3600)
        if tok.get("refresh_token"):  # providers may rotate the refresh token
            new["refresh_token"] = tok["refresh_token"]
        return new
