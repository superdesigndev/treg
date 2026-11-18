"""Framework-neutral SSRF guards for upstream and webhook targets."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit


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


PUBLIC, REFUSED, TIMED_OUT = "public", "refused", "timed_out"
CALL, METADATA = "call", "metadata"
# A metadata lookup is asked for without signing in, so its pool also refuses at once past this many
# lookups per thread instead of queueing them.
_METADATA_PENDING_PER_THREAD = 4

log = logging.getLogger("treg.ssrf")


@dataclass(eq=False)
class _Lookup:
    pool: str
    work: Future
    answer: asyncio.Future
    waiters: int = 0


_executors: dict[str, ThreadPoolExecutor] = {}
_inflight: dict[tuple[str, str], _Lookup] = {}


def _threads(pool: str) -> int:
    from ...config import get_settings
    s = get_settings()
    return max(1, s.ssrf_metadata_lookup_threads if pool == METADATA else s.ssrf_lookup_threads)


def _lookup_executor(pool: str = CALL) -> ThreadPoolExecutor:
    executor = _executors.get(pool)
    if executor is None:
        executor = _executors[pool] = ThreadPoolExecutor(
            max_workers=_threads(pool),
            thread_name_prefix="treg-ssrf-dns" if pool == CALL else f"treg-ssrf-dns-{pool}")
    return executor


def _forget(key: tuple[str, str], entry: _Lookup) -> None:
    if _inflight.get(key) is entry:
        del _inflight[key]


async def lookup_host(host: str, check=None, *, pool: str = CALL) -> str:
    """host_is_public off the event loop: PUBLIC, REFUSED, or TIMED_OUT when no answer came in time.

    Each pool has its own threads: CALL for the call road, METADATA for MCP client documents fetched
    before sign-in, so no flood of the second can delay the first. At most one lookup per host and
    pool is in flight; a caller arriving meanwhile awaits the same answer, and no verdict is kept once
    it lands. A caller waits at most `ssrf_lookup_timeout_s`. When the last caller waiting on a lookup
    gives up, a lookup that has not started yet is cancelled; one already running keeps its thread
    until the resolver returns, and later callers for that host join it."""
    if not host:
        return REFUSED
    from ...config import get_settings
    key = (pool, host)
    entry = _inflight.get(key)
    if entry is None:
        if pool == METADATA and sum(1 for e in _inflight.values() if e.pool == pool) >= (
                _threads(pool) * _METADATA_PENDING_PER_THREAD):
            log.warning("ssrf_lookup_busy pool=%s", pool)
            return TIMED_OUT
        work = _lookup_executor(pool).submit(check or host_is_public, host)
        entry = _Lookup(pool, work, asyncio.wrap_future(work))
        _inflight[key] = entry
        entry.answer.add_done_callback(lambda _f, k=key, e=entry: _forget(k, e))
    entry.waiters += 1
    limit = get_settings().ssrf_lookup_timeout_s
    try:
        ok = await asyncio.wait_for(asyncio.shield(entry.answer), limit if limit > 0 else None)
    except TimeoutError:
        log.warning("ssrf_lookup_timeout pool=%s limit_s=%s", pool, limit)
        return TIMED_OUT
    finally:
        entry.waiters -= 1
        if entry.waiters == 0 and not entry.answer.done() and entry.work.cancel():
            _forget(key, entry)
    return PUBLIC if ok else REFUSED


async def host_is_public_async(host: str, check=None, *, pool: str = CALL) -> bool:
    """lookup_host as a yes or no: a lookup that timed out counts as not public."""
    return await lookup_host(host, check, pool=pool) == PUBLIC


async def url_host_is_public(url: object) -> bool:
    """host_is_public for a whole URL (a str or an httpx.URL), resolved off the event loop."""
    try:
        host = urlsplit(str(url)).hostname or ""
    except ValueError:
        return False
    return await host_is_public_async(host)
