"""A repository's published GitHub releases, from GitHub's public releases API, kept per process for
a few minutes so a burst of CLIs asks GitHub once."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

_TTL_S = 600.0
_cache: dict[str, tuple[float, list[Release]]] = {}


@dataclass(frozen=True)
class Release:
    tag: str
    prerelease: bool


async def published(repo: str) -> list[Release]:
    """The repository's newest releases, newest first, drafts left out; empty when GitHub cannot be
    read."""
    cached = _cache.get(repo)
    if cached and time.monotonic() - cached[0] < _TTL_S:
        return cached[1]
    releases: list[Release] = []
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.github.com/repos/{repo}/releases",
                                 params={"per_page": 30},
                                 headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            releases = [Release(str(item.get("tag_name") or ""), bool(item.get("prerelease")))
                        for item in r.json() if not item.get("draft")]
    except (httpx.HTTPError, ValueError, AttributeError):
        releases = []
    _cache[repo] = (time.monotonic(), releases)
    return releases
