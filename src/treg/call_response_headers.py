"""Selected upstream response headers that official clients may surface.

`/call/` already relays upstream headers to a raw HTTP client. MCP `call` and
`treg call` (plain and `--json`) copy the body and selected X-Treg-* /
Retry-After fields, and previously dropped the rest — so a resumable upload's
session URL in `Location` never reached the caller. Cookie and credential
headers are never included.

Keep the allowlist in sync with `cli/internal/output/call.go`.
"""

from __future__ import annotations

from collections.abc import Mapping

# Tight allowlist: headers a documented two-leg upload (or similar) needs.
SAFE_UPSTREAM_RESPONSE_HEADERS: tuple[str, ...] = (
    "location",
    "content-location",
    "content-range",
)

_CANONICAL_NAMES = {
    "location": "Location",
    "content-location": "Content-Location",
    "content-range": "Content-Range",
}


def selected_upstream_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Lower-cased allowlisted headers, omitting empty values."""
    if not headers:
        return {}
    get = getattr(headers, "get", None)
    if get is None:
        return {}
    out: dict[str, str] = {}
    for name in SAFE_UPSTREAM_RESPONSE_HEADERS:
        value = get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[name] = text
    return out


def attach_selected_upstream_headers(
    dest: dict, headers: Mapping[str, str] | None, *, key: str = "response_headers",
) -> None:
    """Set `dest[key]` only when at least one allowlisted header is present."""
    selected = selected_upstream_headers(headers)
    if selected:
        dest[key] = selected


def selected_upstream_header_lines(headers: Mapping[str, str] | None) -> list[str]:
    """Plain-CLI stderr lines, one per selected header, in allowlist order."""
    selected = selected_upstream_headers(headers)
    return [f"treg: {_CANONICAL_NAMES[name]}: {value}" for name, value in selected.items()]
