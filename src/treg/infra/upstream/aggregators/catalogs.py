"""The aggregators' catalog listings, for `treg-worker overflow sync --live`."""

from __future__ import annotations

import httpx

from .monid import BASE as MONID
from .orthogonal import BASE as ORTH


async def orthogonal_apis(client: httpx.AsyncClient, key: str) -> list[dict]:
    apis, offset = [], 0
    while True:
        r = await client.get(f"{ORTH}/list-endpoints", params={"limit": 500, "offset": offset},
                             headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        d = r.json()
        apis += d["apis"]
        if not d.get("pagination", {}).get("hasMore"):
            return apis
        offset += 500


async def orthogonal_price(client: httpx.AsyncClient, key: str, api: str, path: str) -> str | None:
    r = await client.post(f"{ORTH}/details", json={"api": api, "path": path},
                          headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        return None
    return (r.json().get("endpoint") or {}).get("price")


async def monid_discover(client: httpx.AsyncClient, key: str, query: str, limit: int = 40) -> list[dict]:
    r = await client.post(f"{MONID}/discover", json={"query": query, "limit": limit},
                          headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        return []
    return r.json().get("results") or []
