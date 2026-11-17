"""An httpx transport that connects only to public addresses, checked at connect time.

A separate "is this host public?" lookup followed by httpx's own lookup lets a name answer a public
address to the check and an internal one to the connection (DNS rebinding). Here the address the
socket opens is the address that was checked: the name is resolved once, every answer must be
public, and the connection goes to one of those answers. TLS still verifies the certificate and
sends SNI for the URL's host, and the request keeps its Host header, so the upstream sees the same
request.

`TREG_SSRF_PIN` rolls it out: `off` builds a plain client, `log` connects the way httpx always did
and logs each connection pinning would refuse, `enforce` pins. Hosts passed as `bypass_hosts` (the
catalog's fixed provider hosts) always connect the way httpx always did, and behind an environment
egress proxy every mode builds the plain client.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import urllib.request
from typing import Iterable

import httpcore
import httpx

from .ssrf import _is_public_ip, _lookup_executor

log = logging.getLogger("treg")

# The stagger between connection attempts, anyio's own Happy Eyeballs delay (RFC 8305).
_STAGGER_S = 0.25
_background: set[asyncio.Task] = set()


class NonPublicAddress(httpcore.ConnectError):
    """The upstream host resolves to an address treg does not connect to."""


def _public_addresses(host: str, port: int, getaddrinfo=socket.getaddrinfo) -> list[str]:
    """Every address `host` resolves to, all public, in the resolver's order with the address
    families interleaved as RFC 8305 asks (anyio does the same), so a run of unreachable IPv6
    answers cannot use up the timeout before an IPv4 one is tried."""
    try:
        infos = getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NonPublicAddress(f"upstream host {host!r} does not resolve") from exc
    by_family: dict[int, list[str]] = {}
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            raise NonPublicAddress("upstream host resolves to a non-public address") from exc
        if not _is_public_ip(ip):
            raise NonPublicAddress("upstream host resolves to a non-public address")
        family = by_family.setdefault(ip.version, [])
        if str(ip) not in family:
            family.append(str(ip))
    if not by_family:
        raise NonPublicAddress(f"upstream host {host!r} does not resolve")
    queues = list(by_family.values())
    addresses: list[str] = []
    while any(queues):
        for queue in queues:
            if queue:
                addresses.append(queue.pop(0))
    return addresses


async def _close_quietly(stream: httpcore.AsyncNetworkStream) -> None:
    try:
        await stream.aclose()
    except Exception:  # noqa: BLE001 - a losing connection that fails to close is still discarded
        pass


async def _discard(tasks: set[asyncio.Task], streams: list[httpcore.AsyncNetworkStream]) -> None:
    """Cancel the attempts still running and close every connection that is not being returned."""
    for task in tasks:
        task.cancel()
    for outcome in await asyncio.gather(*tasks, return_exceptions=True):
        if not isinstance(outcome, BaseException):
            streams.append(outcome)
    for stream in streams:
        await _close_quietly(stream)


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None, getaddrinfo=socket.getaddrinfo,
                 *, bypass_hosts: frozenset[str] = frozenset(), enforce: bool = True) -> None:
        self._inner = inner or httpcore.AnyIOBackend()
        self._getaddrinfo = getaddrinfo
        self._bypass = frozenset(h.lower() for h in bypass_hosts)
        self._enforce = enforce

    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.lower() in self._bypass:
            return await self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        if not self._enforce:
            task = asyncio.ensure_future(self._observe(host, port))
            _background.add(task)
            task.add_done_callback(_background.discard)
            return await self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        lookup = loop.run_in_executor(_lookup_executor(), _public_addresses, host, port, self._getaddrinfo)
        try:
            addresses = await asyncio.wait_for(lookup, timeout)
        except asyncio.TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"resolving {host!r} timed out") from exc
        return await self._race(host, addresses, port, deadline, local_address, socket_options)

    async def _observe(self, host: str, port: int) -> None:
        """`log` mode: the lookup pinning would make, off the request's path, logged when it refuses."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(_lookup_executor(), _public_addresses, host, port, self._getaddrinfo)
        except NonPublicAddress as exc:
            log.warning("ssrf_pin would refuse host=%s port=%s: %s", host, port, exc)
        except Exception:  # noqa: BLE001 - observing must never affect the call
            pass

    async def _race(
        self, host: str, addresses: list[str], port: int, deadline: float | None,
        local_address: str | None, socket_options: Iterable[httpcore.SOCKET_OPTION] | None,
    ) -> httpcore.AsyncNetworkStream:
        """Happy Eyeballs over the approved addresses, as httpcore's own backend does over a name
        (RFC 8305): the next address starts after a short stagger or as soon as one fails, the first
        to connect wins, and all attempts share the one connect timeout. Every connection but the
        one returned is closed, including when the caller is cancelled during cleanup."""
        loop = asyncio.get_running_loop()

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - loop.time())

        queue = iter(addresses)
        pending: set[asyncio.Task] = set()
        extras: list[httpcore.AsyncNetworkStream] = []
        failure: BaseException | None = None
        winner: httpcore.AsyncNetworkStream | None = None
        try:
            while True:
                address = next(queue, None)
                if address is not None:
                    pending.add(asyncio.ensure_future(self._inner.connect_tcp(
                        address, port, timeout=remaining(), local_address=local_address,
                        socket_options=socket_options)))
                if not pending:
                    break
                left = remaining()
                if left == 0.0:
                    raise httpcore.ConnectTimeout(f"connecting to {host!r} timed out")
                wait = _STAGGER_S if address is not None else None
                if left is not None:
                    wait = left if wait is None else min(wait, left)
                done, pending = await asyncio.wait(pending, timeout=wait,
                                                   return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is not None:
                        failure = task.exception()
                    elif winner is None:
                        winner = task.result()
                    else:
                        extras.append(task.result())
                if winner is not None:
                    break
        except BaseException:
            if winner is not None:
                extras.append(winner)
                winner = None
            raise
        finally:
            cleanup = asyncio.ensure_future(_discard(pending, extras))
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                if winner is not None:
                    await asyncio.shield(_close_quietly(winner))
                raise
        if winner is not None:
            return winner
        if isinstance(failure, (httpcore.ConnectError, httpcore.ConnectTimeout)):
            raise failure
        raise httpcore.ConnectError(f"could not connect to {host!r}") from failure

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise NonPublicAddress("a unix socket is not an upstream")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def public_only_transport(*, limits: httpx.Limits | None = None,
                          backend: httpcore.AsyncNetworkBackend | None = None,
                          getaddrinfo=socket.getaddrinfo, bypass_hosts: frozenset[str] = frozenset(),
                          enforce: bool = True) -> httpx.AsyncHTTPTransport:
    """httpx's own transport with its connection pool rebuilt on `PublicOnlyBackend`, with the TLS
    context httpx's default builds (it honours SSL_CERT_FILE / SSL_CERT_DIR)."""
    limits = limits or httpx.Limits()
    transport = httpx.AsyncHTTPTransport(limits=limits)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=True),
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
        network_backend=PublicOnlyBackend(backend, getaddrinfo, bypass_hosts=bypass_hosts,
                                          enforce=enforce),
    )
    return transport


def is_refusal(exc: BaseException) -> bool:
    """Whether an httpx error is this transport refusing a non-public address."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, NonPublicAddress):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def environment_proxies() -> dict[str, str]:
    """The egress proxies httpx would take from the environment (it reads them the same way)."""
    return {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https", "all") and v}


def upstream_client(*, bypass_hosts: frozenset[str] = frozenset(), **kwargs) -> httpx.AsyncClient:
    """The client for any request to an upstream a caller or provider chose. With the SSRF guard on
    (`proxy_ssrf_check`) and `ssrf_pin` at `log` or `enforce`, it connects through
    `public_only_transport`; otherwise it is the plain client treg always used.

    Behind an environment egress proxy it is always the plain client: a proxy resolves the name
    itself, so there is no address to pin, and passing a transport would make httpx skip the proxy
    and change how every upstream, the catalog's included, is reached."""
    from ...config import get_settings

    s = get_settings()
    if s.proxy_ssrf_check and s.ssrf_pin in ("log", "enforce"):
        if kwargs.get("trust_env", True) and environment_proxies():
            log.warning("ssrf_pin=%s has no effect: an environment egress proxy is set", s.ssrf_pin)
        else:
            kwargs["transport"] = public_only_transport(
                limits=kwargs.pop("limits", None), bypass_hosts=bypass_hosts,
                enforce=s.ssrf_pin == "enforce")
    return httpx.AsyncClient(**kwargs)
