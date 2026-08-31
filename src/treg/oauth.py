"""OAuth freshness — treg owns keeping tokens alive. The injector stays dumb; this runs in the
api layer just before a call, and in the health runner.

An oauth secret is a SELF-REFRESHABLE blob:
    {access_token|token, refresh_token, expires_at|expiry, token_uri, client_id, client_secret}
`ensure_fresh` refreshes in place (re-encrypt + persist) when the token is stale. A single-flight
lock per secret id prevents a refresh stampede when many calls hit an expired token at once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from . import crypto
from .domain.connections.refresh import (
    EXPIRING_SOON_DAYS,
    _DEFAULT_TOKEN_URI,
    _SKEW,
    _expires_at,
    _locks,
    connection_view,
    ensure_fresh as _ensure_fresh,
    expiry_of,
    expiry_state,
    is_refreshable,
    is_stale,
    secret_is_refreshable,
)
from .infra.oauth_refresh import HTTPXOAuthRefreshPort
from .models import PendingOAuth, Secret


async def refresh(blob: dict, client: httpx.AsyncClient) -> dict:
    return await HTTPXOAuthRefreshPort(client).exchange(blob)


async def ensure_fresh(secret: Secret, db: AsyncSession, client: httpx.AsyncClient) -> None:
    await _ensure_fresh(secret, db, HTTPXOAuthRefreshPort(client))

# ---- connect flow (Phase C): mint the first token via browser consent --------------------
def pkce_challenge(verifier: str) -> str:
    """S256 challenge for a PKCE verifier (base64url, no padding)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def consent_url(p: PendingOAuth) -> str:
    """The provider consent URL the user opens.

    access_type=offline + prompt=consent are Google's way of guaranteeing a refresh_token, so the
    credential lands in auto-refresh mode. Providers that want different parameters carry them on
    the registry entry (`auth_params`), which replaces these defaults entirely."""
    q = {
        # TikTok reads `client_key`; everyone else reads the OAuth2 `client_id`.
        getattr(p, "client_id_param", "") or "client_id": p.client_id,
        "redirect_uri": p.redirect_uri,
        "response_type": "code",
        # `scopes` is stored in the provider's own delimiter (space, or comma for TikTok), so it
        # goes onto the URL verbatim — re-joining here would undo that.
        "scope": p.scopes,
        "state": p.state,
    }
    q.update(json.loads(p.auth_params) if p.auth_params else {"access_type": "offline", "prompt": "consent"})
    if p.code_verifier:  # PKCE — X rejects an authorization code exchanged without it
        q["code_challenge"] = pkce_challenge(p.code_verifier)
        q["code_challenge_method"] = "S256"
    return f"{p.auth_uri}?{urlencode(q)}"


async def exchange_code(p: PendingOAuth, code: str, client: httpx.AsyncClient) -> dict:
    """Trade the authorization code for tokens; return a self-refreshable oauth blob."""
    client_secret = crypto.decrypt(p.client_secret)
    cid_param = getattr(p, "client_id_param", "") or "client_id"
    data = {
        "code": code,
        cid_param: p.client_id,
        "redirect_uri": p.redirect_uri,
        "grant_type": "authorization_code",
    }
    if p.code_verifier:
        data["code_verifier"] = p.code_verifier
    kwargs: dict = {}
    if p.token_endpoint_auth_method == "client_secret_basic":
        # X's confidential clients REQUIRE HTTP Basic; sending the secret in the body is rejected.
        kwargs["auth"] = (p.client_id, client_secret)
    else:
        data["client_secret"] = client_secret
    resp = await client.post(p.token_uri, data=data, **kwargs)
    resp.raise_for_status()
    tok = resp.json()
    access = tok.get("access_token")
    if not access:  # a 200 with an error-shaped body — surface the provider's reason, not a KeyError
        raise ValueError(f"token endpoint returned no access_token: {tok.get('error') or tok}")
    blob = {
        "access_token": access,
        "token": access,
        "refresh_token": tok.get("refresh_token"),
        # Always stored under the canonical key — `is_refreshable` and every reader look for
        # "client_id". Only the wire spelling differs, and that travels as client_id_param below.
        "client_id": p.client_id,
        "client_secret": client_secret,
        "token_uri": p.token_uri,
        "expires_at": time.time() + float(tok.get("expires_in") or 3600),
    }
    if cid_param != "client_id":  # TikTok — refresh must post client_key, not client_id
        blob["client_id_param"] = cid_param
    if p.token_endpoint_auth_method and p.token_endpoint_auth_method != "client_secret_post":
        # X / Pinterest demand HTTP Basic at the token endpoint. refresh() has no PendingOAuth to
        # ask months from now, so the blob itself must remember — omitting this is exactly the bug
        # where connect succeeded and every refresh 401'd.
        blob["token_endpoint_auth_method"] = p.token_endpoint_auth_method
    if getattr(p, "long_lived_exchange", False):
        blob = await _extend_meta_token(blob, client)
    return blob


async def _extend_meta_token(blob: dict, client: httpx.AsyncClient) -> dict:
    """Swap Meta's short-lived user token for the ~60-day one.

    Meta's authorization-code exchange returns a token good for an hour or two and no
    refresh_token, so a connection made this way is dead by the time anyone uses it. This second
    call is the only way to get a durable user credential out of Facebook Login.

    A failure here is deliberately NOT fatal: the short-lived token is still a working credential,
    and refusing the whole connect would be a worse outcome than a connection the user has to
    remake sooner. `expires_at` keeps telling the truth either way, which is what `needs_reconnect`
    reads.
    """
    resp = await client.get(
        blob["token_uri"],
        params={
            "grant_type": "fb_exchange_token",
            "client_id": blob["client_id"],
            "client_secret": blob["client_secret"],
            "fb_exchange_token": blob["access_token"],
        },
    )
    if resp.status_code != 200:
        return blob
    tok = resp.json()
    access = tok.get("access_token")
    if not access:
        return blob
    return {
        **blob,
        "access_token": access,
        "token": access,
        # Meta omits expires_in when it issues a non-expiring token (system users, some business
        # tokens). Falling back to the 60-day default would then invent an expiry that isn't real
        # and nag the user to reconnect a credential that never dies.
        "expires_at": time.time() + float(tok["expires_in"]) if tok.get("expires_in") else None,
    }
