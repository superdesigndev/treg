"""A release's SHA256SUMS, read from where the release is published. A published release's files
never change, so a read is kept for the life of the process; a failed one is retried after a
minute, so a burst of CLIs does not wait on it each time."""

from __future__ import annotations

import re
import time

import httpx

_RETRY_S = 60.0
_LINE = re.compile(r"^([0-9a-fA-F]{64}) [ *](\S+)$")
_sums: dict[str, dict[str, str]] = {}
_failed: dict[str, float] = {}


async def checksums(url: str) -> dict[str, str]:
    """File name to lowercase SHA-256 from the SHA256SUMS at url; empty when it cannot be read."""
    if url in _sums:
        return _sums[url]
    if url in _failed and time.monotonic() - _failed[url] < _RETRY_S:
        return {}
    sums: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            r = await client.get(url)
        if r.status_code == 200:
            sums = {m[2]: m[1].lower() for line in r.text.splitlines() if (m := _LINE.match(line.strip()))}
    except httpx.HTTPError:
        sums = {}
    if sums:
        _sums[url] = sums
    else:
        _failed[url] = time.monotonic()
    return sums
