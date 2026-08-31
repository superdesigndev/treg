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
from treg.config import get_settings


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


async def test_provider_page_anatomy_and_structured_data(clients: AsyncClient):
    service = _first_provider()
    r = await clients.get(f"/tools/{service}")
    assert r.status_code == 200
    html = r.text
    base = get_settings().public_url.rstrip("/")
    assert f'rel="canonical" href="{base}/tools/{service}"' in html
    assert 'id="tools"' in html and 'id="faq"' in html and 'href="/pricing"' in html
    types = {d["@type"] for d in _ld(html)}
    assert {"BreadcrumbList", "ItemList", "FAQPage", "HowTo"} <= types
    item_list = next(d for d in _ld(html) if d["@type"] == "ItemList")
    assert item_list["numberOfItems"] == len(item_list["itemListElement"])


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


async def test_pricing_page(clients: AsyncClient):
    r = await clients.get("/pricing")
    assert r.status_code == 200
    assert "$1.00" in r.text and "markup" in r.text
    assert {"BreadcrumbList", "FAQPage"} <= {d["@type"] for d in _ld(r.text)}


async def test_sitemap_and_catalog_link_every_provider_page(clients: AsyncClient):
    services = [r["service"] for r in web_routes._provider_rows()]
    sm = (await clients.get("/sitemap.xml")).text
    cat = (await clients.get("/catalog")).text
    for s in services:
        assert f"/tools/{s}<" in sm, s
        assert f'href="/tools/{s}"' in cat, s
    assert "/pricing<" in sm
