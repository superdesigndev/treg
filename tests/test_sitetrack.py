"""Product analytics on the marketing surface: every page a visitor can land on loads /sitetrack.js.

`/sitetrack.js` is what makes a landing page measurable at all — it boots PostHog with pageviews on
and sets the first-touch `treg_utm` cookie. A page without it is not "under-tracked": it emits
nothing, so its visitors, signups, activations and top-ups cannot be attributed to it, and no
error ever says so. That is exactly how `/grokbot` shipped: a launch landing page behind its own
route, hand-written HTML outside the `_page()` shell, carrying `/adtrack.js` and `/gtag.js` but not
this — a week of launch traffic with zero pageviews (found 2026-09-07 while comparing landing
routes, when the route simply did not appear in the data).

Two guards, same shape as `test_adsconv.py`'s ad-capture guards:

1. The hand-kept list of marketing surfaces, fetched over HTTP, so a route that serves the wrong
   file or drops the tag on the way out fails here.
2. A file-level invariant that needs no maintenance: any hand-written page in `web/` that is
   instrumented for ads (`/adtrack.js` or `/gtag.js`) is a marketing page, and a marketing page is
   instrumented for analytics too. A new standalone landing page copied from an existing one is in
   scope the moment it exists.

The server-rendered `_page()` shell is deliberately OUT of scope: it carries `/adtrack.js` but not
`/sitetrack.js`, a product/legal call documented on `_page` itself. This file guards the
hand-written surface only.
"""

from __future__ import annotations

import re
from pathlib import Path

from httpx import AsyncClient

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "treg" / "web"

# The tag must be root-absolute. `people-search.html` and `fable-gtm.html` shipped with a relative
# `src="sitetrack.js"`; that resolves at `/fable` but breaks the moment a page is served under a
# path with a trailing slash or a sub-directory, and it makes the tag invisible to a plain
# `"/sitetrack.js" in text` check — the same check the other guards use.
SITETRACK_TAG = re.compile(r'<script\s+src="/sitetrack\.js"')
AD_TAG = re.compile(r'<script\s+src="/(?:adtrack|gtag)\.js"')  # a tag, not prose about one (privacy.html)


async def test_every_marketing_surface_loads_the_analytics_script(clients: AsyncClient):
    """Every standalone landing page must load /sitetrack.js. Add a new one HERE in the same
    commit as its route."""
    surfaces = [
        "/",
        "/resources",
        "/people-search",
        "/grokbot",
        "/fable",
        "/gpt6",
        "/use-cases/seo-data-for-ai-agents",
        "/use-cases/lead-enrichment-for-ai-agents",
        "/use-cases/social-trend-research-for-ai-agents",
        "/use-cases/competitor-ad-research-for-ai-agents",
        "/use-cases/company-research-for-ai-agents",
    ]
    missing = []
    for path in surfaces:
        r = await clients.get(path)
        assert r.status_code == 200, f"{path} -> HTTP {r.status_code}"
        if not SITETRACK_TAG.search(r.text):
            missing.append(path)
    assert not missing, (
        f"landing pages that do not load /sitetrack.js: {missing}. Their visitors emit no "
        "pageview, so nothing they do can be attributed to the page."
    )


def test_every_ad_instrumented_page_is_also_analytics_instrumented():
    """A hand-written page that loads the ad scripts is a marketing page; it loads /sitetrack.js.

    This is the structural net. It reads the files, not the routes, so it catches a page before
    anyone remembers to add it to the list above — and it is what would have caught `/grokbot`.
    """
    missing = []
    for page in sorted(WEB_DIR.glob("*.html")):
        text = page.read_text()
        if not AD_TAG.search(text):
            continue
        if not SITETRACK_TAG.search(text):
            missing.append(page.name)
    assert not missing, (
        f"pages with ad tracking but no analytics: {missing}. Add "
        '`<script src="/sitetrack.js"></script>` before `/adtrack.js`.'
    )


def test_no_page_loads_a_tracking_script_by_relative_path():
    """`src="sitetrack.js"` (no leading slash) resolves only by luck of the route's shape."""
    bad = []
    for page in sorted(WEB_DIR.glob("*.html")):
        for m in re.finditer(r'<script\s+src="([^"]*track\.js|[^"]*gtag\.js)"', page.read_text()):
            if not m.group(1).startswith("/"):
                bad.append(f"{page.name}: {m.group(0)}")
    assert not bad, f"relative tracking script paths: {bad}"
