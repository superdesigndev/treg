"""Leadscart — BYOK enrichment API (people search, verified email find, Lists)."""
from __future__ import annotations

import dataclasses
import httpx
from httpx import AsyncClient

from treg import oauth_providers as P
from treg.api import app
from treg.domain.catalog import store as cs


def test_every_paid_leadscart_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "leadscart"]
    assert eps
    for ep in eps:
        if ep["cost"]["type"] == "free" or ep.get("scope") == "own_account":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "leadscart")["usd"] > 0, ep["id"]


async def test_leadscart_connect_rejects_a_bogus_bearer_key(clients: AsyncClient, monkeypatch):
    """Live bad-key behavior on 2026-10-18: 401 {"error":{"code":"unauthorized",...}}."""

    def probe(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/credits"
        auth = request.headers.get("authorization", "")
        assert auth.startswith("Bearer ")
        if auth.endswith("bad-key"):
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": "unauthorized",
                        "message": "Missing or invalid API key.",
                        "retryable": False,
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {"balance": 0, "lists_equivalent": 0, "expiring_within_7_days": 0, "next_expiry": None, "plan": "free"},
                "credits": {"spent": 0, "remaining": 0},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        monkeypatch.setitem(
            P.REGISTRY,
            "leadscart",
            dataclasses.replace(P.REGISTRY["leadscart"], base_url="http://upstream"),
        )
        bad = await clients.post("/connections/token", json={"provider": "leadscart", "token": "bad-key"})
        assert bad.status_code == 422, bad.text
        good = await clients.post("/connections/token", json={"provider": "leadscart", "token": "own-key"})
        assert good.status_code == 200, good.text
