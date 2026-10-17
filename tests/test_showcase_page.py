"""The showcase: apps built with treg, their makers and their launch posts. Crawlers and link
previews must read the apps without running the page's script, and it describes treg.to only
where treg.to serves it."""

from __future__ import annotations

import json
import re

from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.config import get_settings

PATH = "/showcase"


async def test_apps_are_in_the_served_html_and_the_structured_data(clients: AsyncClient):
    r = await clients.get(PATH)
    assert r.status_code == 200
    html = r.text
    assert "{BASE}" not in html
    tiles = re.findall(r'<article class="app" id="app-([a-z0-9-]+)"', html)
    assert tiles, "the gallery must be written in the HTML, not drawn by the script"
    posts = re.findall(r'<article class="post" id="post-([a-z0-9-]+)"', html)
    assert posts == tiles  # every app has its launch post, in the same order
    (block,) = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    listed = json.loads(block)["mainEntity"]["itemListElement"]
    assert len(listed) == len(tiles)
    for tile, item in zip(tiles, listed):
        assert f'id="app-{tile}"' in html and item["item"]["name"] in html
    for src in sorted(set(re.findall(r'src="(media/showcase/[^"]+)"', html))):
        assert (await clients.get("/" + src)).status_code == 200, src
    wall = (await clients.get("/media/showcase/wall.json")).json()
    for post in wall:
        assert (await clients.get("/media/showcase/" + post["photo"])).status_code == 200, post["handle"]


async def test_showcase_is_linked_and_listed(clients: AsyncClient):
    for page in ("/", "/blog", "/use-cases", "/people-search", "/leads-signals", "/grokbot", "/fable",
                 "/ugc", "/aeo", "/jev", "/gtm-engineering"):
        r = await clients.get(page)
        assert f'href="{PATH}"' in r.text, page
        # the bundled pages unwrap their hosted markers when they serve (the home page keeps its own)
        if page != "/":
            assert "hosted-->" not in r.text, page
    assert f"{PATH}<" in (await clients.get("/sitemap.xml")).text


async def test_showcase_404s_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get(PATH)).status_code == 404
            assert f"{PATH}<" not in (await c.get("/sitemap.xml")).text
            for page in ("/", "/people-search", "/grokbot", "/fable", "/ugc", "/aeo", "/jev"):
                r = await c.get(page)
                assert r.status_code == 200, page
                assert f'href="{PATH}"' not in r.text, page
    finally:
        get_settings.cache_clear()
