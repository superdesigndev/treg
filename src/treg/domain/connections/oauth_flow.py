"""Pure OAuth consent-flow rules."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import urlencode, urlsplit


def pkce_challenge(verifier: str) -> str:
    """Return the S256 challenge for a PKCE verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def pkce_challenge_hex(verifier: str) -> str:
    """TikTok's desktop PKCE: the SHA-256 of the verifier as lowercase hex, not base64url."""
    return hashlib.sha256(verifier.encode()).hexdigest()


def is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def consent_url(pending) -> str:
    """Build the provider consent URL from a stored pending connection."""
    query = {
        getattr(pending, "client_id_param", "") or "client_id": pending.client_id,
        "redirect_uri": pending.redirect_uri,
        "response_type": "code",
        "scope": pending.scopes,
        "state": pending.state,
    }
    query.update(
        json.loads(pending.auth_params)
        if pending.auth_params else {"access_type": "offline", "prompt": "consent"}
    )
    # A provider that hashes the verifier its own way stored its challenge in auth_params.
    if pending.code_verifier and "code_challenge" not in query:
        query["code_challenge"] = pkce_challenge(pending.code_verifier)
        query["code_challenge_method"] = "S256"
    return f"{pending.auth_uri}?{urlencode(query)}"
