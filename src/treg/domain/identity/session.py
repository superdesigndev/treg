"""Signed browser sessions and identity tokens.

Both are tiny stateless HMAC tokens, but newly minted credentials carry a signed ``aud`` claim so a
browser session can never be replayed as an ``X-Treg-Token`` bearer (or vice versa). Legacy tokens
predate that claim; the readers below keep only the compatibility that can be distinguished safely.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
import time

from ...config import get_settings

TTL_SECONDS = 7 * 24 * 3600
BOOTSTRAP_TTL_SECONDS = 7 * 24 * 3600
COOKIE = "treg_session"
SESSION_AUDIENCE = "session"
IDENTITY_AUDIENCE = "identity"
BOOTSTRAP_SCOPE = "bootstrap"
TEAM_SCOPE = "team"

# When no signing secret is configured we fall back to a RANDOM per-process key (mirrors
# crypto._EPHEMERAL), NOT a source-visible constant: a static "dev-session-key" would let anyone
# who reads the code forge a session cookie for any user id (incl. a superadmin) — full auth
# bypass. Ephemeral means sessions simply don't survive a restart, the intended loud signal to
# set TREG_SESSION_SECRET / TREG_SECRET_KEY.
_EPHEMERAL_KEY = _secrets.token_bytes(32)


def _key() -> bytes:
    s = get_settings()
    configured = s.session_secret or s.secret_key
    return configured.encode() if configured else _EPHEMERAL_KEY


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _make(
    user_id: int,
    *,
    audience: str,
    ttl: int | None,
    token_version: int,
    org: str | None = None,
    key_generation: int | None = None,
    scope: str | None = None,
) -> str:
    # `tv` binds the token to the user's current token_version; bumping that row invalidates every
    # token minted at an older version (see api._revoke path). Callers pass user.token_version.
    #
    # `org` is optional and stateless like the rest of the claim: an identity token that PINS a team.
    # It exists so a copyable "API key" works as a bare bearer where no `X-Treg-Org` header can travel
    # (an MCP server's Authorization header). Team-pinned Default keys additionally carry `kg`, a
    # per-team generation that makes rotation invalidate only the prior token for that membership,
    # and `scp=team` makes that signed org authoritative. Omitted-org `scp=bootstrap` credentials are
    # short-lived and limited to account onboarding.
    #
    claims = {"uid": user_id, "tv": token_version, "aud": audience}
    if ttl is not None:
        claims["exp"] = int(time.time()) + ttl
    if org:
        claims["org"] = org
    if key_generation is not None:
        claims["kg"] = key_generation
    if scope:
        claims["scp"] = scope
    raw = json.dumps(claims, separators=(",", ":")).encode()
    sig = hmac.new(_key(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def make_session(user_id: int, ttl: int = TTL_SECONDS, token_version: int = 0) -> str:
    """Mint a time-bounded browser credential that bearer paths always reject."""
    return _make(
        user_id, audience=SESSION_AUDIENCE, ttl=ttl, token_version=token_version,
    )


def make_identity(
    user_id: int,
    token_version: int = 0,
    org: str | None = None,
    *,
    ttl: int | None = None,
    key_generation: int | None = None,
    scope: str | None = None,
) -> str:
    """Mint a bearer credential. Copied API keys use the no-expiry default; the MCP OAuth bridge
    passes a short TTL for its internal exchange token. Both remain revocable through ``tv``."""
    return _make(
        user_id, audience=IDENTITY_AUDIENCE, ttl=ttl,
        token_version=token_version, org=org, key_generation=key_generation, scope=scope,
    )


def _read_claims(token: str) -> dict | None:
    """Verify the signature and normalize claims without deciding how the token may be used."""
    if not token or "." not in token:
        return None
    try:
        p, s = token.split(".", 1)
        raw = _unb64(p)
        expected = hmac.new(_key(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(s), expected):
            return None
        data = json.loads(raw)
        out = {"uid": int(data["uid"]), "tv": int(data.get("tv", 0))}
        if data.get("exp") is not None:
            out["exp"] = int(data["exp"])
        if data.get("org"):
            out["org"] = str(data["org"])
        if data.get("kg") is not None:
            out["kg"] = int(data["kg"])
        if data.get("aud") is not None:
            out["aud"] = str(data["aud"])
        if data.get("scp") is not None:
            out["scope"] = str(data["scp"])
        return out
    except Exception:  # noqa: BLE001 — any malformed credential is simply invalid
        return None


def read_session_claims(cookie: str) -> dict | None:
    """Read a browser session. New identity tokens are rejected by signed audience; an untyped
    legacy token is accepted only while its required ``exp`` is live."""
    claims = _read_claims(cookie)
    if claims is None or claims.get("aud") not in (None, SESSION_AUDIENCE):
        return None
    if claims.get("exp", 0) < time.time():
        return None
    return claims


def read_identity_claims(token: str) -> dict | None:
    """Read an identity bearer token.

    Typed session credentials are always rejected. Typed identity credentials honor ``exp`` when
    one was deliberately supplied (the MCP OAuth bridge or seven-day bootstrap), while normal copied
    team keys omit it.

    Legacy untyped credentials are inherently ambiguous. An ``org`` claim safely identifies a
    team-pinned copied key, so it remains valid even after its old 30-day ``exp``. An untyped token
    with no ``exp`` is also identity-only. An org-less token carrying ``exp`` is accepted only until
    that timestamp; accepting it after expiry would also turn an expired legacy browser cookie into
    a bearer token.
    """
    claims = _read_claims(token)
    if claims is None:
        return None
    audience = claims.get("aud")
    if audience == SESSION_AUDIENCE:
        return None
    if audience == IDENTITY_AUDIENCE:
        if claims.get("exp") is not None and claims["exp"] < time.time():
            return None
        return claims
    if audience is not None:  # another signed token family, including MCP OAuth access tokens
        return None
    if claims.get("org") or claims.get("exp") is None:
        return claims
    return claims if claims["exp"] >= time.time() else None


def read_session(cookie: str) -> int | None:
    """Return the user id from a valid session. Token-version checks require a DB-aware caller."""
    claims = read_session_claims(cookie)
    return claims["uid"] if claims else None
