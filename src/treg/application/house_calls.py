"""treg as a client of itself: catalog calls on one of treg's own teams, through the public `/call/`.

Some features make catalog calls nobody asked for yet: the /jev demo judges launch posts, the
onboarding looks a new user up before they have done anything. Those calls go out exactly as a
visitor's agent would make them, with an ordinary member token of a house team, so every one is a
real metered call on that team (a real hold, a real bill) and none is ever on the visitor's credit.
Nothing here touches money directly.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import get_settings


@dataclass(frozen=True)
class HouseAnswer:
    status: int                # 0 = the request never got an answer
    body: dict
    cost_micro: int
    error: str | None = None
    served_by: str = ""


class HouseCalls:
    """One house token, one client label; sums what its calls cost."""

    def __init__(self, http: httpx.AsyncClient, token: str, client: str, base: str = ""):
        self.http, self.base = http, (base or get_settings().public_url).rstrip("/")
        self.headers = {"X-Treg-Token": token, "X-Treg-Client": client}
        self.cost_micro = 0
        self.calls: dict[str, int] = {}

    async def request(self, method: str, endpoint: str, kind: str, *, json: dict | None = None,
                      params: dict | None = None, headers: dict | None = None,
                      timeout: float = 60) -> HouseAnswer:
        self.calls[kind] = self.calls.get(kind, 0) + 1
        try:
            r = await self.http.request(method, f"{self.base}/call/{endpoint}", json=json, params=params,
                                        headers={**self.headers, **(headers or {})}, timeout=timeout)
        except httpx.HTTPError as exc:
            return HouseAnswer(status=0, body={}, cost_micro=0, error=type(exc).__name__)
        cost = int(r.headers.get("X-Treg-Cost-Micro") or 0)
        self.cost_micro += cost
        try:
            d = r.json()
        except ValueError:
            d = {}
        body = d if isinstance(d, dict) else {"output": d}
        return HouseAnswer(status=r.status_code, body=body, cost_micro=cost,
                           error=None if r.status_code < 400 else f"http_{r.status_code}",
                           served_by=r.headers.get("X-Treg-Served-By") or "")
