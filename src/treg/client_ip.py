"""Which address a request came from, decided once for every per-IP limiter, audit row and log.

`X-Forwarded-For` is a list each proxy appends to, so its FIRST entry is whatever the client
wrote: trusting it lets a script pick a fresh address per request and walk past every per-IP
limit. The address comes, in order, from:

1. `TREG_CLIENT_IP_HEADER`, a header only the edge can set (on a Cloudflare-fronted deployment
   `cf-connecting-ip`: Cloudflare refuses a request that carries its own copy);
2. the entry `TREG_TRUSTED_PROXY_HOPS` from the RIGHT of `X-Forwarded-For`, the one the nearest
   trusted proxy appended (default 1: one reverse proxy in front; 0 when clients connect directly);
3. the socket peer.
"""
from __future__ import annotations

import ipaddress
import logging
import time
from collections.abc import Mapping

from .config import get_settings

log = logging.getLogger("treg")
_MISSING_LOG_EVERY_S = 60.0
_last_missing_log = 0.0
_shape_reported = False
_EDGE_HEADERS = ("cf-connecting-ip", "true-client-ip", "x-real-ip", "forwarded")


def _report_shape(lower: Mapping[str, str]) -> None:
    """Once per process: which address headers the deployment's proxies deliver, names and counts
    only, never values, so an operator can tell what TREG_CLIENT_IP_HEADER can rely on."""
    global _shape_reported
    if _shape_reported:
        return
    _shape_reported = True
    present = ",".join(h for h in _EDGE_HEADERS if h in lower) or "-"
    hops = len([p for p in (lower.get("x-forwarded-for") or "").split(",") if p.strip()])
    log.info("event=client_ip_sources headers=%s forwarded_for_entries=%d", present, hops)


def _ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def _note_missing(header: str) -> None:
    """The configured header was absent or not an address: the edge did not set it (or a hop
    stripped it), so this request fell back. Logged at most once a minute per process."""
    global _last_missing_log
    now = time.monotonic()
    if now - _last_missing_log >= _MISSING_LOG_EVERY_S:
        _last_missing_log = now
        log.warning("event=client_ip_header_missing header=%s", header)


def from_headers(headers: Mapping[str, str], peer: str | None) -> str:
    """The client address from request headers (any casing) and the socket peer; '?' if none."""
    lower = {k.lower(): v for k, v in headers.items()}
    if lower:
        _report_shape(lower)
    s = get_settings()
    name = s.client_ip_header.strip().lower()
    if name:
        trusted = _ip(lower.get(name))
        if trusted:
            return trusted
        if "x-forwarded-for" in lower:
            # Proxied yet unmarked: the edge did not set it. A request with no X-Forwarded-For
            # never crossed a proxy (a health check, an in-process relay), so silence there.
            _note_missing(name)
    hops = s.trusted_proxy_hops
    if hops > 0:
        chain = [part.strip() for part in (lower.get("x-forwarded-for") or "").split(",") if part.strip()]
        if len(chain) >= hops:
            appended = _ip(chain[-hops])
            if appended:
                return appended
    return _ip(peer) or (peer or "?")


def of(request) -> str:
    """The client address of a Starlette/FastAPI request."""
    return from_headers(request.headers, request.client.host if request.client else None)


def forwarding_headers(ip: str) -> dict[str, str]:
    """Headers that make an in-process relay (MCP -> API) resolve to `ip` under the same rules."""
    if not _ip(ip):
        return {}
    headers = {"X-Forwarded-For": ", ".join([ip] * max(1, get_settings().trusted_proxy_hops))}
    name = get_settings().client_ip_header.strip()
    if name:
        headers[name] = ip
    return headers
