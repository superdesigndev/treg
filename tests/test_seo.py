"""Crawler-facing surfaces: robots.txt, sitemap.xml, HEAD, template substitution and redirects.

These are easy to break silently: nothing in the app fails when a sitemap starts listing a
renamed route, and nobody notices until traffic does. So the sitemap test
walks every URL it publishes rather than spot-checking, and the host tests assert on
`public_url` rather than on the literal treg.to: a self-hosted registry must advertise itself.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest
from httpx import AsyncClient

from treg.config import get_settings


SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _base() -> str:
    return get_settings().public_url.rstrip("/")


def _locs(xml: str) -> list[str]:
    return [e.text or "" for e in ET.fromstring(xml).iter(f"{SITEMAP_NS}loc")]


# --------------------------------------------------------------------------------------- robots


async def test_robots_txt_keeps_crawlers_out_of_what_costs_or_gates(clients: AsyncClient):
    """The metered proxy and the authenticated app are the two that actually matter: one bills per
    request, the other has nothing to show a crawler."""
    body = (await clients.get("/robots.txt")).text
    for path in ("/app", "/call/", "/login", "/oauth/", "/docs/api"):
        assert f"Disallow: {path}" in body, path
    assert "Disallow: /catalog" not in body   # the catalog is the whole point of indexing us


# -------------------------------------------------------------------------------------- sitemap

async def test_sitemap_is_valid_xml_on_the_public_host(clients: AsyncClient):
    r = await clients.get("/sitemap.xml")
    assert r.status_code == 200, r.text
    assert "xml" in r.headers["content-type"]
    locs = _locs(r.text)
    assert len(locs) > 50, "the catalog shelves should dominate the sitemap"
    assert all(u.startswith(_base() + "/") or u == _base() + "/" for u in locs), locs[:3]
    assert f"{_base()}/" in locs
    assert f"{_base()}/catalog" in locs
    assert any(u.startswith(f"{_base()}/catalog/") for u in locs)


async def test_every_sitemap_url_answers_200(clients: AsyncClient):
    """The test that earns its keep: rename a route and the sitemap starts publishing 404s, with
    nothing else in the suite noticing. Walks a sample of the catalog pages plus every static one,
    since 88 full renders would dominate the suite's runtime."""
    locs = _locs((await clients.get("/sitemap.xml")).text)
    static = [u for u in locs if not u.startswith(f"{_base()}/catalog/")]
    shelves = [u for u in locs if u.startswith(f"{_base()}/catalog/")][:5]
    for url in static + shelves:
        path = url[len(_base()):] or "/"
        r = await clients.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code} (listed in sitemap.xml)"


async def test_sitemap_redirects_from_a_legacy_host(clients: AsyncClient):
    """A sitemap served on treg.superdesign.dev but full of treg.to URLs is a cross-submission a
    crawler may discard wholesale. Send it to the canonical copy instead."""
    for path in ("/robots.txt", "/sitemap.xml"):
        r = await clients.get(path, headers={"Host": "treg.superdesign.dev"},
                              follow_redirects=False)
        assert r.status_code == 301, path
        assert r.headers["location"] == f"{_base()}{path}"


# ------------------------------------------------------------------------------------------ HEAD

@pytest.mark.parametrize("path", ["/", "/tutorial", "/llms.txt", "/favicon.svg", "/support",
                                  "/robots.txt", "/sitemap.xml", "/catalog", "/meta"])
async def test_head_is_answered_wherever_get_is(clients: AsyncClient, path: str):
    """FastAPI's APIRoute never adds HEAD to a GET route, so every page 405'd on the probe crawlers
    and link unfurlers send first. api.py widens them all in one pass after registration."""
    r = await clients.head(path)
    assert r.status_code == 200, f"HEAD {path} -> {r.status_code}"
    assert r.content == b""


async def test_head_is_still_refused_where_there_is_no_get(clients: AsyncClient):
    """The widening must be surgical: a POST-only route keeps refusing HEAD."""
    assert (await clients.head("/tool-requests")).status_code == 405


# --------------------------------------------------------------------------------- catalog pages

async def test_the_prerender_is_a_sibling_of_the_vue_root(clients: AsyncClient):
    """Vue compiles #app's own innerHTML as its template, so prerendered markup inside it would be
    parsed as a template (and blow up on the first stray moustache). It must sit outside."""
    body = (await clients.get("/catalog")).text
    assert body.index('id="prerender"') < body.index('id="app"')
    app_html = body[body.index('<div id="app"'):]
    assert 'id="prerender"' not in app_html


async def test_the_json_catalog_routes_still_answer_json(clients: AsyncClient):
    """`/catalog/<slug>` sits in front of these. Registration order keeps them matching first, and
    if that ever changes the dashboard and every CLI break at once."""
    for path in ("/catalog/platforms", "/catalog/platforms/google", "/catalog/search?q=backlinks",
                 "/catalog/endpoints/moz.web.url.metrics"):
        r = await clients.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("application/json"), path
    # These two ARE valid URLs, claimed by the JSON routes registered before `/catalog/{slug}`.
    # What must never happen is the page route swallowing one and serving HTML to the dashboard.
    for slug in ("platforms", "search"):
        r = await clients.get(f"/catalog/{slug}")
        assert r.headers["content-type"].startswith("application/json"), slug


@pytest.mark.parametrize("slug", ["endpoints", "examples", "not-a-platform"])
async def test_unknown_slugs_404(clients: AsyncClient, slug: str):
    """`endpoints` and `examples` reach the page route (their JSON siblings need a trailing id), so
    only the reserved-word guard stops them rendering a nonsense shelf."""
    assert (await clients.get(f"/catalog/{slug}")).status_code == 404


# ------------------------------------------------------------------------------------------ docs

async def test_widening_head_did_not_leak_into_the_public_schema(clients: AsyncClient):
    """Adding HEAD to every GET route gave FastAPI a second operation per path — 58 duplicate
    entries in openapi.json, each with a duplicate operation id. Only the /call proxy, which
    declares HEAD itself, should have one."""
    paths = (await clients.get("/openapi.json")).json()["paths"]
    with_head = [p for p, ops in paths.items() if "head" in ops]
    assert with_head == ["/call/{rest}"], with_head


def test_no_shelf_is_published_that_the_app_grid_hides():
    """Adding a platform is a data-only change — drop the YAML in and it appears on both sides. The
    one way that breaks: `catalog_store` auto-registers a platform with no `platforms:` entry in
    capabilities.yaml as `category: "Other"` (domain/catalog/store.py, `platforms.setdefault`), and the
    dashboard's `platCategories` skips `Other` outright (`if(c==='Other') continue`). The shelf page
    would still render and the sitemap would still publish it — but nothing in the app's own grid
    would link to it. Give the new platform a label and category in capabilities.yaml.
    """
    from treg.routers.catalog import _platform_rows
    orphans = [r["slug"] for r in _platform_rows() if r["category"] == "Other"]
    assert not orphans, (
        f"{orphans} have endpoints but no capabilities.yaml `platforms:` entry — the sitemap will "
        "publish /catalog/<slug> for each while the app's tile grid hides them")


@pytest.mark.parametrize("path", ["/", "/support", "/terms", "/privacy", "/tutorial", "/catalog",
                                  "/robots.txt", "/skill.md", "/llms.txt", "/.well-known/skill.md"])
async def test_no_page_ships_an_unsubstituted_base(clients: AsyncClient, path: str):
    """`{BASE}` reaching a browser means a canonical or og:url is pointing at nothing, and an
    unfilled `{ENDPOINTS}` or `{PROVIDERS}` puts a template on the front door."""
    text = (await clients.get(path)).text
    for placeholder in ("{BASE}", "{ENDPOINTS}", "{PROVIDERS}"):
        assert placeholder not in text, (path, placeholder)


# ------------------------------------------------------------------------------------ redirects

async def test_indexnow_key_is_served_from_the_root(clients: AsyncClient):
    from treg.routers.web import INDEXNOW_KEY
    r = await clients.get(f"/{INDEXNOW_KEY}.txt")
    assert r.status_code == 200 and r.text == INDEXNOW_KEY


async def test_grok_bot_redirects_to_grokbot(clients: AsyncClient):
    """/agents/grok-bot and /agents/grok-bot.md 301 to /grokbot: the launch page is the
    primary destination for "Grok Bot" clicks."""
    r = await clients.get("/agents/grok-bot", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/grokbot"
    r = await clients.get("/agents/grok-bot.md", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/grokbot"
