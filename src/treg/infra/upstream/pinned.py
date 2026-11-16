"""An httpx transport that connects only to public addresses, checked at connect time.

A separate "is this host public?" lookup followed by httpx's own lookup lets a name answer a public
address to the check and an internal one to the connection (DNS rebinding). Here the address the
socket opens is the address that was checked: the name is resolved once, every answer must be
public, and the connection goes to one of those answers. TLS still verifies the certificate and
sends SNI for the URL's host, and the request keeps its Host header, so the upstream sees the same
request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Iterable

import httpcore
import httpx

from .ssrf import _is_public_ip, _lookup_executor


# The stagger between connection attempts, anyio's own Happy Eyeballs delay (RFC 8305).
_STAGGER_S = 0.25


class NonPublicAddress(httpcore.ConnectError):
    """The upstream host resolves to an address treg does not connect to."""


def _public_addresses(host: str, port: int, getaddrinfo=socket.getaddrinfo) -> list[str]:
    try:
        infos = getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NonPublicAddress(f"upstream host {host!r} does not resolve") from exc
    addresses: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            raise NonPublicAddress("upstream host resolves to a non-public address") from exc
        if not _is_public_ip(ip):
            raise NonPublicAddress("upstream host resolves to a non-public address")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise NonPublicAddress(f"upstream host {host!r} does not resolve")
    return addresses


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None, getaddrinfo=socket.getaddrinfo) -> None:
        self._inner = inner or httpcore.AnyIOBackend()
        self._getaddrinfo = getaddrinfo

    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        lookup = loop.run_in_executor(_lookup_executor(), _public_addresses, host, port, self._getaddrinfo)
        try:
            addresses = await asyncio.wait_for(lookup, timeout)
        except asyncio.TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"resolving {host!r} timed out") from exc
        return await self._race(host, addresses, port, deadline, local_address, socket_options)

    async def _race(
        self, host: str, addresses: list[str], port: int, deadline: float | None,
        local_address: str | None, socket_options: Iterable[httpcore.SOCKET_OPTION] | None,
    ) -> httpcore.AsyncNetworkStream:
        """Happy Eyeballs over the approved addresses, as httpcore's own backend does over a name
        (RFC 8305): the next address starts after a short stagger or as soon as one fails, the first
        to connect wins, and all attempts share the one connect timeout."""
        loop = asyncio.get_running_loop()

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - loop.time())

        queue = iter(addresses)
        pending: set[asyncio.Task] = set()
        failure: BaseException | None = None
        winner: httpcore.AsyncNetworkStream | None = None
        try:
            start_next = True
            while True:
                if start_next and (address := next(queue, None)) is not None:
                    pending.add(asyncio.ensure_future(self._inner.connect_tcp(
                        address, port, timeout=remaining(), local_address=local_address,
                        socket_options=socket_options)))
                    more = True
                else:
                    more = False
                if not pending:
                    break
                left = remaining()
                if left == 0.0:
                    raise httpcore.ConnectTimeout(f"connecting to {host!r} timed out")
                wait = _STAGGER_S if more else None
                if left is not None:
                    wait = left if wait is None else min(wait, left)
                done, pending = await asyncio.wait(pending, timeout=wait,
                                                   return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None and winner is None:
                        winner = task.result()
                    elif task.exception() is None:
                        await task.result().aclose()
                    else:
                        failure = task.exception()
                if winner is not None:
                    return winner
                start_next = True
        finally:
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    stream = await task
                except BaseException:  # noqa: BLE001 - a losing attempt's outcome is discarded
                    continue
                if stream is not winner:
                    await stream.aclose()
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
                          getaddrinfo=socket.getaddrinfo) -> httpx.AsyncHTTPTransport:
    """httpx's own transport with its connection pool rebuilt on `PublicOnlyBackend`. Environment
    proxies are ignored on purpose: a proxy resolves the name itself, out of reach of the check."""
    limits = limits or httpx.Limits()
    transport = httpx.AsyncHTTPTransport(limits=limits, trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
        network_backend=PublicOnlyBackend(backend, getaddrinfo),
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


def upstream_client(**kwargs) -> httpx.AsyncClient:
    """The client for any request to an upstream a caller or provider chose. With the SSRF guard on
    (`proxy_ssrf_check`) it connects through `public_only_transport`; off, it is a plain client."""
    from ...config import get_settings

    if get_settings().proxy_ssrf_check:
        kwargs["transport"] = public_only_transport(limits=kwargs.pop("limits", None))
        kwargs["trust_env"] = False
    return httpx.AsyncClient(**kwargs)
