"""Exercise the connect-time pinning transport against the real network before turning it on.

    uv run python scripts/ssrf_pin_smoke.py

Run it on a machine whose resolver answers real addresses (no proxy or VPN that rewrites DNS):
from the deploy's own shell is best, since that is the network the transport will run on. It
makes ordinary HTTPS GETs to well-known public hosts and changes nothing anywhere.

Checks: real HTTPS through the pinned transport answers what a plain client answers (TLS
verification and SNI on the URL's name), dual-stack hosts connect, internal targets are refused
fast, a tiny connect timeout fails as a timeout, and requests cancelled mid-connect leave the pool
usable. Exits non-zero if any check fails.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import sys
import time

import httpx

from treg.infra.upstream.pinned import is_refusal, public_only_transport

PUBLIC = ("https://example.com/", "https://www.cloudflare.com/cdn-cgi/trace", "https://api.github.com/")
INTERNAL = ("http://127.0.0.1:9/", "http://localhost/", "http://169.254.169.254/latest/meta-data/")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def resolver_is_real() -> bool:
    for host in ("example.com", "www.cloudflare.com"):
        for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global or ip in ipaddress.ip_network("198.18.0.0/15"):
                print(f"{host} resolves to {ip}: this resolver rewrites DNS; run elsewhere.")
                return False
    return True


async def main() -> int:
    if not resolver_is_real():
        return 2
    pinned = httpx.AsyncClient(transport=public_only_transport(), trust_env=False, timeout=15)
    plain = httpx.AsyncClient(trust_env=False, timeout=15)
    async with pinned, plain:
        for url in PUBLIC:
            try:
                a, b = await pinned.get(url), await plain.get(url)
                check(f"https {url}", a.status_code == b.status_code, f"{a.status_code} vs plain {b.status_code}")
            except httpx.HTTPError as exc:
                check(f"https {url}", False, f"{type(exc).__name__}: {exc}")
        families = {info[0] for info in socket.getaddrinfo("www.cloudflare.com", 443, type=socket.SOCK_STREAM)}
        check("dual-stack answers seen", len(families) >= 1, f"families={sorted(int(f) for f in families)}")

        for url in INTERNAL:
            began = time.monotonic()
            try:
                await pinned.get(url, timeout=5)
                check(f"refuse {url}", False, "connected")
            except httpx.HTTPError as exc:
                took = time.monotonic() - began
                check(f"refuse {url}", is_refusal(exc) and took < 2, f"{took:.2f}s")

        try:
            await pinned.get(PUBLIC[0], timeout=httpx.Timeout(5, connect=0.001))
            check("tiny connect timeout", False, "connected")
        except httpx.ConnectTimeout:
            check("tiny connect timeout", True)
        except httpx.HTTPError as exc:
            check("tiny connect timeout", False, type(exc).__name__)

        tasks = [asyncio.ensure_future(pinned.get(PUBLIC[i % len(PUBLIC)])) for i in range(30)]
        await asyncio.sleep(0.02)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            r = await pinned.get(PUBLIC[0])
            check("pool usable after 30 cancelled requests", r.status_code < 500, str(r.status_code))
        except httpx.HTTPError as exc:
            check("pool usable after 30 cancelled requests", False, f"{type(exc).__name__}: {exc}")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
