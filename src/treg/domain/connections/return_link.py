"""Where a finished OAuth connect may send the browser back to.

A product starting a connect for its own user names a `return_to` URL; the callback redirects
there instead of showing treg's own page. Only an exact origin the team listed is accepted, so a
start request cannot turn treg's callback into a redirect to anywhere.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_ORIGINS = 20
MAX_URL_LENGTH = 2048
OUTCOME_PARAMS = ("treg_state", "treg_status")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class ReturnLinkError(ValueError):
    pass


def _bad_characters(value: str) -> bool:
    return any(ord(c) < 33 or ord(c) == 127 or c == "\\" for c in value)


def _origin(url: str, *, allow_loopback_http: bool) -> tuple[str, object]:
    """(origin, parsed URL) for an https URL, or a loopback http one where that is allowed."""
    if not url or len(url) > MAX_URL_LENGTH or _bad_characters(url):
        raise ReturnLinkError("must be an absolute https URL")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ReturnLinkError("must be an absolute https URL") from None
    host = (parts.hostname or "").lower()
    if not host or "@" in parts.netloc:
        raise ReturnLinkError("must be an absolute https URL with a host and no user name")
    scheme = parts.scheme.lower()
    if scheme == "http" and not (allow_loopback_http and host in _LOOPBACK):
        raise ReturnLinkError("must use https")
    if scheme not in ("https", "http"):
        raise ReturnLinkError("must use https")
    default_port = 443 if scheme == "https" else 80
    shown_host = f"[{host}]" if ":" in host else host
    origin = f"{scheme}://{shown_host}" + (f":{port}" if port and port != default_port else "")
    return origin, parts


def normalize_origin(value: str, *, allow_loopback_http: bool) -> str:
    """`https://app.example.com` exactly: no path, query, fragment or user name."""
    origin, parts = _origin(value.strip(), allow_loopback_http=allow_loopback_http)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ReturnLinkError(f"{value!r} is not an origin: send only scheme, host and port")
    return origin


def normalize_origins(values: list[str], *, allow_loopback_http: bool) -> list[str]:
    if len(values) > MAX_ORIGINS:
        raise ReturnLinkError(f"at most {MAX_ORIGINS} origins")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ReturnLinkError("each origin must be a string")
        try:
            origin = normalize_origin(value, allow_loopback_http=allow_loopback_http)
        except ReturnLinkError as exc:
            raise ReturnLinkError(f"{value!r}: {exc}") from None
        if origin not in out:
            out.append(origin)
    return out


def check_return_to(url: str, allowed: list[str] | None, *, allow_loopback_http: bool) -> str:
    """The `return_to` as given, when its origin is one the team listed; else ReturnLinkError."""
    origin, parts = _origin(url, allow_loopback_http=allow_loopback_http)
    if parts.fragment:
        raise ReturnLinkError("must not carry a fragment")
    if origin not in (allowed or []):
        raise ReturnLinkError(
            f"{origin} is not one of this team's return origins (PATCH /orgs/{{id}} oauth_return_origins)")
    return url


def with_outcome(url: str, *, state: str, status: str) -> str:
    """`url` with `treg_state` and `treg_status` set, replacing any copies it already had."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in OUTCOME_PARAMS]
    query += [("treg_state", state), ("treg_status", status)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
