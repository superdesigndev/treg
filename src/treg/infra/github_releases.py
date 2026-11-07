"""The newest GitHub release of a repository whose tag starts with a prefix, from GitHub's public
releases API, kept per process for a few minutes so a burst of CLIs asks GitHub once."""

from __future__ import annotations

import time

import httpx

_TTL_S = 600.0
_cache: dict[tuple[str, str, bool], tuple[float, str]] = {}


async def newest(repo: str, prefix: str, *, prereleases: bool) -> str:
    """The newest published release's tag without `prefix`, or "" when there is none or GitHub
    cannot be read. Drafts never count; pre-releases count only when asked for."""
    key = (repo, prefix, prereleases)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _TTL_S:
        return cached[1]
    version = ""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://api.github.com/repos/{repo}/releases",
                                 params={"per_page": 30},
                                 headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            for release in r.json():
                tag = release.get("tag_name") or ""
                if release.get("draft") or not tag.startswith(prefix):
                    continue
                if release.get("prerelease") and not prereleases:
                    continue
                version = tag.removeprefix(prefix)
                break
    except (httpx.HTTPError, ValueError):
        version = ""
    _cache[key] = (time.monotonic(), version)
    return version
