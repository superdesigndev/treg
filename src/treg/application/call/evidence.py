"""Redaction and bounded failure evidence for proxied calls."""

from __future__ import annotations

import base64
import json
import logging
import re
import zlib
from urllib.parse import quote, quote_plus, unquote

from ... import crypto
from ...config import get_settings
from ...infra.upstream import injectors
from ...models import Secret, Tool


# ---- failure evidence: what a failed call is allowed to leave behind ----------------------------
# Sized to hold a real provider error whole — a typical 400 body is 80-300 characters and a verbose
# JSON one about 800 — while still capping a ~14KB CDN error page and a caller stuck in a retry loop.
_ERROR_RESPONSE_MAX = 2000
_ERROR_REQUEST_MAX = 1000
# Unmetered calls keep streaming unless the caller explicitly declared a small body. Starlette's
# request cache then lets relay replay those exact bytes without a second read from the socket.
_ERROR_CALLER_BODY_MAX = 64 * 1024
# Sliced off the FRONT before any decode, so an 8MB single-line HTML error page never gets decoded or
# regex-scanned on the request path. Every limit above is characters; this one is bytes.
_ERROR_BODY_SLICE = 8192
_ERROR_MASKING_FAILED = "<redacted: could not render credentials for masking>"

# Third-party secret shapes. `_EVIDENCE_SECRET_RE` below covers values that LOOK like a key; these
# two cover the places a value hides by its NAME instead — in a URL or a JSON body — which
# `_CRED_FLAG_EQ_RE` misses because it requires a leading dash (it was written for argv, where
# `--token=x` is the only shape).
_QUERY_CRED_RE = re.compile(
    r"(?i)((?:api[-_]?key|apikey|key|token|secret|password|passwd|pwd|auth|access[-_]?token"
    r"|sig|signature)\"?\s*[=:]\s*\"?)[^&\s\"',}]+")
_URL_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")

# `_ARGV_SECRET_RE`'s catch-all masks ANY 24+ run of [A-Za-z0-9_-], which is right for an argv log and
# wrong here: it deletes 100% of provider correlation identifiers — UUIDs, ULIDs, 32-char trace ids,
# request ids — which are exactly what you quote to a provider's support desk. Measured on real error
# bodies: the prose always survived, the correlation field never did. So for evidence we keep the
# TARGETED half (known key prefixes, JWTs) and drop the catch-all. Platform credentials do not depend
# on it — they have exact masking plus the fail-closed backstop below — and the owner has accepted
# that a third-party secret may occasionally survive here.
_EVIDENCE_SECRET_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|AKIA|ASIA|AIza|xox[baprs])[A-Za-z0-9_\-]{6,}\b"
    # Anchored + possessive for the same reason as the argv rule above, and it matters MORE here:
    # this one runs on a PROVIDER's response body, which is uncontrolled input on the request path.
    r"|\beyJ[A-Za-z0-9_\-]++\.[A-Za-z0-9_.\-]{8,}")

# Response headers worth keeping on a FAILED platform call. An empty-bodied 401 or 429 is otherwise
# undiagnosable, and these say which of "bad credential" / "wrong scheme" / "quota gone" / "retry in
# N" it was. Allowlisted, never the whole bag: `authorization` and `set-cookie` live in there too.
_EVIDENCE_HEADERS = (
    "retry-after", "www-authenticate",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
    "x-request-id", "x-requestid", "request-id", "x-correlation-id", "x-amzn-requestid",
    "cf-ray", "x-trace-id",
)


_SENSITIVE_JSON_SECRET_KEYS = {
    "access_token", "refresh_token", "id_token", "token", "client_secret", "api_key", "secret",
    "password", "private_key",
}


def _secret_renderings(tool: Tool, secrets: dict[int, Secret]) -> list[str]:
    """Every spelling of every injected credential for this tool, longest first.

    This is the primary defence and the only deterministic one: platform credentials come from a
    named setting and org credentials from an encrypted Secret, so both can be matched exactly instead
    of guessed at. Providers routinely quote the offending request back inside a 400/401 body — the
    header they received, or the full URL including the query — and a key can survive
    `_EVIDENCE_SECRET_RE` by simply not looking like a known key shape. Exact substring masking is why
    the deterministic layer carries the weight here and the pattern layer is only a net.

    treg injects the value verbatim, but a PROVIDER may hand it back transformed, and a transform it
    can reverse is one we have to anticipate. Four families, all observed shapes rather than guesses:

    * the raw value, and the value after the binding's `format` (`Bearer {secret}`, `Basic {secret}`);
    * percent-encoded — twelve providers authenticate by query param, so the key comes back inside an
      echoed URL. Both cases: `quote()` emits UPPERCASE hex (`%2F`) and plenty of servers echo lower;
    * JSON-escaped, because a body quoting a URL usually writes `\\/` for `/`;
    * **the DECODED halves of a Basic credential.** `config.py` states that dataforseo's platform
      value is already the base64 of `login:password`. A provider that decodes Basic auth and reports
      `{"received_username": …, "received_password": …}` echoes treg's credential in a form where
      neither the base64 blob nor `Basic <blob>` appears. dataforseo is the largest provider by
      spend, so this is the opposite of theoretical.
    """
    out: set[str] = set()

    def add(value: str) -> None:
        """One secret and every spelling of it a provider might echo back."""
        if not value or len(value) < 4:
            return  # too short to mask without redacting half the message
        enc = quote(value, safe="")
        out.update({value, enc, enc.lower(), quote_plus(value), value.replace("/", "\\/"),
                    json.dumps(value, ensure_ascii=False)[1:-1]})

    def add_credential(value: str) -> None:
        add(value)
        for part in _basic_credential_parts(value):  # mask what a provider can DECODE
            add(part)

    for binding in tool.bindings or []:
        fmt = str(binding.get("format") or "{secret}")
        # A constant provider header can share the binding's credential reference so the normal
        # injector owns the whole protocol shape, but it does not inject that credential. Treating
        # its literal format as a secret spelling masked ordinary dates such as Crustdata's API
        # version from every failure-evidence snippet.
        if "{secret}" not in fmt:
            continue
        setting = binding.get("platform_setting")
        if setting:
            value = getattr(get_settings(), setting, None)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            add_credential(value)
            add(fmt.format(secret=value))
            continue

        sid = binding.get("secret_id")
        if sid is None:
            continue
        plain = crypto.decrypt(secrets[sid].value)
        add_credential(plain)
        injector = binding.get("injector", "env")
        if injector not in ("oauth", "secret_file"):
            add(fmt.format(secret=plain.strip()))
            continue

        field = str(binding.get("secret_field") or "access_token")
        token = injectors._token_from_json(plain, field)
        add_credential(token)
        add(f"Bearer {token}")
        add(fmt.format(secret=token.strip()))
        data = json.loads(plain)
        if not isinstance(data, dict):
            raise ValueError("JSON credential is not an object")
        sensitive = _SENSITIVE_JSON_SECRET_KEYS | {field.lower()}
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in sensitive and isinstance(value, str):
                add_credential(value)
    # Longest first so `Bearer abc` is masked as a unit before the bare `abc` inside it turns the
    # line into `Bearer ***` — same result here, but the ordering stops a shorter secret that is a
    # substring of a longer one from fragmenting it into an unmatchable remainder.
    return sorted((s for s in out if len(s) >= 4), key=len, reverse=True)


def _safe_secret_renderings(tool: Tool, secrets: dict[int, Secret]) -> list[str] | None:
    """Render credentials for masking, or signal that evidence must be replaced wholesale."""
    try:
        return _secret_renderings(tool, secrets)
    except Exception as exc:  # noqa: BLE001 — malformed/encrypted credentials must fail closed
        logging.getLogger("treg").warning("could not render credentials for error masking: %s", exc)
        return None


def _basic_credential_parts(value: str) -> list[str]:
    """`login:password` and its two halves, when `value` is the base64 of a Basic credential.

    Returns [] for anything that is not — an ordinary API key rarely base64-decodes to printable text
    containing a colon, and a false positive here only costs an extra (harmless) mask.
    """
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001 — not base64, or not text: simply not a Basic credential
        return []
    if ":" not in decoded or not decoded.isprintable():
        return []
    login, _, password = decoded.partition(":")
    return [p for p in (decoded, login, password) if p]


def _decode_error_body(raw: bytes, content_encoding: str = "", content_type: str = "") -> str:
    """Bytes off the wire → something a human can read, or an honest marker saying why not.

    `force_identity` asks the provider not to compress a metered response, but a CDN or WAF error page
    is generated at the edge and answers however it likes — and those 403s are exactly the responses
    this feature exists to explain. `relay` streams `aiter_raw()`, so nothing has decoded them.
    """
    if not raw:
        return ""
    enc = (content_encoding or "").strip().lower()
    if enc and enc != "identity":
        if enc not in ("gzip", "deflate"):  # br, zstd — no stdlib decoder we can rely on
            return f"<{enc}-encoded, {len(raw)} bytes, not decoded>"
        try:
            # INCREMENTAL and capped just past the evidence limit. `gzip.decompress` is unbounded, so
            # slicing the INPUT to 8KiB does not bound the OUTPUT: 20MB of one repeated byte
            # compresses to under 20KB, and a bomb would expand to megabytes that four regexes then
            # walk synchronously on the request path.
            d = zlib.decompressobj(16 + zlib.MAX_WBITS if enc == "gzip" else -zlib.MAX_WBITS)
            raw = d.decompress(raw, _ERROR_RESPONSE_MAX * 4)
        except Exception:  # noqa: BLE001 — a truncated slice of a gzip stream is expected to fail
            return f"<{enc}-encoded, {len(raw)} bytes, undecodable>"
    text = raw.decode("utf-8", "replace")
    # A binary payload decoded with errors="replace" is a wall of U+FFFD that says nothing. Report the
    # shape instead, keeping a short hex head so the content type is still identifiable.
    if text.count("�") > len(text) // 5 or ("\x00" in text[:512]):
        return f"<binary {content_type or 'response'}, {len(raw)} bytes, head={raw[:32].hex()}>"
    return text


def _caller_request_snippet(
    query_items: tuple[tuple[str, str], ...], content_type: str, tool: Tool,
    caller_body: bytes, secrets: list[str],
) -> str:
    """What the CALLER actually sent, redacted — the half of a failure treg otherwise forgets.

    `CallRecord.path` stores the catalog's upstream URL with only `{placeholder}` path params filled,
    so the caller's real query and body survive nowhere else (`params_hash` is one-way). Without this
    a 400 cannot be explained even when the provider says exactly what was wrong with it.

    Query params are read from the INBOUND request, which never carries an injected credential:
    injection builds a separate outbound list (see proxy.relay). The binding's own query names are
    dropped anyway, for the caller who passed a value into the slot the injector overwrites.
    """
    drop = {b.get("name", "Authorization") for b in (tool.bindings or [])
            if b.get("location", "header") == "query"}
    parts = []
    pairs = [f"{k}={v}" for k, v in query_items if k not in drop]
    if pairs:
        parts.append("?" + "&".join(pairs))
    if caller_body:
        parts.append(_decode_error_body(caller_body[:_ERROR_BODY_SLICE], "",
                                        content_type))
    return _redact_snippet(" ".join(parts), secrets, _ERROR_REQUEST_MAX)


def _redact_snippet(text: str, secrets: list[str], limit: int) -> str:
    """Mask, THEN truncate — never the other way round.

    Truncating first can cut a 40-character token down to a 12-character survivor that no longer
    matches the 24+ rule, which is how a "redacted" field ends up holding half a key. `_redact_argv`
    already gets this order right; this follows it.
    """
    if not text:
        return ""
    for secret in secrets:  # exact and deterministic, before any pattern guessing
        text = text.replace(secret, "***")
    text = _URL_USERINFO_RE.sub("://***:***@", text)
    text = _QUERY_CRED_RE.sub(r"\1***", text)
    text = _EVIDENCE_SECRET_RE.sub("***", text)
    text = " ".join(text.split())  # collapse newlines/indentation; these are read in a table
    # Fail closed. Everything above is a list of transforms we thought of; this asks whether a secret
    # survived one we did not, by re-checking a NORMALISED copy (percent-decoded, JSON-unescaped,
    # lowercased). If one is still there, drop the whole snippet: losing a debugging message is a bad
    # day, leaking the credential every tenant shares is a much worse one.
    if secrets:
        probe = unquote(text.replace("\\/", "/")).lower()
        if any(s.lower() in probe for s in secrets):
            return "<redacted: a credential survived masking>"
    if len(text) <= limit:
        return text
    # Truncation can expose a partial token at the seam that was safe only while whole.
    return re.sub(r"[A-Za-z0-9_\-+/=.]{8,}$", "***", text[:limit]) + "…"

def _error_response_evidence(
    raw_headers: tuple[tuple[bytes, bytes], ...], body: bytes, secrets: list[str],
) -> str:
    """Build the redacted provider half of a failed-call evidence row."""
    # Headers first: a 401 or 429 often has an empty or generic body, and `Retry-After` /
    # `WWW-Authenticate` / the rate-limit trio are then the entire diagnosis.
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in raw_headers
    }
    hdrs = " ".join(f"{h}={headers[h]}" for h in _EVIDENCE_HEADERS if headers.get(h))
    evidence = _redact_snippet(
        (f"[{hdrs}] " if hdrs else "") +
        _decode_error_body(body[:_ERROR_BODY_SLICE],
                           headers.get("content-encoding", ""),
                           headers.get("content-type", "")),
        secrets, _ERROR_RESPONSE_MAX)
    return evidence or "<no response body or headers>"
