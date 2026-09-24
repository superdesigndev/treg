"""The injector contract — the seam that keeps the proxy core dumb.

A tool carries a LIST of bindings; the proxy applies each. A binding is a plain dict:
    {secret_id, injector, location: "header"|"query"|"json", name, format, secret_field, token_encode}
The proxy never branches on auth shape — it calls INJECTORS[binding["injector"]] per binding.
Underneath there are two mechanics: place a string (env, cli_auth) or pull a field from a
JSON blob (secret_file, oauth). Acquisition (CLI keychain / OAuth handshake / token file) is
onboarding's job. Adding a shape never touches the proxy.

A `location: "json"` binding is semantic JSON injection, not byte-faithful relay: the relay parses
and reserializes the object. Never use it for an upstream that signs or hashes the raw request body.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

# An injector places one decrypted secret into the outgoing (headers, params) per its binding.
Injector = Callable[[dict[str, str], list, dict[str, object] | None, dict, str], None]

INJECTORS: dict[str, Injector] = {}


def register(name: str) -> Callable[[Injector], Injector]:
    def deco(fn: Injector) -> Injector:
        INJECTORS[name] = fn
        return fn

    return deco


def ensure_base64(value: str) -> str:
    """Ensure a value is Base64-encoded for HTTP Basic auth.

    HTTP Basic providers (DataForSEO, Moz, PredictLeads) expect `login:password` Base64-encoded.
    The marketplace connect flow (`connect.test_api_credential`) Base64-encodes at paste time, so
    secrets added that way are already encoded. But `treg secret add <provider>` stores raw values.

    Detect already-encoded values the same way connect.py does: if the value decodes to printable
    text containing a colon, it is already Base64. A raw `login:password` cannot be mistaken for
    one (colon is not in the Base64 alphabet), and a Base64 blob cannot be a raw pair (no colon).
    """
    try:
        decoded = base64.b64decode(value, validate=True).decode()
        if ":" in decoded and decoded.isprintable():
            return value  # already Base64-encoded
    except Exception:  # noqa: BLE001 — not Base64 or not text: encode it
        pass
    return base64.b64encode(value.encode()).decode()


def _place(headers, params: list, json_body: dict[str, object] | None,
           binding: dict, value: str) -> None:
    """Put `value` where the binding declares. `headers` is a mapping that overwrites by name
    (dict or httpx.Headers); `params` is a list of (k, v) pairs (preserves duplicate caller
    params). For a query binding we drop any caller param of the same name so the injected
    credential wins, then append it."""
    # A pasted credential often carries the paste's trailing newline (echo/pbpaste both add one).
    # A newline is ILLEGAL in a header value, so httpx dies with an opaque 502 at call time —
    # sometimes months after the paste. Surrounding whitespace is never part of a real credential.
    cleaned = value.strip()
    # HTTP Basic providers declare `token_encode: "base64"`. Secrets added via the marketplace
    # connect flow are already encoded; secrets added via `treg secret add` are raw. Encode now
    # if needed, so both paths produce the same Authorization header.
    if binding.get("token_encode") == "base64":
        cleaned = ensure_base64(cleaned)
    rendered = binding.get("format", "{secret}").format(secret=cleaned)
    name = binding.get("name", "Authorization")
    location = binding.get("location", "header")
    if location == "query":
        params[:] = [(k, v) for (k, v) in params if k != name]
        params.append((name, rendered))
    elif location == "json":
        if json_body is None:
            raise ValueError("JSON credential injection requires a JSON object request body")
        json_body[name] = rendered
    else:
        headers[name] = rendered


def _token_from_json(blob: str, field: str) -> str:
    """Pull a token field out of a stored JSON secret (a `.secret`/OAuth token file)."""
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError("secret is not valid JSON for this injector") from exc
    if not isinstance(data, dict) or field not in data:
        raise ValueError(f"field {field!r} not found in secret JSON")
    val = data[field]
    if not isinstance(val, (str, int, float)) or isinstance(val, bool):
        # str(dict/list/None/bool) would inject garbage ("{'x': 1}", "None", "True") as the
        # credential — a confusing upstream 401 instead of a clear config error.
        raise ValueError(f"field {field!r} is {type(val).__name__}, expected a string token")
    return str(val)


# ---- string-value shapes ------------------------------------------------------------------
@register("env")
def env_injector(headers: dict[str, str], params: list, json_body: dict[str, object] | None,
                 binding: dict, secret: str) -> None:
    """Plain-string credential (ENV-style)."""
    _place(headers, params, json_body, binding, secret)


@register("cli_auth")
def cli_auth_injector(headers: dict[str, str], params: list, json_body: dict[str, object] | None,
                      binding: dict, secret: str) -> None:
    """Material lifted from a CLI's own config/keychain (e.g. stripe/gh). Placed like a string;
    the CLI-specific *extraction* happens during onboarding, not here."""
    _place(headers, params, json_body, binding, secret)


# ---- JSON-blob shapes ---------------------------------------------------------------------
@register("secret_file")
def secret_file_injector(headers: dict[str, str], params: list, json_body: dict[str, object] | None,
                         binding: dict, secret: str) -> None:
    """A `.secret/` token file (GCP, Google Ads, GSC): pull the field and place it."""
    _place(headers, params, json_body, binding,
           _token_from_json(secret, binding.get("secret_field", "access_token")))


@register("oauth")
def oauth_injector(headers: dict[str, str], params: list, json_body: dict[str, object] | None,
                   binding: dict, secret: str) -> None:
    """A stored OAuth token JSON: inject the access token.

    Auto-refresh on expiry is intentionally NOT here — refreshing is network + persistence,
    which belongs to the OAuth connect flow (Step 5), not to the hot injection path.
    """
    _place(headers, params, json_body, binding,
           _token_from_json(secret, binding.get("secret_field", "access_token")))


def inject(headers: dict[str, str], params: list, binding: dict, secret: str,
           *, json_body: dict[str, object] | None = None) -> None:
    injector = INJECTORS.get(binding.get("injector", "env"))
    if injector is None:
        raise ValueError(f"unknown injector: {binding.get('injector')!r}")
    injector(headers, params, json_body, binding, secret)
