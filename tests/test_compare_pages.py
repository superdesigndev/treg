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
from treg.routers.web import _fill_headline

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
        assert keys - cp.OPTIONAL_ROWS <= set(c["cells"]) <= keys, key
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
        assert '<meta name="robots" content="index, follow' in html
        assert html.count("<h1>") == 1 and f"vs</span> {_html.escape(cp.short(key))}</h1>" in html
        assert cp.checked_label(key) in html
        assert "/adtrack.js" in html and "/sitetrack.js" in html


async def test_the_x402_row_shows_only_where_a_record_fills_it(clients: AsyncClient):
    for key, c in cp.COMPETITORS.items():
        html = (await clients.get(f"/compare/{cp.page_slug(key)}")).text
        shown = "x402 or stablecoin pay per call</th>" in html
        assert shown == ("x402" in c["cells"] or "x402" not in cp.OPTIONAL_ROWS), key
        if shown and not cp.TREG_X402:
            assert cp.TREG_CELLS["x402"].startswith("Not yet"), key


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
        assert blocks["WebPage"]["dateModified"] == cp.modified(key) >= cp.checked(key)


async def test_the_hub_links_every_comparison(clients: AsyncClient):
    r = await clients.get("/compare")
    assert r.status_code == 200
    for path in PAGES:
        assert f'href="{path}"' in r.text, path
    (collection,) = [b for b in _ld(r.text) if b["@type"] == "CollectionPage"]
    assert len(collection["mainEntity"]["itemListElement"]) == len(PAGES)
    assert (await clients.get("/media/compare/compare.css")).status_code == 200


async def test_every_comparison_shares_its_own_image(clients: AsyncClient):
    """`scripts/og_images.py --group compare` made a card for each page; a new page needs one."""
    for path in ["/compare", *PAGES]:
        html = (await clients.get(path)).text
        (img,) = re.findall(r'<meta property="og:image" content="([^"]+)"', html)
        rel = urlsplit(img).path
        assert rel == f"/media/og{path}.jpg", (path, img)
        assert (await clients.get(rel)).status_code == 200, path


async def test_footers_name_the_same_comparisons(clients: AsyncClient):
    """The Compare column: the shared footer builds it from cp.FOOTER; the hand-written footers list the same."""
    want = [f"/compare/{cp.page_slug(k)}" for k in cp.FOOTER] + ["/compare"]
    for path in ["/", "/rankings", "/showcase", "/pricing", "/grokbot", "/people-search", "/gtm-engineering", "/aeo"]:
        html = (await clients.get(path)).text
        col = html.split('<div class="lab">Compare</div>', 1)[1].split("</div>", 1)[0]
        assert re.findall(r'href="([^"]+)"', col) == want, path
        for k in cp.FOOTER:
            assert f">treg vs {cp.short(k)}</a>" in col, (path, k)


async def test_no_comparison_passes_ranking_credit_to_another_product(clients: AsyncClient):
    """Every link that leaves treg is nofollow, except treg's own code on GitHub."""
    own = {"treg.to", "www.treg.to", "localhost", "test"}
    for path in ["/compare", *PAGES]:
        html = (await clients.get(path)).text
        for tag in re.findall(r"<a\s[^>]*>", html):
            href = re.search(r'href="([^"]*)"', tag)
            host = urlsplit(href.group(1)).hostname if href else None
            if not host or host in own or href.group(1).startswith("https://github.com/superdesigndev/treg"):
                continue
            rel = re.search(r'rel="([^"]*)"', tag)
            assert rel and "nofollow" in rel.group(1).split(), (path, tag)


async def test_each_page_has_a_markdown_twin_kept_out_of_the_index(clients: AsyncClient):
    for key, c in cp.COMPETITORS.items():
        r = await clients.get(f"/compare/{cp.page_slug(key)}.md")
        assert r.status_code == 200 and r.headers["x-robots-tag"] == "noindex"
        assert r.text.startswith(f"# treg vs {cp.short(key)}") and "{ENDPOINTS}" not in r.text
        assert _fill_headline(c["tldr"]) in r.text


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


async def test_every_competitor_shows_its_own_logo(clients: AsyncClient):
    from pathlib import Path
    web = Path(cp.__file__).parent / "web"
    for key, c in cp.COMPETITORS.items():
        logo = c["logo"]
        assert logo.startswith(("/media/compare/logos/", "/logos/")), key
        f = web / logo.lstrip("/")
        assert f.exists(), (key, logo)
        if f.suffix == ".svg":  # served from our origin, so it must stay a picture, not a document
            body = f.read_text(encoding="utf-8").lower()
            assert not re.search(r"<script|\son\w+=|javascript:|foreignobject|href=\"https?:", body), key
        assert (await clients.get(logo)).status_code == 200, key
        page = (await clients.get(f"/compare/{cp.page_slug(key)}")).text
        assert f'src="{logo}"' in page, key
