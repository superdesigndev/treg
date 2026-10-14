"""The public provider pages (/tools/<service>), /pricing, and the signed-out marketplace redirect.

These routes render everything from the catalog, so the tests read their expectations off the
same census (`_provider_rows`) rather than naming a vendor that could be retired. The route-shape
tests exist because `/tools` is also the authed team-tools API: a public page must never shadow
it (it once did, and 59 tests went red), and a signed-out visit must land on a page that answers.
"""

from __future__ import annotations

import json
import re

from httpx import AsyncClient

from treg.routers import web as web_routes


def _ld(html: str) -> list[dict]:
    return [json.loads(m) for m in
            re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def _first_provider() -> str:
    rows = web_routes._provider_rows()
    assert rows, "catalog has no providers"
    return rows[0]["service"]


async def test_every_provider_page_renders(clients: AsyncClient):
    for row in web_routes._provider_rows():
        r = await clients.get(f"/tools/{row['service']}")
        assert r.status_code == 200, (row["service"], r.status_code)
        assert f'href="/tools/{row["service"]}"' in r.text or "canonical" in r.text


async def test_unknown_provider_is_404_and_api_paths_are_not_shadowed(clients: AsyncClient):
    assert (await clients.get("/tools/nope")).status_code == 404
    # The API's own GETs on this prefix still answer as the API (JSON), never as a page.
    r = await clients.get("/tools")
    assert r.headers["content-type"].startswith("application/json"), r.headers["content-type"]
    r = await clients.get("/tools/by-name/anything")
    assert r.headers["content-type"].startswith("application/json"), r.headers["content-type"]


async def test_signed_out_marketplace_redirects_to_public_page(clients: AsyncClient):
    service = _first_provider()
    r = await clients.get(f"/app/marketplace/{service}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/tools/{service}"
    # An unknown service 404s here rather than redirecting into a 404.
    r = await clients.get("/app/marketplace/nope", follow_redirects=False)
    assert r.status_code == 404


async def test_provider_page_reads_the_observation_reader_not_the_session(clients, caplog):
    """`/tools/{service}` once handed `_observed_or_empty` the request's AsyncSession instead of the
    app's observation reader; the measured line silently came up empty and every page view logged
    an AttributeError traceback (prod, 2026-09-06)."""
    import logging
    with caplog.at_level(logging.WARNING, logger="treg.catalog"):
        r = await clients.get("/tools/dataforseo")
    assert r.status_code == 200
    assert "endpoint stats unavailable" not in caplog.text


