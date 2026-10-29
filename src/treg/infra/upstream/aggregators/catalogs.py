"""The aggregators' catalog listings, for `treg-worker overflow sync --live`."""

from __future__ import annotations

import httpx

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


