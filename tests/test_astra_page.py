"""The launch destination must work for a signed-out viewer, including its bundled assets."""

import re
from urllib.parse import urlsplit

from httpx import AsyncClient


async def test_gpt6_is_public_indexable_and_answers_head(clients: AsyncClient):
    clients.cookies.clear()
    response = await clients.get("/gpt6?utm_source=launch-video")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert '<link rel="canonical" href="https://treg.to/gpt6">' in response.text
    assert 'href="https://chatgpt.com/plugins/plugins_6a7fb961a34881918798681dade464ec?q=treg"' in response.text
    assert 'class="start-section"' not in response.text
    assert 'type="email"' in response.text
    assert 'data-page="gpt6"' in response.text
    assert 'href="/auth/google"' in response.text
    assert 'href="/auth/github"' in response.text
    assert 'role="tablist"' in response.text
    assert 'src="/sitetrack.js"' in response.text
    assert 'src="/adtrack.js"' in response.text
    head = await clients.head("/gpt6")
    assert head.status_code == 200
    assert not head.content
    assert "/gpt6</loc>" in (await clients.get("/sitemap.xml")).text


async def test_gpt6_bundled_assets_and_seekable_film(clients: AsyncClient):
    page = (await clients.get("/gpt6")).text
    urls = set(re.findall(r'(?:src|poster|href)="((?:media|logos)/[^"#]+)"', page))
    for url in urls:
        response = await clients.get("/" + urlsplit(url).path)
        assert response.status_code == 200, url
    # The film is loaded on demand and must support a browser seeking within it.
    assert '<video id="launch-film" controls playsinline preload="none"' in page
    assert 'src="media/astra/launch.mp4"' not in page
    video = await clients.get("/media/astra/launch.mp4", headers={"Range": "bytes=0-127"})
    assert video.status_code == 206
    assert len(video.content) == 128
    assert video.headers["content-type"] == "video/mp4"


async def test_astra_redirect_preserves_campaign_query(clients: AsyncClient):
    for method in (clients.get, clients.head):
        response = await method("/astra?utm_source=launch-video&ref=campaign", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/gpt6?utm_source=launch-video&ref=campaign"
    sitemap = (await clients.get("/sitemap.xml")).text
    assert "/astra</loc>" not in sitemap


async def test_gpt6_enrichment_follows_benchmark(clients: AsyncClient):
    page = (await clients.get("/gpt6")).text
    assert page.index('id="proof"') < page.index('id="usecases"') < page.index('id="connectors"')
    assert 'Every enrichment job.' in page
    assert len(re.findall(r'data-uc="', page)) == 8
    assert 'class="uc uc-more" href="/catalog"' in page
