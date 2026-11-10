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
    # The page's action is a ready prompt: every copy carries the llms.txt setup, so it works for a
    # visitor with no account; connector agents get the dashboard's doors; the nav Start free (and the
    # "no agent yet" button the script adds) are the sign-in fallbacks.
    assert "https://treg.to/install.sh | sh && treg login" in html and "https://treg.to/llms.txt" in html
    # With an ad click in the cookie the page swaps in a short install link bound to that click, so
    # a sign-in finished in an agent's own browser still attributes (tests/test_ad_link.py).
    assert "fetch('/i', { method: 'POST'" in html
    assert "'curl -fsSL https://treg.to/i/' + code + ' | sh && treg login'" in html
    assert "modal=add-custom-connector" in html and "x.ai/bot/plugin/55647425" in html
    assert "chatgpt.com/plugins/plugin_asdk_app_6ac4c2df9c548191b83ea0c156bd9b6a" in html and "Developer mode" not in html
    # The general prompt is in the HTML itself, so a visitor without JavaScript still has something to copy.
    assert "Then: </span>" in html and "<noscript>" in html
    # The brand in prose is treg.to; a bare "treg" only names the CLI, the wordmark and the prompts.
    assert "© 2026 treg.to" in html and "Add treg.to to Claude.ai" in html
    assert len(re.findall(r'<button[^>]* data-cta="', html)) == 1
    # Every card copies through its one bubble; there is no second copy button to drift from it.
    assert "data-copy-job" not in html
    assert len(re.findall(r'<article class="use" data-job="(seo|leads|ads|social)">', html)) == 4
    assert len(re.findall(r'class="bubble" data-ask="', html)) == 4
    # Paid traffic is mostly phones, which have no terminal: a phone's first pick is ChatGPT.
    assert "var first = touch ? 'chatgpt' : 'claude-code'" in html
    # The cards quote real runs; retired or mismatched comparisons must not come back: X dropped its
    # subscription tiers, and SpyFu researches Google ads, not Meta's.
    assert "$0.0036" in html and "$119.99/mo" in html and "$0.0076" in html and "18 for $0.88" in html
    # Card media is served from the page's own folder, never hotlinked from a CDN that expires it.
    assert "fbcdn.net" not in html and "pbs.twimg.com" not in html and "/media/start/" in html
    assert "$200" not in html and "SpyFu" not in html and "$471" not in html and "$238" not in html and "$358" not in html
    # One price per vendor: Semrush AI Visibility is quoted at $99.
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
