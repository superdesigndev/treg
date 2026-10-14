"""Pure OAuth consent-flow rules."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import urlencode


def pkce_challenge(verifier: str) -> str:
    """Return the S256 challenge for a PKCE verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


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
    if pending.code_verifier:
        query["code_challenge"] = pkce_challenge(pending.code_verifier)
        query["code_challenge_method"] = "S256"
    return f"{pending.auth_uri}?{urlencode(query)}"
