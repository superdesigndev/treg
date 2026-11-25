"""Framework-neutral SSRF guards for upstream and webhook targets."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_global also accepts multicast and the NAT64 translation prefix 64:ff9b::/96,
    # including mappings to non-global IPv4 addresses. is_reserved blocks that prefix.
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def safe_webhook_url(url: str | None, *, fold_trailing_dot: bool = True) -> bool:
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
    if fold_trailing_dot:
        host = host.rstrip(".")
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


_executor: ThreadPoolExecutor | None = None
_inflight: dict[str, asyncio.Future] = {}


def _lookup_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        from ...config import get_settings
        _executor = ThreadPoolExecutor(
            max_workers=max(1, get_settings().ssrf_lookup_threads), thread_name_prefix="treg-ssrf-dns")
    return _executor


async def host_is_public_async(host: str, check=None) -> bool:
    """host_is_public off the event loop, on a small dedicated pool. At most one lookup per host is in
    flight; a caller arriving meanwhile awaits the same answer. No verdict is kept once it lands. A
    caller that has waited `ssrf_lookup_timeout_s` is refused; the lookup itself cannot be interrupted
    and finishes on its thread, answering only callers still waiting."""
    if not host:
        return False
    fut = _inflight.get(host)
    if fut is None:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_lookup_executor(), check or host_is_public, host)
        _inflight[host] = fut
        fut.add_done_callback(_forget(host))
    from ...config import get_settings
    try:
        return await asyncio.wait_for(asyncio.shield(fut), get_settings().ssrf_lookup_timeout_s)
    except TimeoutError:
        _log.warning("ssrf_lookup_timeout")
        return False


def _forget(host: str):
    def done(fut: asyncio.Future) -> None:
        if _inflight.get(host) is fut:
            _inflight.pop(host, None)
        if not fut.cancelled():
            fut.exception()  # a refused caller may have stopped waiting; mark the outcome retrieved
    return done


async def url_host_is_public(url: object) -> bool:
    """host_is_public for a whole URL (a str or an httpx.URL), resolved off the event loop."""
    try:
        host = urlsplit(str(url)).hostname or ""
    except ValueError:
        return False
    return await host_is_public_async(host)
