"""The comparison pages: every claim about another product is dated and sourced from that product's
own site, the structured data says what the page says, and the pages exist only where treg.to
serves them, because they quote treg.to's own pricing and grant."""

from __future__ import annotations

import html as _html
import json
import re
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from treg import compare_pages as cp
from treg.api import app
from treg.config import get_settings

PAGES = [f"/compare/{cp.page_slug(k)}" for k in cp.COMPETITORS]


def _ld(html: str) -> list[dict]:
    return [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def _text(html: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", html))


def test_every_page_fills_every_row_and_card():
    keys = {row for _, items in cp.ROWS for row, _ in items}
    assert keys == set(cp.TREG_CELLS)
    groups = {g for g, _, _ in cp.GROUPS}
    for key, c in cp.COMPETITORS.items():
        assert set(c["cells"]) == keys, key
        assert set(c["spec"]) == {k for k, _ in cp.SPEC_LABELS}, key
        assert c["group"] in groups, key
        assert set(c.get("treg_cells", {})) <= keys, key
        assert c["faq"] and c["better"] and c["different"] and c["fit_them"] and c["fit_treg"], key


def test_every_competitor_fact_has_a_source_on_its_own_site():
    for key, c in cp.COMPETITORS.items():
        own = urlsplit(c["site"]).hostname.removeprefix("www.").split(".")[-2]
        assert any(own in (urlsplit(u).hostname or "") for _, u in c["sources"]), key
        for _, u in c["sources"]:
            assert u.startswith("https://"), (key, u)


async def test_each_page_answers_with_its_own_head(clients: AsyncClient):
    seen_titles = set()
    for key, c in cp.COMPETITORS.items():
        path = f"/compare/{cp.page_slug(key)}"
        r = await clients.get(path)
        assert r.status_code == 200, path
        html = r.text
        for placeholder in ("{BASE}", "{ENDPOINTS}", "{PROVIDERS}"):
            assert placeholder not in html, (path, placeholder)
        title = re.search(r"<title>(.*?)</title>", html).group(1)
        assert _html.unescape(title) == c["title"] and len(c["title"]) <= 65, path
        assert c["title"] not in seen_titles
        seen_titles.add(c["title"])
        desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
        assert 70 <= len(_html.unescape(desc)) <= 160, (path, len(desc))
        base = get_settings().public_url.rstrip("/")
        assert f'<link rel="canonical" href="{base}{path}"/>' in html
        assert f'property="og:url" content="{base}{path}"' in html
        assert html.count("<h1>") == 1 and f"vs</span> {_html.escape(cp.short(key))}</h1>" in html
        assert cp.CHECKED_LABEL in html
        assert "/adtrack.js" in html and "/sitetrack.js" in html


async def test_structured_data_matches_the_visible_page(clients: AsyncClient):
    for key, c in cp.COMPETITORS.items():
        r = await clients.get(f"/compare/{cp.page_slug(key)}")
        blocks = {b["@type"]: b for b in _ld(r.text)}
        assert {"WebPage", "BreadcrumbList", "FAQPage"} <= set(blocks), key
        text = " ".join(_text(r.text).split())
        faq = blocks["FAQPage"]["mainEntity"]
        assert [(q["name"], q["acceptedAnswer"]["text"]) for q in faq] == c["faq"]
        for q, a in c["faq"]:
            assert q in text and a in text, (key, q)
        assert blocks["WebPage"]["dateModified"] == cp.CHECKED


async def test_the_hub_links_every_comparison(clients: AsyncClient):
    r = await clients.get("/compare")
    assert r.status_code == 200
    for path in PAGES:
        assert f'href="{path}"' in r.text, path
    (collection,) = [b for b in _ld(r.text) if b["@type"] == "CollectionPage"]
    assert len(collection["mainEntity"]["itemListElement"]) == len(PAGES)
    assert (await clients.get("/media/compare/compare.css")).status_code == 200


async def test_each_page_has_a_markdown_twin_kept_out_of_the_index(clients: AsyncClient):
    for key, c in cp.COMPETITORS.items():
        r = await clients.get(f"/compare/{cp.page_slug(key)}.md")
        assert r.status_code == 200 and r.headers["x-robots-tag"] == "noindex"
        assert r.text.startswith(f"# treg vs {cp.short(key)}") and "{ENDPOINTS}" not in r.text
        assert c["tldr"] in r.text


@pytest.mark.parametrize("path", ["/compare/clay", "/compare/treg-vs-nobody", "/compare/treg-vs-clay.txt"])
async def test_unknown_comparisons_404(clients: AsyncClient, path: str):
    assert (await clients.get(path)).status_code == 404


async def test_comparisons_are_listed_and_linked(clients: AsyncClient):
    sitemap = (await clients.get("/sitemap.xml")).text
    for path in ["/compare", *PAGES]:
        assert f"{path}<" in sitemap, path
    assert 'href="/compare"' in (await clients.get("/pricing")).text


async def test_comparisons_404_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            for path in ["/compare", PAGES[0], PAGES[0] + ".md"]:
                assert (await c.get(path)).status_code == 404, path
            assert "/compare" not in (await c.get("/sitemap.xml")).text
            assert 'href="/compare"' not in (await c.get("/pricing")).text
    finally:
        get_settings.cache_clear()
