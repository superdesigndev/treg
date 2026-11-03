"""Round-2 security fixes — proxy SSRF guard (B)."""
from __future__ import annotations

from httpx import AsyncClient


# --- B: no SSRF via base_url ------------------------------------------------------------------
async def test_base_url_rejects_internal_hosts(clients: AsyncClient):
    for bad in ("http://169.254.169.254/latest/meta-data/", "http://localhost/x",
                "http://127.0.0.1:8080", "http://10.0.0.1", "http://foo.internal",
                "http://2130706433/"):  # 127.0.0.1 as one decimal number
        r = await clients.post("/tools", json={"name": "ssrf", "base_url": bad})
        assert r.status_code == 422, f"{bad} should be refused, got {r.status_code}"
    ok = await clients.post("/tools", json={"name": "okpub", "base_url": "https://api.stripe.com/v1"})
    assert ok.status_code == 200, ok.text
