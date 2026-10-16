"""The hand-built pages under src/treg/web: what they ship, how they are cached, and the copy rule
that the catalog's size is read from the catalog, never typed."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient

from treg import page_media
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store

WEB = Path(__file__).resolve().parents[1] / "src" / "treg" / "web"
ROOT = WEB.parents[2]


async def test_grokbot_icons_are_served_files(clients: AsyncClient):
    """The page used to inline every icon as base64 (half its weight, re-sent on every visit).
    Each image it names now has to resolve, or the icon silently disappears."""
    page = await clients.get("/grokbot")
    assert page.status_code == 200
    assert "data:image" not in page.text
    srcs = set(re.findall(r'<img[^>]*\ssrc="(/[^"]+)"', page.text))
    assert len(srcs) > 20
    for src in sorted(srcs):
        r = await clients.get(urlsplit(src).path)
        assert r.status_code == 200, src
        assert r.headers["content-type"].startswith("image/"), src


async def test_static_pages_fill_the_catalog_size(clients: AsyncClient):
    endpoints, providers = catalog_store.headline_counts(catalog_store.load())
    for path in ("/grokbot", "/fable", "/people-search", "/ugc", "/jev", "/gpt6", "/resources"):
        text = (await clients.get(path)).text
        assert f"{endpoints} " in text, path
    assert f"{providers} providers" in (await clients.get("/grokbot")).text


async def test_static_page_revalidates_to_a_304(clients: AsyncClient):
    """Filling placeholders turned these pages from FileResponses into rendered HTML; the ETag
    keeps a browser's `no-cache` revalidation cheap, including an edge-weakened tag."""
    first = await clients.get("/grokbot")
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-cache"
    for sent in (etag, f"W/{etag}", f'"other", {etag}'):
        again = await clients.get("/grokbot", headers={"If-None-Match": sent})
        assert again.status_code == 304, sent
        assert again.content == b""
    assert (await clients.get("/grokbot", headers={"If-None-Match": '"other"'})).status_code == 200


@pytest.mark.parametrize(("path", "policy"), [
    ("/media/landing/gateway.js", "no-cache"),
    ("/media/landing/gateway.css", "no-cache"),
    ("/media/grokbot/grokbot.png", "public, max-age=86400"),
    ("/media/og.png", "public, max-age=86400"),
])
async def test_media_cache_policy(clients: AsyncClient, path: str, policy: str):
    """Unversioned page media: code revalidates (a heuristic lifetime would pair stale scripts
    with new HTML), images keep a day like /logos."""
    r = await clients.get(path)
    assert r.status_code == 200, path
    assert r.headers["cache-control"] == policy
    if policy == "no-cache":
        again = await clients.get(path, headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304


def test_offloaded_media_names_the_current_bytes():
    """A manifest entry is a promise that the store holds these exact bytes. Editing a video
    without re-running scripts/offload_page_media.py would redirect to the old cut, so it fails
    here instead."""
    for path, key in page_media.manifest().items():
        file = page_media.MEDIA_DIR / path
        assert file.is_file(), path
        assert key == page_media.content_key(path, file.read_bytes()), path


async def test_offloaded_media_redirects_only_when_configured(clients: AsyncClient, monkeypatch):
    path, key = next(iter(page_media.manifest().items()))
    local = await clients.get(f"/media/{path}", headers={"Range": "bytes=0-15"})
    assert local.status_code == 206

    monkeypatch.setattr(get_settings(), "site_media_url", "https://cdn.example/site/")
    for method in (clients.get, clients.head):
        r = await method(f"/media/{path}", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == f"https://cdn.example/site/{key}"
    # A file without a published copy is still served here.
    kept = await clients.get("/media/og.png")
    assert kept.status_code == 200
    assert kept.headers["cache-control"] == "public, max-age=86400"


# A typed catalog size: "2,896 tools", "2,600+ data endpoints", "+ 2,867 more</b> tools", and any
# whole-catalog provider count ("60 providers", "60+ providers", "~80 providers"). Per-job figures
# ("3 providers, ranked", "16 providers") are facts about one capability and stay allowed.
_TYPED_ENDPOINTS = re.compile(r"\b\d{1,2},\d{3}\+?\s+(?:more\s+)?(?:[\w&;]+\s+){0,3}(?:tools|endpoints)\b", re.I)
_TYPED_PROVIDERS = re.compile(r"(?:~\s*\d+|\b\d+\+|\b[3-9]\d|\b\d{3,})\s+providers\b", re.I)


def _front_door_files() -> list[Path]:
    skip = {"dashboard", "vendor", "media"}
    files = [p for p in WEB.rglob("*") if p.suffix in {".html", ".js", ".md", ".txt"}
             and not skip & set(p.relative_to(WEB).parts)]
    return files + [ROOT / "README.md", ROOT / "docs" / "TUTORIAL.md"]


def test_no_page_types_the_catalog_size():
    """AGENTS.md: the catalog changes weekly and every stale number is a lie. Six pages once
    disagreed with each other (and with the catalog) on how many tools there are. A served page
    says `{ENDPOINTS}` / `{PROVIDERS}` and the route fills it; copy that is not templated states
    no count at all."""
    hits = []
    for path in _front_door_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            plain = re.sub(r"<[^>]+>", "", line)
            for rx in (_TYPED_ENDPOINTS, _TYPED_PROVIDERS):
                hits += [f"{path.relative_to(ROOT)}:{n}: {m.group(0)}" for m in rx.finditer(plain)]
    assert not hits, "\n".join(hits)
