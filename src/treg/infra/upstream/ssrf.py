"""Framework-neutral SSRF guards for upstream and webhook targets."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_global also accepts multicast and the NAT64 translation prefix 64:ff9b::/96,
    # including mappings to non-global IPv4 addresses. is_reserved blocks that prefix.
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def safe_webhook_url(url: str | None) -> bool:
    """Allow only globally routable unicast addresses for HTTP(S) webhook targets.

    DNS names are checked at call time; local names and non-public literals are refused here
    so webhooks cannot target metadata endpoints or internal services through blind SSRF.
    """
    if not url:
        return False
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not a standard literal — but decimal/hex/octal/short forms (2130706433, 0x7f000001, 127.1) still
        # resolve to an IP and would reach loopback/internal. Normalize via inet_aton and re-check; a real
        # DNS name fails inet_aton and gets the best-effort allow (call-time resolution catches rebinding).
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except (OSError, ValueError):
            return True  # a genuine DNS name
    return _is_public_ip(ip)


def host_is_public(host: str) -> bool:
    """Call-time SSRF guard: RESOLVE `host` and require every address to be public. Defeats DNS
    rebinding — a name that passed the registration check but now points at an internal IP. (A narrow
    resolve-vs-connect race remains; pinning the IP would need a custom transport.)"""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # unresolvable → refuse rather than let httpx try
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not _is_public_ip(ip):
            return False
    return True
