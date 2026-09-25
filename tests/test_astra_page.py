"""The launch destination must work for a signed-out viewer, including its bundled assets."""

import re
from urllib.parse import urlsplit

from httpx import AsyncClient


async def test_gpt6_bundled_assets_and_seekable_film(clients: AsyncClient):
    page = (await clients.get("/gpt6")).text
    urls = set(re.findall(r'(?:src|poster|href)="((?:media|logos)/[^"#]+)"', page))
    for url in urls:
        response = await clients.get("/" + urlsplit(url).path)
        assert response.status_code == 200, url
    # The film must support a browser seeking within it.
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


