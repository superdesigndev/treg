"""/start: the paid-traffic landing page. It is an ad destination, so it must stay out of search,
exist only where its claims (the $1 credit, vendor plans) are true, and be measurable."""

from __future__ import annotations

import re

from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.config import get_settings

PATH = "/start"


async def test_start_is_served_noindex_and_measurable(clients: AsyncClient):
    r = await clients.get(PATH)
    assert r.status_code == 200
    assert r.headers.get("x-robots-tag") == "noindex"
    html = r.text
    assert '<meta name="robots" content="noindex">' in html
    assert "{BASE}" not in html and "{ENDPOINTS}" not in html
    assert 'data-page="start"' in html
    for script in ("/sitetrack.js", "/adtrack.js"):
        assert f'<script src="{script}"></script>' in html
    # An ad destination is not a search page: no canonical, not in the sitemap.
    assert 'rel="canonical"' not in html
    assert f"{PATH}<" not in (await clients.get("/sitemap.xml")).text


async def test_start_copy_keeps_the_public_promises(clients: AsyncClient):
    """The wording rules for treg.to copy, checked where a regression would ship them to ad traffic."""
    html = (await clients.get(PATH)).text
    text = re.sub(r"<[^>]+>", " ", html.split("<script>")[0])
    lowered = text.lower()
    assert "misses are free" not in lowered and "nothing for a miss" not in lowered
    assert "monid" not in html.lower()
    # The page only sells: one action, and the per-call prices it quotes come from real runs.
    assert len(re.findall(r'<button[^>]* data-cta="', html)) == 3
    assert "Sales Navigator $119.99/mo" in html and "$0.019" in html and "$317" in html
    # Retired or mismatched comparisons must not come back: X dropped its subscription tiers, and
    # SpyFu researches Google ads, not Meta's.
    assert "$200" not in html and "SpyFu" not in html and "$471" not in html and "$238" not in html and "$358" not in html
    # One price per vendor: the bill quotes Apollo Basic at $49 and Semrush AI Visibility at $99.
    assert "$59" not in html and "$139" not in html
    # The custom page-view event is sent from the page script, so the tracker must load before it.
    assert html.index('<script src="/sitetrack.js">') < html.index("track('start_viewed'")


async def test_start_404s_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get(PATH)).status_code == 404
    finally:
        get_settings.cache_clear()
