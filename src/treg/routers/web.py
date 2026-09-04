"""Presentation-only web, SEO, tutorial, and public-document routes."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import html as _html
import html as html_mod
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import adsconv, agent_pages, oauth_providers
from ..domain import referrals
from ..domain.catalog import store as catalog_store
from ..domain.identity import session as sess
from ..config import PUBLIC_HOST_ALIASES, get_settings
from ..infra.db import get_session
from ..models import User
from ..domain.catalog import stats as endpoint_stats
from .catalog import (_endpoint_observation_reader, _observed_or_empty, _platform_rows,
                      _provider_display, catalog_platform)
from ..domain.identity.access import _user_from_session
from .auth_helpers import OAUTH_RETURN_COOKIE, _is_https, _take_oauth_return
from .signup_cookies import _remember_referral


LOCAL_USER_EMAIL = "you@local.treg"   # the single-user identity; a real address is never needed


async def _local_owner(db: AsyncSession) -> User | None:
    """The single-user identity, if this deployment is in that mode."""
    if not get_settings().single_user_ok:
        return None
    return (await db.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one_or_none()


# One level deeper than api.py, so anchor assets to the package root.
_WEB_DIR = Path(__file__).parent.parent / "web"


# The app alias preserves the moved handlers' original @app.get decorator text byte-for-byte.
catalog_pages_router = APIRouter()
app = catalog_pages_router


# ---- the crawlable catalog: /catalog and /catalog/<slug> -------------------------------------
#
# The JSON routes above are what agents and the dashboard read. These two render the SAME data as
# server-side HTML, because until now none of it had a URL: the dashboard browses platforms through
# hash routes (/app#platform/<slug>) behind a login, so ~2,600 endpoints across 80 shelves were
# invisible to every crawler and every AI answer engine. No JavaScript here on purpose — the text IS
# the product surface, and it has to be readable by something that will not run a script or click.
#
# `/catalog/<slug>` is registered after the JSON routes so /catalog/platforms, /catalog/search,
# /catalog/endpoints/… and /catalog/examples/… keep matching first. Registration order alone is a
# thin guarantee, so the reserved names are also refused explicitly below.
_CATALOG_RESERVED = {"platforms", "search", "endpoints", "examples"}

_GH = "https://github.com/superdesigndev/treg"


def _usd_short(usd: float) -> str:
    """A dollar figure a person can read. `%g` flips to scientific notation below 1e-4, and a shelf
    advertising "from $1.2e-07 per call" reads as a bug rather than as a price — so anything under
    a hundredth of a cent is labelled as such instead."""
    if not usd:
        return "free"
    return "<$0.0001" if usd < 0.0001 else f"${usd:.3g}"


def _price_label(cost: dict | None) -> str:
    """A price in ONE currency, so rows down a page stay comparable. Mirrors `_cost_usd` in cli.py
    rather than importing it: pulling treg.cli into the server process costs ~200ms and drags the
    whole CLI in for one string (see `_treg_version`)."""
    if not isinstance(cost, dict):
        return ""
    usd = cost.get("usd")
    if usd is None:
        return "own account"     # no rate published — never invent a dollar figure
    if not usd:
        return "free"
    unit = {"per_call": "call", "per_result": "result", "per_success": "success"}.get(
        cost.get("type"), "call")
    return f"{_usd_short(usd)}/{unit}"


def _css_stamp(name: str = "catalog.css") -> str:
    """The stylesheet's own mtime, stamped onto its URL. Skins are served with a real max-age
    (they are static and every page pulls them), so without a stamp an edited skin keeps rendering
    from the browser's copy until the cache expires — the trap `/tutorial.js` already guards."""
    f = _WEB_DIR / name
    try:
        return str(int(f.stat().st_mtime))
    except OSError:
        return "0"


def _serp_desc(text: str, limit: int = 155) -> str:
    """A meta description Google will print whole. Past ~155 characters it truncates mid-sentence,
    so cut at the last sentence that fits, then at the last word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "? ", "! "):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[:i + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "."


# Google prints about 60 characters of a title; tests/test_agent_pages.py holds every page to 65.
_TITLE_MAX = 65


def _page(title: str, description: str, path: str, body: str, ld: list[dict],
          *, nav_current: str = "", head_extra: str = "", css: str = "catalog.css") -> HTMLResponse:
    """The shared shell for every server-rendered page. One place that owns <title>, the meta
    description, the canonical, the og/twitter card and the JSON-LD, so a new page cannot ship
    without them — that omission is exactly what left the landing page bare for a year.

    It owns `/adtrack.js` for the same reason. That script was on the hand-written marketing HTML
    (landing, usecase-*, resources, index) and on nothing rendered here, so every page off this
    shell was invisible to paid attribution: no `treg_ad` cookie means
    `signup._ad_attribution_from` returns empty, `org.ad_gclid` stays NULL, and `adsconv.queue()`
    no-ops by design — a paid click could sign up and make its first call and Google would never
    hear about it. It fails silently, with nothing in the logs. Keep it here, not per page.

    `/sitetrack.js` is deliberately NOT here. It already shipped more widely (it is on
    `tutorial.html` too), but it can load PostHog with pageview/session-recording config, which
    contradicts what `web/privacy.html` promises and lists no such processor. Broadening it to the
    whole server-rendered surface is a product/legal call, not a side effect of fixing ad
    attribution. `treg_ad` and `/adtrack.js` are already documented in that policy, so this is not.

    The "Start free" CTA carries `?ref=<page>`: a logged-out visit to bare `/app` is bounced to the
    marketing landing with nothing open, which loses the page the visitor was reading. With `ref`
    the app keeps them and opens sign-in in place (see the boot in index.html), and the page that
    produced the signup is recorded."""
    base = get_settings().public_url.rstrip("/")
    ref = quote(path.strip("/").replace("/", "-") or "home", safe="")
    t, d = _esc_html(title), _esc_html(description)
    url = _esc_html(base + path)  # `path` reaches attribute context — escape it like title/description
    # `<` escaped to its \u form inside the JSON: a catalog label containing "</script>" would
    # otherwise close the block early and put the rest of the payload into the document as markup.
    # Still valid JSON, so parsers and Google's validator read it unchanged.
    blocks = "\n".join(
        '<script type="application/ld+json">'
        + json.dumps(b, separators=(",", ":")).replace("<", "\\u003c")
        + "</script>"
        for b in ld)
    def navlink(href: str, label: str, extra: str = "") -> str:
        cur = ' aria-current="page"' if href == nav_current else ""
        return f'<a href="{href}"{cur}{extra}>{label}</a>'
    # The job, workflow and agent pages exist on the hosted deployment only (`_hosted`): a
    # self-hosted registry must not put three 404s in its own footer.
    hub_links = ('<a href="/use-cases">Use cases</a><a href="/workflows">Workflows</a>'
                 '<a href="/agents">Agents</a>' if _hosted() else "")
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{t}</title>
<meta name="description" content="{d}"/>
<link rel="canonical" href="{url}"/>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<meta property="og:type" content="website"/>
<meta property="og:site_name" content="treg"/>
<meta property="og:url" content="{url}"/>
<meta property="og:title" content="{t}"/>
<meta property="og:description" content="{d}"/>
<meta property="og:image" content="{base}/media/og.png"/>
<meta property="og:image:width" content="1200"/>
<meta property="og:image:height" content="630"/>
<meta property="og:image:alt" content="treg.to: one key for the whole tool catalog, priced per call"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:title" content="{t}"/>
<meta name="twitter:description" content="{d}"/>
<meta name="twitter:image" content="{base}/media/og.png"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Pixel&family=Inter:wght@400;450;500;600;650;700&family=DM+Mono:ital,wght@0,400;0,500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/{css}?v={_css_stamp(css)}"/>
{head_extra}
{blocks}
</head>
<body>
<div class="navwrap"><nav class="nav">
  <a class="brand" href="/"><span class="glyph">▚</span> treg</a>
  <div class="links">
    {navlink("/catalog", "Catalog")}
    {navlink("/tutorial", "Tutorial")}
    {navlink("/docs", "API")}
    <a class="hidem" href="{_GH}" target="_blank" rel="noopener">GitHub ↗</a>
    <a class="candy" href="/app?ref={ref}">Start free</a>
  </div>
</nav></div>
{body}
<footer>
  <div class="foot-in">
    <div class="foot-brand">
      <div class="brand"><span class="glyph">▚</span> treg</div>
      <div class="note">100% open source</div>
    </div>
    <nav class="foot-cols" aria-label="Site">
      <div class="foot-col"><div class="lab">Explore</div>
        <a href="/catalog">Catalog</a>{hub_links}</div>
      <div class="foot-col"><div class="lab">Build</div>
        <a href="/tutorial">Docs</a><a href="/docs">API</a><a href="/llms.txt">llms.txt</a
        ><a href="{_GH}" target="_blank" rel="noopener">GitHub ↗</a></div>
      <div class="foot-col"><div class="lab">Company</div>
        <a href="/resources">Resources</a><a href="/support">Support</a
        ><a href="/terms">Terms</a><a href="/privacy">Privacy</a></div>
    </nav>
  </div>
</footer>
<script src="/adtrack.js"></script>
</body>
</html>""", headers={"Cache-Control": "public, max-age=600"})


def _spa_catalog_page(title: str, description: str, path: str, ld: list[dict],
                      prerender: str) -> HTMLResponse:
    """Serve the dashboard SPA at a PUBLIC catalog URL, with the head a crawler needs.

    The public catalog is not a second implementation of the marketplace — it IS the marketplace.
    `/catalog` and `/catalog/<slug>` hand back `index.html`, and the Vue app renders the same
    platform views a member sees (its catalog API is unauthenticated, so it works signed out; see
    `publicCatalog` in index.html). That is the whole point: one UI, so the two can never drift
    apart visually the way a hand-built copy would.

    Two things have to be added on the way out:

    1. **The head.** The SPA ships one bare `<title>treg</title>`. Every catalog URL needs its own
       title, description, canonical, og/twitter card and JSON-LD, so they are substituted in here —
       the same trick `_spa_with_og` uses for shared skill/tool links.
    2. **A no-JS fallback.** Vue compiles `#app`'s own innerHTML as its template, so prerendered
       markup cannot go inside it. `#prerender` is therefore a SIBLING, removed by the app on boot.
       It is deliberately plainer than the Vue view — the ledger's row-merging is a chain of
       client-side computeds, and reproducing it server-side would recreate exactly the duplicate
       implementation this design avoids. It carries the TEXT (names, summaries, providers, prices),
       which is what a crawler that does not run scripts is here for.
    """
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    base = get_settings().public_url.rstrip("/")
    t, d = _esc_html(title), _esc_html(description)
    # `path` carries the {slug} from the URL. Today an unknown slug 404s in catalog_platform before
    # it reaches here, so a quote can't get this far — but that is an upstream lookup's side effect,
    # not a guarantee this function makes. Escape it where it is used, so a future "slug not found →
    # suggestions" page cannot turn a canonical tag into a reflected XSS.
    url = _esc_html(base + path)
    blocks = "\n".join(
        '<script type="application/ld+json">'
        + json.dumps(b, separators=(",", ":")).replace("<", "\\u003c") + "</script>"
        for b in ld)
    meta = (
        f"<title>{t}</title>\n"
        f'<meta name="description" content="{d}"/>\n'
        f'<link rel="canonical" href="{url}"/>\n'
        f'<meta name="robots" content="index, follow"/>\n'   # index.html defaults to noindex
        f'<meta property="og:type" content="website"/>\n'
        f'<meta property="og:site_name" content="treg"/>\n'
        f'<meta property="og:url" content="{url}"/>\n'
        f'<meta property="og:title" content="{t}"/>\n'
        f'<meta property="og:description" content="{d}"/>\n'
        f'<meta property="og:image" content="{base}/media/og.png"/>\n'
        f'<meta property="og:image:width" content="1200"/>\n'
        f'<meta property="og:image:height" content="630"/>\n'
        f'<meta name="twitter:card" content="summary_large_image"/>\n'
        f'<meta name="twitter:title" content="{t}"/>\n'
        f'<meta name="twitter:description" content="{d}"/>\n'
        f'<meta name="twitter:image" content="{base}/media/og.png"/>\n'
        + blocks
    )
    html = index.read_text(encoding="utf-8")
    # index.html carries `robots: noindex` for the authenticated app; these URLs are public, and the
    # `index, follow` in `meta` only wins if the noindex is gone. Stripped BEFORE `meta` is spliced
    # in, so this scan only ever runs over the static bundle — never over a string carrying a
    # caller-supplied title, which is what made it a ReDoS candidate rather than a fixed-cost pass.
    html = re.sub(r'<meta name="robots" content="noindex[^>]*>\s*', "", html, count=1)
    # Match whatever title the page carries, not one exact string — a rename in the dashboard must
    # not be able to switch every catalog page's head off without a word (the same failure
    # `_spa_with_og` was written to survive).
    html, hits = re.subn(r"<title>.*?</title>", lambda _m: meta, html, count=1,
                         flags=re.IGNORECASE | re.DOTALL)
    if not hits:
        html = html.replace("<head>", "<head>\n" + meta, 1)
    marker = '<div id="app"'
    if marker in html:
        html = html.replace(marker, f'<div id="prerender">{prerender}</div>\n{marker}', 1)
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=600"})


# The fallback's own skin. Scoped to #prerender and written against the dashboard's OWN tokens
# (already defined in index.html), so it reads as the same product for the moment it is on screen.
_PRERENDER_CSS = """<style>
#prerender{max-width:1100px;margin:0 auto;padding:38px 26px 60px;font-family:var(--sans,system-ui);
  color:var(--ink,#1a1a1a)}
#prerender h1{font-size:30px;letter-spacing:-.01em;margin:0 0 8px}
#prerender .lede{color:var(--muted,#7c7c7c);margin:0 0 20px;max-width:64ch}
#prerender h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted2,#989898);margin:26px 0 10px;padding-bottom:8px;
  border-bottom:1px solid var(--line,#26262322)}
#prerender ul{list-style:none;margin:0;padding:0}
#prerender li{padding:9px 0;border-bottom:1px solid var(--line,#26262322)}
#prerender li b{font-weight:600}
#prerender li i{font-style:normal;color:var(--muted,#7c7c7c);display:block;font-size:13.5px}
#prerender .m{font-family:var(--mono,ui-monospace);font-size:11.5px;
  color:var(--muted2,#989898);margin-top:3px;display:block}
#prerender a{color:var(--teal,#1a7da6);text-decoration:none}
</style>"""


@app.get("/catalog", include_in_schema=False)
async def catalog_index():
    """The catalog index — the marketplace's Catalog view, on a public, indexable URL."""
    base = get_settings().public_url.rstrip("/")
    rows = _platform_rows()
    # The WHOLE catalog, not the sum of the tiles: a tile counts only its browse surface, so the
    # account/utility endpoints (real inventory, listed on each shelf page) would go uncounted and
    # this page would quietly contradict the number on the landing.
    cat = catalog_store.load()
    total_eps = len(cat.endpoints)
    providers = sorted({e["provider"] for e in cat.endpoints})

    cats: dict[str, list[dict]] = {}
    for row in rows:
        cats.setdefault(row["category"], []).append(row)
    sections = []
    for name, items in cats.items():
        lis = []
        for r in items:
            price = _price_label(r["price_from"])
            vendors = ", ".join(_provider_display(p) for p in r["providers"])
            lis.append(
                f'<li><b><a href="/catalog/{_esc_html(r["slug"])}">{_esc_html(r["label"])}</a></b>'
                f'<i>{_esc_html(r["summary"])}</i>'
                f'<span class="m">{r["endpoints"]} endpoints · {r["capabilities"]} capabilities'
                + (f" · from {_esc_html(price)}" if price else "")
                + f" · {_esc_html(vendors)}</span></li>")
        sections.append(f"<h2>{_esc_html(name)}</h2><ul>{''.join(lis)}</ul>")

    # The provider links live HERE, in the crawlable prerender, rather than on an index page of
    # their own: /providers earned no searches and made a second "browse everything" URL beside
    # this one. The /tools pages still get their internal links; there is just one index.
    prov_rows = _provider_rows()
    prov_links = " · ".join(
        f'<a href="/tools/{_esc_html(r["service"])}">{_esc_html(r["display"])}</a>'
        for r in prov_rows)
    prerender = (_PRERENDER_CSS
                 + "<h1>The tool catalog</h1>"
                 + f'<p class="lede">{total_eps:,} endpoints across {len(rows)} platforms and '
                   f"{len(providers)} providers — every tool your agent can call through one key, "
                   "priced up front and billed per call, with no provider signup.</p>"
                 # The two hubs are linked from HERE as well as the nav: this prerender is the page
                 # Google crawls most, and before this line the job and workflow pages were reachable
                 # only from the sitemap — "URL is unknown to Google" on every one of them.
                 + ('<p>Looking for a job rather than a platform? <a href="/use-cases">The use cases</a> '
                    'compare the providers that do one job, <a href="/workflows">the workflows</a> '
                    'chain several jobs into one prompt with the price of each step, and '
                    '<a href="/agents">the agent pages</a> show the whole menu for one '
                    'agent.</p>' if _hosted() else "")
                 + "".join(sections)
                 + f"<h2>The providers</h2><p>{len(prov_rows)} vendors serve this catalog, each "
                   f"with its own page: {prov_links}</p>")

    ld = [
        {"@context": "https://schema.org", "@type": "ItemList",
         "name": "treg tool catalog",
         "description": f"{total_eps} API endpoints across {len(rows)} platforms, callable through one key.",
         "numberOfItems": len(rows),
         "itemListElement": [
             {"@type": "ListItem", "position": i, "name": r["label"],
              "url": f"{base}/catalog/{r['slug']}"}
             for i, r in enumerate(rows, 1)]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Catalog", "item": base + "/catalog"}]},
    ]
    return _spa_catalog_page(
        f"Tool catalog — {total_eps:,} API endpoints your agent can call | treg",
        f"Browse {total_eps:,} endpoints across {len(rows)} platforms and {len(providers)} providers "
        "— SEO, social, enrichment, ads and scraping data. One key, priced per call, no provider signup.",
        "/catalog", ld, prerender)


@app.get("/catalog/{slug}", include_in_schema=False)
async def catalog_page(slug: str):
    """One platform shelf — the marketplace's platform view, on a public, indexable URL."""
    if slug in _CATALOG_RESERVED:
        raise HTTPException(status_code=404, detail=f"unknown platform {slug!r}")
    # include_hidden=1, exactly as the SPA asks for it (see `loadPlatform`): the account/utility
    # endpoints are real inventory and the page files them in their own section rather than hiding
    # them. Asking for a different population than the view that is about to replace this would put
    # two different endpoint counts on one URL.
    detail = await catalog_platform(slug, include_hidden=1)
    base = get_settings().public_url.rstrip("/")
    plat = detail["platform"]
    label, category = plat["label"], plat["category"]
    row = next((r for r in _platform_rows() if r["slug"] == slug), None)
    summary = (row or {}).get("summary", "")
    caps = detail["capabilities"]
    eps = [e for cap in caps for e in cap["endpoints"]] + detail["extended"]
    prices = [c["usd"] for e in eps if isinstance(c := e.get("cost"), dict) and c.get("usd")]
    cheapest = _usd_short(min(prices)) if prices else ""

    blocks = []
    for cap in caps:
        lis = []
        for e in cap["endpoints"]:
            price = _price_label(e.get("cost"))
            bits = [_esc_html(e["provider_display"])]
            if e.get("verified"):
                bits.append("live-verified")
            if price:
                bits.append(_esc_html(price))
            bits.append(_esc_html(e["id"]))
            lis.append(f'<li><b>{_esc_html(e["name"])}</b>'
                       f'<i>{_esc_html(e.get("summary") or "")}</i>'
                       f'<span class="m">{" · ".join(bits)}</span></li>')
        blocks.append(f'<h2>{_esc_html(cap["description"] or cap["id"])}</h2><ul>{"".join(lis)}</ul>')

    provs = ", ".join(p["display_name"] for p in detail["providers"].values())
    prerender = (_PRERENDER_CSS
                 + f'<p class="m"><a href="/catalog">← Catalog</a> · {_esc_html(category)}</p>'
                 + f"<h1>{_esc_html(label)}</h1>"
                 + f'<p class="lede">{_esc_html(summary)} {len(eps)} endpoints from '
                   f"{_esc_html(provs)}"
                 + (f", from {_esc_html(cheapest)} per call" if cheapest else "")
                 + ". Jobs that several providers do sit on one row, so you can compare price and "
                   "coverage before you spend a call — <b>choosing is yours</b>; treg does not route "
                   "between providers automatically.</p>"
                 + "".join(blocks))

    desc = (f"{len(eps)} {label.lower()} API endpoints from "
            f"{', '.join(p['display_name'] for p in list(detail['providers'].values())[:3])}"
            + (f", from {cheapest} per call" if cheapest else "")
            + ". Call them through one treg key — no provider signup.")
    ld = [
        {"@context": "https://schema.org", "@type": "ItemList",
         "name": f"{label} — API endpoints on treg",
         "numberOfItems": len(caps),
         "itemListElement": [
             {"@type": "ListItem", "position": i, "name": cap["description"] or cap["id"],
              "url": f"{base}/catalog/{slug}#{cap['id']}"}
             for i, cap in enumerate(caps, 1)]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Catalog", "item": base + "/catalog"},
            {"@type": "ListItem", "position": 3, "name": label, "item": f"{base}/catalog/{slug}"}]},
    ]
    return _spa_catalog_page(f"{label} API — {len(eps)} endpoints, priced per call | treg",
                             desc[:300], f"/catalog/{slug}", ld, prerender)


# --------------------------------------------------------------------------- /agents/<agent>

def _hosted() -> bool:
    """True on the reference deployment only. The agent pages describe treg.to's own listings (the
    ChatGPT Connector, the OAuth connector, the free grant), none of which is true of a self-hosted
    registry, so off these hosts the pages do not exist rather than lie."""
    host = (urlsplit(get_settings().public_url).hostname or "").lower()
    return host in PUBLIC_HOST_ALIASES


def _pub(e: dict) -> bool:
    """An endpoint the PUBLIC pages may count or list: hidden utility kinds out, and the
    `kind: routed` meta-rows (PR #242) out with them — a routed row delegates to children that
    are already on the page, so anywhere public it double-counts and surfaces a provider named
    "treg", which the brand rules say must never appear as a vendor."""
    return e["kind"] not in catalog_store.HIDDEN_KINDS and e.get("kind") != "routed"


def _catalog_census() -> tuple[int, int]:
    """(browse-surface endpoint count, platform count): the two numbers the agent pages state."""
    cat = catalog_store.load()
    browse = [e for e in cat.endpoints if _pub(e)]
    return len(browse), len({e["platform"] for e in browse})


def _anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _logo(domain: str | None, alt: str) -> str:
    """A 20px brand mark from the favicon service the landing uses, or the treg glyph when the
    brand is unknown (never a wrong logo)."""
    if not domain:
        return '<span class="lg lg-none" aria-hidden="true">▚</span>'
    return (f'<img class="lg" src="https://www.google.com/s2/favicons?domain={_esc_html(domain)}&amp;sz=64" '
            f'alt="{_esc_html(alt)}" width="20" height="20" loading="lazy"/>')


def _job_category(label: str) -> str:
    """The category a job belongs to, looked up by label. Category is METADATA, never part of the
    URL: Composio files every blueprint at a flat /use-case/<slug> and renders the category as a
    chip, which is why re-cutting their taxonomy costs them nothing. Ours does the same now, after
    one re-cut taught us the price of putting it in the path."""
    for category, jobs in agent_pages.USE_CASES:
        for lbl, _ in jobs:
            if lbl == label:
                return category
    return ""


def _use_case_page_for(label: str) -> str | None:
    """The flat URL for a job, or None when no page has been written for it."""
    for slug, spec in agent_pages.USE_CASE_PAGES.items():
        if spec["label"] == label:
            return f"/use-cases/{slug}"
    return None


def _related_link(label: str, agent_slug: str) -> tuple[str, str]:
    """(href, owning category) for a `related` job, resolved by LABEL across the whole menu.

    Four categories carry fewer than five jobs, so `related` has to cross categories there. It used
    to resolve inside the current page's category only, which sent those cards to the wrong anchor
    under a caption naming the wrong category. The 66 labels are unique (tested), so the label is
    enough to find the owner.
    """
    for category, jobs in agent_pages.USE_CASES:
        for lbl, _ in jobs:
            if lbl == label:
                return (_use_case_page_for(label)
                        or f"/agents/{agent_slug}#{agent_pages.category_slug(category)}"), category
    return f"/agents/{agent_slug}", ""


def _use_case_caps(label: str) -> tuple[str, ...]:
    for _category, jobs in agent_pages.USE_CASES:
        for lbl, caps in jobs:
            if lbl == label:
                return caps
    return ()


# ---- the reverse index: which job pages use a provider, which workflows use a job -------------
#
# Every job page already links DOWN to the catalog and every workflow step links down to its job
# page. Nothing linked UP: a provider page did not name the jobs it serves, and a job page did not
# name the workflows that chain it. The result was measurable — the 38 job pages and both workflow
# pages had zero impressions and "URL is unknown to Google", while /tools/<provider> (linked from
# /catalog) was indexed. These two indexes are computed once per process from the same tables the
# pages render from, so a new job or workflow is cross-linked the moment it is routed.

@lru_cache(maxsize=1)
def _jobs_by_provider() -> dict[str, list[tuple[str, str]]]:
    """provider service -> [(job slug, job sentence)], for every job whose capabilities the
    provider answers. Ordered by the job table so the list reads the way the hub does."""
    cat = catalog_store.load()
    out: dict[str, list[tuple[str, str]]] = {}
    for slug, spec in agent_pages.USE_CASE_PAGES.items():
        provs = {e["provider"] for cid in _use_case_caps(spec["label"])
                 for e in cat.for_capability(cid) if _pub(e)}
        for p in provs:
            out.setdefault(p, []).append((slug, spec["sentence"]))
    return out


@lru_cache(maxsize=1)
def _workflows_by_capability() -> dict[str, list[tuple[str, str]]]:
    """capability id -> [(workflow slug, workflow sentence)] for every workflow with a step on it."""
    out: dict[str, list[tuple[str, str]]] = {}
    for slug, spec in agent_pages.WORKFLOWS.items():
        for _name, cid, *_rest in spec.get("steps", ()):
            out.setdefault(cid, []).append((slug, spec["sentence"]))
    return out


def _workflows_for_caps(caps: tuple[str, ...]) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for cid in caps:
        for slug, sentence in _workflows_by_capability().get(cid, ()):
            seen.setdefault(slug, sentence)
    return list(seen.items())


def _menu_rows(cat, category: str, jobs) -> list[dict]:
    """The use-case menu for one category, priced from the catalog. Shared by the HTML and the
    Markdown renderings of the agent page so the two can never list different jobs."""
    rows = []
    for label, caps in jobs:
        eps = [e for cid in caps for e in cat.for_capability(cid) if _pub(e)]
        if not eps:  # the test forbids this, but a page must never render an empty promise
            continue
        prices = [c["usd"] for e in eps if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"]]
        plats, seen = [], set()
        for cid in caps:
            ceps = [e for e in cat.for_capability(cid) if _pub(e)]
            if not ceps:
                continue
            slug = ceps[0]["platform"]
            plats.append({"cap": cid, "slug": slug, "dup": slug in seen,
                          "label": (cat.platforms.get(slug) or {}).get("label") or slug,
                          "domain": agent_pages.PLATFORM_DOMAINS.get(slug)})
            seen.add(slug)
        rows.append({"label": label, "caps": caps, "platforms": plats,
                     "providers": len({e["provider"] for e in eps}),
                     "verified": sum(1 for e in eps if e["verified"]),
                     "from_usd": min(prices) if prices else None,
                     # no priced endpoint at all = the team's own account does the job, unmetered
                     "own_account": not prices,
                     "page": _use_case_page_for(label)})
    return rows


_COPY_JS = """
<script>
document.querySelectorAll('button[data-copy]').forEach(function(b){
  b.addEventListener('click', async function(){
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent='copied'; b.classList.add('done');
          setTimeout(function(){ b.textContent='copy'; b.classList.remove('done'); }, 1400); } catch(e) {}
  });
});
</script>"""

_MD_ALT = '<link rel="alternate" type="text/markdown" href="{href}"/>'


@app.get("/agents", include_in_schema=False)
async def agents_hub():
    """The hub the agent pages hang from. Until this existed the nav's "Agents" link pointed at one
    client's page (/agents/claude-code) because there was nowhere else to point it, and the bare URL
    404ed while every agent page's breadcrumb implied it existed."""
    if not _hosted():
        raise HTTPException(status_code=404, detail="not found")
    base = get_settings().public_url.rstrip("/")
    n_eps, n_plats = _catalog_census()
    n, p = f"{n_eps:,}", str(n_plats)
    def _blurb(defn: str) -> str:
        # The card answers "which client, and does it install as a plugin or an MCP server" — the
        # definition's opening clause. The counts already live on the meta line; printing the whole
        # definition put them on every card twice and cut it mid-word at the length cap.
        head = defn.split(" that gives ")[0]
        if head == defn and len(defn) > 140:  # a future definition without the clause still fits
            head = defn[:140].rsplit(" ", 1)[0]
        return head.rstrip(".") + "."
    cards = "".join(
        f'<a class="pcard" href="/agents/{slug}"><h3>{_esc_html(spec["name"])}</h3>'
        f'<p>{_esc_html(_blurb(spec["definition"].format(n=n, p=p)))}</p>'
        f'<div class="meta">{n} tools &middot; {p} platforms</div></a>'
        for slug, spec in agent_pages.AGENTS.items())
    body = (
        '<main class="wrap"><div class="phead">'
        '<div class="crumbs"><a href="/">treg.to</a> / <a href="/agents">Agents</a></div>'
        '<h1>The agents that can use treg.to</h1>'
        '<p class="lede">One page per client: the install steps for that agent, then the menu of '
        f'jobs it can do once connected. Every client gets the same {n} tools through one treg.to '
        'key, at the provider&rsquo;s own rate with $0.000 markup.</p>'
        f'</div><section class="cat"><div class="grid">{cards}</div></section>'
        '<section class="cat"><h2>Everything else</h2><div class="cap"><p style="margin:0">The jobs '
        'themselves are written up at <a href="/use-cases">/use-cases</a>, the multi-step versions '
        'at <a href="/workflows">/workflows</a>, and the whole catalog is at '
        '<a href="/catalog">/catalog</a>.</p></div></section></main>')
    names = [s["name"] for s in agent_pages.AGENTS.values()]
    ld = [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
        {"@type": "ListItem", "position": 2, "name": "Agents", "item": base + "/agents"}]}]
    return _page("Install treg.to in ChatGPT, Claude, Cursor or Grok",
                 f"Install steps for {', '.join(names[:-1])} and {names[-1]}, and the menu of jobs "
                 "each can do once connected. One treg.to key, no markup.",
                 "/agents", body, ld)


@app.get("/agents/{agent}.md", include_in_schema=False)
@app.get("/agents/{agent}", include_in_schema=False)
async def agent_page(request: Request, agent: str):
    """One client: "I use ChatGPT, what can it do now?" A keyword H1 with a rotating "for <role>"
    line under it, the install steps for that client, then the use-case menu: plain-words jobs under buyer
    categories, each priced from the catalog. The menu is `agent_pages.USE_CASES`, the same
    taxonomy the use-case pages hang from, so the agent page is the map of the whole site.
    `/agents/<agent>.md` is the same page as Markdown, for agents and answer engines."""
    as_md = request.url.path.endswith(".md")
    raw = agent[:-3] if agent.endswith(".md") else agent
    # Resolve to the dict's OWN key, never the request's bytes: `agent` is interpolated into the
    # canonical, the rel=alternate href and the JSON-LD breadcrumb below, and a path parameter
    # must not reach those unescaped (CodeQL py/reflective-xss). The lookup is case-insensitive,
    # so a differently-cased URL would otherwise serve a 200 whose canonical points at itself: a
    # duplicate page. Send it to the one spelling instead.
    agent = next((k for k in agent_pages.AGENTS if k == raw.lower()), None)
    if agent is None or not _hosted():
        raise HTTPException(status_code=404, detail="unknown agent")
    if raw != agent:
        return RedirectResponse(f"/agents/{agent}" + (".md" if as_md else ""), status_code=301)
    spec = agent_pages.AGENTS[agent]
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    n_eps, n_plats = _catalog_census()
    n, p = f"{n_eps:,}", str(n_plats)
    name = spec["name"]
    title = spec["title"].format(n=n, p=p)
    desc = _serp_desc(spec["description"].format(n=n, p=p))
    definition = spec["definition"].format(n=n, p=p)
    menu = [(category, agent_pages.CATEGORY_PROMPTS.get(category, ""), _menu_rows(cat, category, jobs))
            for category, jobs in agent_pages.USE_CASES]
    steps_text = [re.sub(r"<[^>]+>", "", st).format(n=n) for st in spec["install_steps"]]

    if as_md:
        md = [f"# {title}", "", definition, "", f"## Install in {name}", ""]
        md += [f"{i}. {html_mod.unescape(st)}" for i, st in enumerate(steps_text, 1)]
        if agent_pages.WORKFLOWS:
            md += ["", f"## Sequences {name} can run from one prompt", ""]
            md += [f"- [{wspec['sentence']}]({base}/workflows/{ws}), {len(wspec.get('steps', ()))} steps, "
                   f"each priced, with the receipt from a real run"
                   for ws, wspec in agent_pages.WORKFLOWS.items()]
        md += ["", f"## What {name} can do now", "",
               "One row per job. Prices are the provider's own rate with $0.000 markup; rows marked FREE run on your own account and are never metered.", ""]
        for category, prompt, rows in menu:
            md += [f"### {category}", ""]
            if prompt:
                md += [f"Try: \"{prompt}\"", ""]
            groups = agent_pages.CATEGORY_GROUPS.get(category)
            order = ([l for _g, labels in groups for l in labels] if groups else None)
            if order:
                rows = sorted(rows, key=lambda r: order.index(r["label"]) if r["label"] in order else 999)
            for r in rows:
                plats = ", ".join(pl["label"] for pl in r["platforms"] if not pl["dup"])
                price = "FREE with your own account" if r["own_account"] else f"from {_usd_short(r['from_usd'])}"
                link = f"{base}{r['page']}" if r["page"] else f"{base}/catalog/{r['platforms'][0]['slug']}"
                md.append(f"- [{r['label']}]({link}): {plats}. {r['providers']} provider{'s' if r['providers'] != 1 else ''}, {price}.")
            md.append("")
        md += ["## Questions", ""]
        for q, a in spec["faq"]:
            md += [f"**{q}** {a}", ""]
        md += [f"HTML version: {base}/agents/{agent}", f"Setup line for any agent: {agent_pages.SETUP_LINE.format(base=base)}"]
        return PlainTextResponse("\n".join(md), media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=600"})

    # Only the FIRST role is server-rendered, on the roleline under the H1 — the H1 itself carries
    # the term and the promise, never a persona. The rest ride in a JSON block for the script.
    roles = f'<span class="ri on">{_esc_html(agent_pages.ROLES[0])}</span>'
    more_roles = json.dumps(list(agent_pages.ROLES[1:])).replace("<", "\\u003c")
    steps = "".join(
        f'<div class="steplabel"><span class="n">{i}</span><b>{st.format(n=n)}</b></div>'
        for i, st in enumerate(spec["install_steps"], 1))
    shot = (f'<div class="sample"><div class="sbar">{_esc_html(spec.get("install_image_bar") or name)}</div>'
            f'<img src="{_esc_html(spec["install_image"])}" alt="{_esc_html(spec["install_image_alt"])}" '
            f'loading="lazy" style="display:block;width:100%"/>'
            + (f'<div class="sbar" style="border-top:1px solid var(--line);border-bottom:0">'
               f'{_esc_html(spec["install_image_caption"])}</div>' if spec.get("install_image_caption") else "")
            + '</div>' if spec.get("install_image") else "")

    # the platform marks in the hero: the busiest shelves, deduped by brand
    hero_tiles, seen_brand = [], set()
    for _cat_name, _prompt, rows in menu:
        for r in rows:
            for pl in r["platforms"]:
                root = ".".join((pl["domain"] or "").split(".")[-2:])
                if pl["domain"] and root not in seen_brand and len(hero_tiles) < 14:
                    seen_brand.add(root)
                    hero_tiles.append(f'<span class="ptile" title="{_esc_html(pl["label"])}">'
                                      f'{_logo(pl["domain"], pl["label"])}</span>')

    cards, sections = [], []
    for category, prompt, rows in menu:
        anchor = _anchor(category)
        priced = [r["from_usd"] for r in rows if r["from_usd"]]
        free_all = all(r["own_account"] for r in rows)
        meta = (f'{len(rows)} jobs &middot; <b style="color:var(--green)">free</b> on your account' if free_all
                else f'{len(rows)} jobs &middot; from {_esc_html(_usd_short(min(priced)))}' if priced
                else f"{len(rows)} jobs")
        blurb = agent_pages.CATEGORY_BLURBS.get(category, "").format(agent=name)
        cards.append(f'<a class="card" href="#{anchor}"><h4>{_esc_html(category)}</h4>'
                     f'<p>{_esc_html(blurb)}</p>'
                     f'<p style="font-family:var(--mono);font-size:11.5px;color:var(--muted2)">{meta}</p></a>')
        body_rows, by_label = [], {}
        for r in rows:
            chips, seen_p = [], set()
            for pl in r["platforms"]:
                if pl["slug"] in seen_p:
                    body_rows.append("")  # keep data-cap discoverable below
                    continue
                seen_p.add(pl["slug"])
                chips.append(f'<a href="/catalog/{_esc_html(pl["slug"])}#{_esc_html(pl["cap"])}" '
                             f'data-cap="{_esc_html(pl["cap"])}">{_logo(pl["domain"], pl["label"])}{_esc_html(pl["label"])}</a>')
            hidden = "".join(f'<span data-cap="{_esc_html(pl["cap"])}" hidden></span>'
                             for pl in r["platforms"] if pl["dup"])
            price = ('<span style="color:var(--green)">free, your account</span>' if r["own_account"]
                     else f'{_esc_html(_usd_short(r["from_usd"]))}')
            name_cell = (f'<a href="{r["page"]}"><b>{_esc_html(r["label"])}</b></a>' if r["page"]
                         else f'<b>{_esc_html(r["label"])}</b>')
            row_html = (f'<tr><td>{name_cell}{hidden}</td>'
                        f'<td style="color:var(--muted)">{" &middot; ".join(chips)}</td>'
                        f'<td>{r["providers"]}</td><td>{price}</td></tr>')
            by_label[r["label"]] = row_html
            body_rows.append(row_html)
        # A category with sub-headings orders its rows by group and prints a divider row before
        # each. Enrichment is 25 jobs and the groups are how a buyer reads them; they are NOT
        # categories, because find / contacts / enrich are stages of one motion and would have
        # committed four more URL segments to a distinction that only exists in a practitioner's head.
        groups = agent_pages.CATEGORY_GROUPS.get(category)
        if groups:
            body_rows = []
            for gname, labels in groups:
                body_rows.append(
                    f'<tr><td colspan="4" style="padding-top:20px"><span class="seclab" '
                    f'style="margin:0">{_esc_html(gname)}</span></td></tr>')
                body_rows += [by_label[l] for l in labels if l in by_label]
        sections.append(
            f'<section id="{anchor}"><div class="wrap"><div class="seclab">{_esc_html(category)}</div>'
            f'<h2>{_esc_html(blurb)}</h2>'
            + (f'<p>Try: <i>&ldquo;{_esc_html(prompt)}&rdquo;</i></p>' if prompt else "")
            + '<div class="tablewrap"><table><thead><tr><th>Job</th><th>Where</th><th>Providers</th>'
              '<th>From</th></tr></thead><tbody>'
            + "".join(body_rows) + '</tbody></table></div></div></section>')

    faq_html = "".join(f'<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>' for q, a in spec["faq"])

    body = (
        '<div class="hero"><div class="wrap">'
        f'<div class="trust" style="margin:0 0 18px"><a href="/">treg.to</a> / '
        f'<a href="/agents/{_esc_html(agent)}">{_esc_html(name)}</a></div>'
        f'<div class="kicker">{n} endpoints &middot; {p} platforms &middot; $0.000 markup</div>'
        # The H1 carries the measured term and the promise, never a persona — a crawler was reading
        # "The ChatGPT Connector for SEO experts" as if that were the audience. The rotating role
        # wheel stays, one line down.
        f'<h1>The {_esc_html(name)} {_esc_html(spec.get("h1_noun", "plugin"))}: '
        f'call {n} APIs without keys</h1>'
        f'<div class="roleline">for <span class="roleslot" id="roleslot">'
        f'<span class="rw" id="rolewheel">{roles}</span></span></div>'
        f'<script type="application/json" id="roles-more">{more_roles}</script>'
        f'<div class="lede">{_esc_html(definition)}</div>'
        '<div class="ctas">'
        f'<a class="candy" href="/app?ref=agents-{_esc_html(agent)}">Start free</a>'
        '<a class="ghostbtn" href="#use-cases">See what it can do</a></div>'
        '<div class="trust">$1.00 of free credit on every new team &middot; no provider signup &middot; no card</div>'
        f'<div class="subline">Your own keys always win and are never metered. '
        f'{_esc_html(name)} sees the price before it spends.</div>'
        + (f'<div class="provstrip"><div class="pl">a few of the {p} platforms</div>'
           f'<div class="ptiles">{"".join(hero_tiles)}</div></div>' if hero_tiles else "")
        + '</div></div>'

        f'<section id="install"><div class="wrap"><div class="seclab">Get started</div>'
        f'<h2>Install in {_esc_html(name)}</h2>{steps}{shot}</div></section>'

        '<section id="use-cases"><div class="wrap"><div class="seclab">The menu</div>'
        f'<h2>What {_esc_html(name)} can do now</h2>'
        '<p>By job, not by endpoint. The price is the lowest provider&rsquo;s own rate with $0.000 added by '
        'treg.to; <b>free</b> means the job runs on an account you already own and is never metered. Where '
        f'several providers do one job, {_esc_html(name)} sees them side by side and choosing is yours.</p>'
        f'<div class="cards">{"".join(cards)}</div>'
        f'<p style="margin-top:20px"><a href="/catalog">Browse all {n} endpoints &rarr;</a> &middot; '
        f'<a href="/use-cases">read the job guides &rarr;</a></p></div></section>'

        # The workflows: several jobs from the menu chained into one prompt, each with a receipt from
        # a real run. Linked from every agent page so the page that describes what an agent can do
        # also names the sequences it can run; before this the workflow pages were reachable from
        # the hub alone.
        + (f'<section id="workflows"><div class="wrap"><div class="seclab">Workflows</div>'
           f'<h2>Sequences {_esc_html(name)} can run from one prompt</h2>'
           '<p>Several jobs from the menu, chained. Each step is priced from the catalog and the page '
           'prints the receipt from a real run, not a rate card.</p><div class="cards">'
           + "".join(f'<a class="card" href="/workflows/{_esc_html(ws)}"><h3>{_esc_html(wspec["sentence"])}</h3>'
                     f'<p>{len(wspec.get("steps", ()))} steps, each a metered call, with the price shown before '
                     f'{_esc_html(name)} spends it.</p></a>'
                     for ws, wspec in agent_pages.WORKFLOWS.items())
           + f'</div><p style="margin-top:20px"><a href="/workflows">All workflows &rarr;</a></p></div></section>'
           if agent_pages.WORKFLOWS else "")

        + "".join(sections)

        + f'<section id="faq"><div class="wrap"><div class="seclab">Questions</div>'
          f'<h2>Before you install</h2>{faq_html}</div></section>'

        + '<div class="final"><div class="wrap">'
          f'<h2>Give {_esc_html(name)} the tools</h2>'
          f'<a class="candy" href="/app?ref=agents-{_esc_html(agent)}-final">Start free</a>'
          '<div class="trust">$1.00 of calls free per new team &middot; '
          '<a href="/catalog">browse the catalog</a></div></div></div>'

        + """
<style>
.hero h1{line-height:1.16}
.roleline{font-size:21px;font-weight:600;letter-spacing:-.01em;margin:8px 0 2px}
.roleslot{display:inline-block;height:1.16em;overflow:hidden;vertical-align:bottom;position:relative}
.roleslot .rw{display:flex;flex-direction:column;align-items:flex-start;transition:transform .62s cubic-bezier(.2,.7,.2,1)}
.roleslot .ri{height:1.16em;line-height:1.16;flex:none;white-space:nowrap;transition:opacity .4s}
.roleslot .ri:not(.on){opacity:.25}
@media (prefers-reduced-motion:reduce){.roleslot .rw{transition:none}}
</style>
<script>
(function(){
  var w=document.getElementById('rolewheel'); if(!w) return;
  try { JSON.parse((document.getElementById('roles-more')||{}).textContent||'[]').forEach(function(r){
    var s=document.createElement('span'); s.className='ri'; s.textContent=r; w.appendChild(s); }); } catch(e) {}
  var items=w.children, i=0, slot=document.getElementById('roleslot');
  if(matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  function fit(){ slot.style.width=items[i].getBoundingClientRect().width+'px'; }
  fit(); addEventListener('resize', fit);
  setInterval(function(){
    if(scrollY>innerHeight*.8) return;
    i=(i+1)%items.length; w.style.transform='translateY(-'+(i*1.16)+'em)';
    for(var k=0;k<items.length;k++) items[k].classList.toggle('on',k===i);
    fit();
  },3000);
})();
</script>""")

    ld = [
        {"@context": "https://schema.org", "@type": "SoftwareApplication", "name": "treg.to",
         "applicationCategory": "DeveloperApplication", "operatingSystem": "Web",
         "url": base + "/", "description": desc,
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD",
                    "description": "Free to install. Calls are metered per call from a prepaid balance at the "
                                   "provider's own rate with no markup; every new team starts with $1.00 free."}},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Agents", "item": base + "/agents"},
            {"@type": "ListItem", "position": 3, "name": name, "item": f"{base}/agents/{agent}"}]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in spec["faq"]]},
    ]
    return _page(title, desc[:300], f"/agents/{agent}", body, ld,
                 head_extra=_MD_ALT.format(href=f"{base}/agents/{agent}.md"), css="usecase.css")


def _uc_agent() -> tuple[str, str]:
    """(slug, display name) of the client the use-case pages use as the example."""
    slug = agent_pages.DEFAULT_AGENT
    return slug, agent_pages.AGENTS[slug]["name"]


_UNIT_WORDS = {"per_success": "found", "per_call": "call", "per_result": "result"}


def _uc_providers(cat, eps: list[dict], obs: dict) -> list[dict]:
    """One row per provider for this job: cheapest priced endpoint, best-sampled observed stats,
    the union of its accepted inputs, and the platform it serves."""
    # Routed endpoints (kind:routed) are the first-party meta-rows that delegate to children; they
    # appear as provider "treg" and must not show as a row in comparison tables. Filter them here
    # rather than at each call site, since every use-case comparison needs the same exclusion.
    eps = [e for e in eps if e.get("kind") != "routed"]

    def usd(e):
        cv = cat.cost_view(e.get("cost"), e.get("provider"))
        return cv["usd"] if cv and cv["usd"] else None

    # Keyed by (provider, platform), not provider alone: one provider often serves several
    # platforms for the same job (ScrapeCreators does Instagram AND YouTube), and collapsing those
    # into one row silently drops a whole platform from a multi-platform page.
    out = []
    for prov, plat in sorted({(e["provider"], e["platform"]) for e in eps}):
        peps = [e for e in eps if e["provider"] == prov and e["platform"] == plat]
        priced = sorted([(usd(e), e) for e in peps if usd(e)], key=lambda t: t[0])
        cheapest_e = priced[0][1] if priced else None
        stats = [(obs.get(e["id"]) or {}) for e in peps]
        best = max((st for st in stats if st.get("samples")), key=lambda st: st["samples"], default=None)
        ins = []
        for e in peps:
            inp = e.get("input") or {}
            for section in ("queryParams", "pathParams", "body", "headers"):
                for k, v in (inp.get(section) or {}).items():
                    if isinstance(v, dict) and k not in ins:
                        ins.append(k)
        slug = plat
        # "no dollar price" and "free" are different facts and were rendering as one. Semrush
        # prices this job in pre-bought API units, so its cost_view has no USD, and the price cell
        # read "free, your own account" for the dearest option on the page.
        free = any((e.get("cost") or {}).get("type") == "free" for e in peps)
        # A $0 trial row is a third fact again: served on treg.to's own free-tier key with a daily
        # allowance per team (catalog.trial_pools), so neither "free, your own account" nor a price
        # is true of it. Carry the allowance so the cell can state it.
        # `default=0`: a row with no cost at all (TikHub's LinkedIn comments v2 carries none)
        # has no cost_view, and an empty max() took the whole page down with a 500.
        trial = max(((cv.get("trial_calls_per_team_day") or 0) for e in peps
                     if (cv := cat.cost_view(e.get("cost"), e.get("provider")))), default=0)
        out.append({
            "id": prov, "name": _provider_display(prov), "eps": peps, "free": free, "trial": trial,
            "domain": agent_pages.PROVIDER_DOMAINS.get(prov),
            "platform": slug, "platform_label": (cat.platforms.get(slug) or {}).get("label") or slug,
            "usd": priced[0][0] if priced else None,
            "unit": _UNIT_WORDS.get((cheapest_e.get("cost") or {}).get("type"), "call") if cheapest_e else "",
            "cheapest_ep": cheapest_e, "inputs": ins[:6],
            "verified": max((e.get("verified") or "" for e in peps), default=""),
            "ok_rate": best.get("ok_rate") if best else None,
            "p50": best.get("p50_ms") if best else None,
            "samples": best.get("samples") if best else 0,
        })
    return out


def _uc_call(e: dict) -> str:
    tr = e.get("test_request") or {}
    q = " ".join(f"--query {k}={v}" for k, v in (tr.get("queryParams") or {}).items())
    parts = [f"treg call {e['id']}"]
    if q:
        parts.append(q)
    if tr.get("body"):
        parts.append("--data '" + json.dumps(tr["body"], separators=(",", ":")) + "'")
    return " ".join(parts)


@app.get("/use-cases/{category}/{job}.md", include_in_schema=False)
@app.get("/use-cases/{category}/{job}", include_in_schema=False)
async def use_case_job_page_nested(category: str, job: str):
    """The URLs the first pages shipped under, when the category was a path segment. They are live
    and indexed, so they 301 to the flat form rather than 404. Keep this forever: a moved URL that
    answers is free, and a moved URL that does not is the whole cost of a taxonomy change."""
    md = job.endswith(".md")
    slug = job[:-3] if md else job
    # A renamed slug must keep redirecting from its nested shape too — this handler used to
    # reject it before consulting the map, which turned the promised 301 into a 404.
    slug = agent_pages.USE_CASE_REDIRECTS.get(slug.lower(), slug)
    if slug not in agent_pages.USE_CASE_PAGES:
        raise HTTPException(status_code=404, detail="unknown use case")
    return RedirectResponse(f"/use-cases/{slug}{'.md' if md else ''}", status_code=301)


@app.get("/use-cases/{job}.md", include_in_schema=False)
@app.get("/use-cases/{job}", include_in_schema=False)
async def use_case_job_page(request: Request, job: str,
                            observations: endpoint_stats.EndpointObservationReader = Depends(
                                _endpoint_observation_reader)):
    """One job. The reader does one thing, the prompt; everything else is what the agent sees
    before it calls. The page takes one of three FORMS, chosen from the data rather than by hand:

      short      one provider, so there is nothing to compare (all of "connect your own accounts")
      platforms  the job spans several platforms, which are not alternatives to one another
      compare    several providers doing one job on one platform: the full comparison

    Everything job-specific comes from `agent_pages.USE_CASE_PAGES`; the example client comes from
    `DEFAULT_AGENT`, so writing page two is data entry. `.md` serves the same page as Markdown.
    """
    as_md = request.url.path.endswith(".md")
    raw = job[:-3] if job.endswith(".md") else job
    # The five ad landing pages are static HTML on this same path shape. One handler owns the path,
    # so serve them before anything else; `_USE_CASES` stays the one source for which they are.
    legacy = _USE_CASES.get(raw.strip("/").lower())
    if legacy and not as_md:
        page = _WEB_DIR / legacy
        if not page.exists():
            raise HTTPException(status_code=404, detail=f"{legacy} not bundled")
        # Read-and-substitute rather than a bare FileResponse: {BASE} is templated for the
        # canonical/og:url so each page names the serving host, not hardcoded treg.to.
        base = get_settings().public_url.rstrip("/")
        content = page.read_text(encoding="utf-8").replace("{BASE}", base)
        # no-cache: these are edited against live campaign data and must never serve stale.
        return HTMLResponse(content, headers={"Cache-Control": "no-cache"})
    # Possessive slugs that shipped before GSC indexing: redirect to the clean slug with 301 so
    # the canonical stays clean and search engines update before indexing the old URL.
    redirect_to = agent_pages.USE_CASE_REDIRECTS.get(raw.lower())
    if redirect_to:
        return RedirectResponse(f"/use-cases/{redirect_to}" + (".md" if as_md else ""), status_code=301)
    # Same rule as `agent_page`: the slug reaches the canonical and the JSON-LD, so it comes from
    # the table's own key, and a differently-cased URL is redirected rather than duplicated.
    key = next((k for k in agent_pages.USE_CASE_PAGES if k == raw.lower()), None)
    if key is None or not _hosted():
        raise HTTPException(status_code=404, detail="unknown use case")
    if raw != key:
        return RedirectResponse(f"/use-cases/{key}" + (".md" if as_md else ""), status_code=301)
    # Fresh name on purpose: rebinding the parameter itself does not read as a taint kill to
    # CodeQL, and the request's spelling must not be what the page prints.
    job_slug = key
    spec = agent_pages.USE_CASE_PAGES[key]
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    agent_slug, agent_name = _uc_agent()
    cat_label = _job_category(spec["label"])
    caps = _use_case_caps(spec["label"])
    eps = [e for cid in caps for e in cat.for_capability(cid) if _pub(e)]
    if not eps:
        raise HTTPException(status_code=404, detail="no endpoints for this job")
    job_workflows = _workflows_for_caps(caps)
    obs = await _observed_or_empty(observations, [e["id"] for e in eps])
    provs = _uc_providers(cat, eps, obs)

    def usd_of(e):
        cv = cat.cost_view(e.get("cost"), e.get("provider"))
        return cv["usd"] if cv and cv["usd"] else None

    platforms = sorted({p["platform_label"] for p in provs})
    form = "short" if len(provs) == 1 else ("platforms" if len(platforms) > 1 else "compare")
    # data-provider must stay unique in the DOM when one provider appears under two platforms
    for pr in provs:
        pr["row_id"] = pr["id"] if form != "platforms" else f'{pr["id"]}-{pr["platform"]}'
    noun = spec.get("result_noun", "result")

    # Cheapest is claimed PER BILLING UNIT. A per-call endpoint that returns a thousand rows is not
    # dearer than a per-result one, and ranking them together names the wrong winner: 38 of the 66
    # jobs on the menu mix units.
    cheapest_by_unit: dict[str, dict] = {}
    for pr in provs:
        u = pr["unit"]
        if pr["usd"] and (u not in cheapest_by_unit or pr["usd"] < cheapest_by_unit[u]["usd"]):
            cheapest_by_unit[u] = pr
    units = list(cheapest_by_unit)
    headline = cheapest_by_unit[units[0]] if units else None
    reliable = sorted([p for p in provs if p["samples"] and p["ok_rate"] is not None],
                      key=lambda p: (-p["ok_rate"], p["p50"] or 9e9, -p["samples"]))
    # No priced row is two different facts: own-account rows are free on the reader's key; trial
    # rows are free on treg.to's key up to a daily allowance. Say whichever is true.
    trial_max = max((p["trial"] for p in provs), default=0)
    free_words = (f"free, up to {trial_max} calls a day on treg.to's key" if trial_max
                  else "free on your own account")
    n = str(len({p["id"] for p in provs}))
    n_ver = sum(1 for e in eps if e.get("verified"))
    latest_verified = max((e.get("verified") or "" for e in eps), default="")
    setup = agent_pages.SETUP_LINE.format(base=base)

    def money(x):
        return _usd_short(x)

    def pct(x):
        return f"{round(x * 100)}%" if x is not None else ""

    def ms(x):
        return (f"{x/1000:.1f}s" if x >= 1000 else f"{int(x)}ms") if x else ""

    def unit_plural(u: str) -> str:
        return {"found": f"{noun}s found", "result": "results"}.get(u, "calls")

    title = spec.get("title", "{sentence}: {n} providers | treg.to").format(
        sentence=spec["sentence"], n=n, agent=agent_name,
        cheapest=money(headline["usd"]) if headline else free_words)
    # The number goes in the title. Search Console shows the pricing phrasing is what reaches this
    # site, and a title that already names the cheapest price is the one fact a vendor's own page
    # cannot carry. Only the compare form has a price to name; a hand-written title that already
    # carries one is left alone.
    if form == "compare" and headline and "$" not in title and title.endswith(" | treg.to"):
        head = title[: -len(" | treg.to")]
        if head.endswith(" compared"):  # "5 verifiers compared" -> "5 verifiers, from $0.0019"
            head = head[: -len(" compared")]
        priced = f"{head}, from {money(headline['usd'])} | treg.to"
        if len(priced) <= _TITLE_MAX:
            title = priced
    lede = spec["lede"].format(n=n, agent=agent_name,
                               cheapest=money(headline["usd"]) if headline else free_words)
    bits_desc = [spec["sentence"] + "."]
    # A single provider is one of three facts, not two: an own-account connection (free), a metered
    # row on treg.to's key, or a trial pool (no USD, a daily allowance on treg.to's free-tier key,
    # then the reader's own key). The trial case fell into `metered_single` and claimed a bill.
    trial_single = form == "short" and not provs[0]["free"] and bool(provs[0]["trial"])
    metered_single = form == "short" and not provs[0]["free"] and not trial_single
    if trial_single:
        bits_desc.append(f"One provider, free for {provs[0]['trial']} calls a day on treg.to's key, then your own key.")
    elif metered_single:
        bits_desc.append(f"One provider, {money(headline['usd'])} per {headline['unit']} on treg.to's key, no signup."
                         if headline else "One provider, served on treg.to's key.")
    elif form == "short":
        bits_desc.append("Runs on the account you already own, so treg.to never meters it.")
    elif headline:
        bits_desc.append(f"{n} providers compared, cheapest {money(headline['usd'])} per {headline['unit']}.")
    bits_desc.append(f"The prompt that works in {agent_name}, with the price shown before the call.")
    desc = _serp_desc(" ".join(bits_desc))

    if as_md:
        md = [f"# {spec['sentence']}", "", lede, "",
              f"## What's the best way to ask {agent_name}?", "",
              f"Setup line (paste into any agent): `{setup}`", "",
              f'Then ask: "{spec["prompt"]}"', ""]
        md += [f"- **{t}** {d}" for t, d in spec["prompt_why"]]
        # Any page with a free own-account row (GA4, Search Console short pages, and the YouTube
        # comparisons where the official Data API is a $0.00 row) drops the metering cards: "9
        # accounts" and "Hunter" are false wherever the reader's own account does the job.
        own_key_free = any(p["free"] for p in provs)
        why_treg = agent_pages.WHY_TREG_OWN_KEY if own_key_free else agent_pages.WHY_TREG
        md += ["", "## Why go through treg.to", ""] + [f"- **{t}** {d}" for t, d in why_treg]
        if trial_single:
            e0 = provs[0]["eps"][0]
            md += ["", "## How it works", "",
                   f"One provider does this job: {provs[0]['name']} (`{e0['id']}`), free for {provs[0]['trial']} "
                   "calls a day per team on treg.to's own free-tier key. Past the allowance the call is refused "
                   "with a hint to connect your own key, and on your own key it is never metered.",
                   "", f"    {_uc_call(e0)}", ""]
        elif metered_single:
            e0 = provs[0]["cheapest_ep"] or provs[0]["eps"][0]
            md += ["", "## How it works", "",
                   f"One provider does this job: {provs[0]['name']} (`{e0['id']}`), served on treg.to's own key at "
                   + (f"{money(provs[0]['usd'])} per {provs[0]['unit']}, " if provs[0]["usd"] else "")
                   + "the provider's own rate with $0.000 markup, metered from your team's balance. "
                   "No account with the provider, and no key of your own, unless you would rather bring one.",
                   "", f"    {_uc_call(e0)}", ""]
        elif form == "short":
            e0 = provs[0]["eps"][0]
            md += ["", "## How it works", "",
                   f"One provider does this job: {provs[0]['name']} (`{e0['id']}`), on the account you already own. "
                   "You connect it once, treg.to keeps the token server side, and the call is never metered.",
                   "", f"    {_uc_call(e0)}", ""]
        else:
            md += ["", f"## Behind the scenes: what {agent_name} sees before it calls", "",
                   f"treg.to does not choose for you. It hands {agent_name} this comparison and it picks, "
                   "or you tell it how.", ""]
            if units:
                md += [f"### {spec.get('q_cheapest', 'Which is cheapest?')}", ""]
                for u in units:
                    pu = cheapest_by_unit[u]
                    md.append(f"- Cheapest per {u}: {pu['name']} at {money(pu['usd'])} (`{pu['cheapest_ep']['id']}`)")
                if len(units) > 1:
                    md += ["", "Those units are not interchangeable: one call can return many results, "
                               "so compare on the unit you will actually be billed in."]
            if reliable:
                md += ["", f"### {spec.get('q_reliable', 'Which is the most reliable?')}", ""]
                md += [f"- {p['name']}: {pct(p['ok_rate'])} over {p['samples']} calls, {ms(p['p50'])} median"
                       for p in reliable[:6]]
                md += ["", "Measured on treg.to traffic; not a controlled benchmark."]
            md += ["", f"### {spec.get('q_compare', 'How do they compare?')}", ""]
            for plat in (platforms if form == "platforms" else [None]):
                rows_ = [p for p in provs if plat is None or p["platform_label"] == plat]
                if plat:
                    md += [f"#### {plat}", ""]
                md += ["| Provider | Price | Accepts | Verified |", "|---|---|---|---|"]
                for p in sorted(rows_, key=lambda p: (p["usd"] is None, p["usd"] or 0)):
                    price = (f"{money(p['usd'])} per {p['unit']}" if p["usd"]
                             else (f"free, {p['trial']} calls a day on treg.to's key, then your own key" if p["trial"]
                                   else ("own account, free" if p["free"] else "no dollar rate published")))
                    md.append(f"| {p['name']} | {price} | {', '.join(p['inputs'])} | {p['verified'] or 'unverified'} |")
                md.append("")
        md += ["Endpoints:", ""] + [f"- `{e['id']}`: {_uc_call(e)}" for e in eps]
        if spec.get("voices"):
            md += ["", "## What people actually struggle with", "", spec["voices_intro"], ""]
            for head, quote, who, url, answer in spec["voices"]:
                md += [f"**{head}**", "", f'> "{quote}" ({who}: {url})', "",
                       f"What this page can do about it: {answer}", ""]
        md += ["", "## What actually differs", ""] + [f"- {x}" for x in spec["notes"]]
        md += ["", f"## {spec.get('what_is_heading', 'What is this?')}", "", spec["what_is"], "", "## Questions", ""]
        for q, a in spec["faq"]:
            md += [f"**{q}** {a}", ""]
        md += [f"HTML version: {base}/use-cases/{job_slug}"]
        return PlainTextResponse("\n".join(md), media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=600"})

    # ---------------------------------------------------------------- html (landing-page skin)
    ptiles = "".join(
        f'<span class="ptile" title="{_esc_html(p["name"])}">{_logo(p["domain"], p["name"])}</span>'
        for p in provs[:12] if p["domain"])
    provstrip = (f'<div class="provstrip"><div class="pl">compared on this page</div>'
                 f'<div class="ptiles">{ptiles}</div></div>' if ptiles else "")
    agent_icons = "".join(
        f'<span class="ptile" title="{_esc_html(label)}">'
        f'<img src="https://unpkg.com/@lobehub/icons-static-png@latest/light/{icon}.png" alt="{_esc_html(label)}" loading="lazy"/></span>'
        for aid, label, icon in agent_pages.AGENT_ICONS[:6])
    hero_price = (f"from {_esc_html(money(headline['usd']))} per {headline['unit']}"
                  if headline else _esc_html(free_words if trial_max else "free on the account you already own"))

    def promptbox(label: str, text: str) -> str:
        return ('<div class="promptbox"><div class="ph">'
                f'<span>{_esc_html(label)}</span>'
                f'<button class="copybtn" data-copy="{_esc_html(text)}">copy</button></div>'
                f'<pre>{_esc_html(text)}</pre></div>')

    why_cards = "".join(f'<div class="card"><h4>{_esc_html(t)}</h4><p>{_esc_html(d)}</p></div>'
                        for t, d in spec["prompt_why"])
    # Any page with a free own-account row (GA4, Search Console short pages, and the YouTube
    # comparisons where the official Data API is a $0.00 row) drops the metering cards: "9
    # accounts" and "Hunter" are false wherever the reader's own account does the job.
    own_key_free = any(p["free"] for p in provs)
    why_treg = agent_pages.WHY_TREG_OWN_KEY if own_key_free else agent_pages.WHY_TREG
    treg_cards = "".join(f'<div class="card"><h4>{_esc_html(t)}</h4><p>{_esc_html(d)}</p></div>'
                         for t, d in why_treg)

    def price_cell(p: dict) -> str:
        if p["usd"]:
            return f'{_esc_html(money(p["usd"]))} <span style="color:var(--muted2)">per {p["unit"]}</span>'
        if p["trial"]:
            return (f'<span style="color:var(--green)">free, {p["trial"]} calls a day</span> '
                    '<span style="color:var(--muted2)">on treg.to\'s key, then your own key</span>')
        if p["free"]:
            return '<span style="color:var(--green)">free, your own account</span>'
        return '<span style="color:var(--muted2)">no dollar rate published</span>'

    def rel_cell(p: dict) -> str:
        return (f'{pct(p["ok_rate"])} <span style="color:var(--muted2)">({p["samples"]} calls)</span>'
                if p["samples"] else '<span style="color:var(--muted2)">not yet measured</span>')

    def prov_table(rows_: list[dict]) -> str:
        body_rows = "".join(
            f'<tr data-provider="{_esc_html(p["id"])}">'
            f'<td><b>{_logo(p["domain"], p["name"])}{_esc_html(p["name"])}</b></td>'
            f'<td>{price_cell(p)}</td>'
            f'<td style="color:var(--muted)">{_esc_html(", ".join(p["inputs"]) or "see endpoints")}</td>'
            f'<td>{rel_cell(p)}</td>'
            f'<td style="color:var(--muted2)">{_esc_html(p["verified"] or "unverified")}</td>'
            '</tr>' for p in rows_)
        return ('<div class="tablewrap"><table><thead><tr>'
                '<th>Provider</th><th>Price</th><th>Accepts</th><th>Success rate</th><th>Verified</th>'
                f'</tr></thead><tbody>{body_rows}</tbody></table></div>')

    sections = []
    if trial_single:
        p0, e0 = provs[0], provs[0]["eps"][0]
        sections.append(
            '<section id="how"><div class="wrap"><div class="seclab">How it works</div>'
            f'<h2>One provider, free for {p0["trial"]} calls a day on treg.to\'s key</h2>'
            f'<p>{_logo(p0["domain"], p0["name"])}<b>{_esc_html(p0["name"])}</b> answers this job, served on treg.to\'s '
            f'own free-tier key with an allowance of {p0["trial"]} calls a day per team. Past the allowance the call is '
            'refused with a hint to connect your own key, and on your own key it is never metered.</p>'
            f'<div class="sample"><div class="sbar">the call</div><pre>{_esc_html(_uc_call(e0))}</pre></div>'
            f'<p style="font-size:12.5px;color:var(--muted)">Every endpoint on this shelf is listed on the '
            f'<a href="/catalog/{_esc_html(e0["platform"])}">{_esc_html((cat.platforms.get(e0["platform"]) or {}).get("label") or e0["platform"])} shelf</a>.</p>'
            + '</div></section>')
    elif metered_single:
        p0 = provs[0]
        e0 = p0["cheapest_ep"] or p0["eps"][0]
        rate = f'{_esc_html(money(p0["usd"]))} per {p0["unit"]}, ' if p0["usd"] else ""
        sections.append(
            '<section id="how"><div class="wrap"><div class="seclab">How it works</div>'
            f'<h2>One provider, served on treg.to\'s key</h2>'
            f'<p>{_logo(p0["domain"], p0["name"])}<b>{_esc_html(p0["name"])}</b> answers this job, at {rate}'
            'the provider\'s own rate with $0.000 markup, metered from your team\'s balance. No account with the '
            'provider and no key of your own, unless you would rather bring one.</p>'
            f'<div class="sample"><div class="sbar">the call</div><pre>{_esc_html(_uc_call(e0))}</pre></div>'
            f'<p style="font-size:12.5px;color:var(--muted)">Every endpoint on this shelf is listed on the '
            f'<a href="/catalog/{_esc_html(e0["platform"])}">{_esc_html((cat.platforms.get(e0["platform"]) or {}).get("label") or e0["platform"])} shelf</a>.</p>'
            + '</div></section>')
    elif form == "short":
        p0, e0 = provs[0], provs[0]["eps"][0]
        sections.append(
            '<section id="how"><div class="wrap"><div class="seclab">How it works</div>'
            f'<h2>One provider, on the account you already own</h2>'
            f'<p>{_logo(p0["domain"], p0["name"])}<b>{_esc_html(p0["name"])}</b> answers this job. You connect it once, '
            'treg.to keeps the token server side, and the call is never metered.</p>'
            f'<div class="sample"><div class="sbar">the call</div><pre>{_esc_html(_uc_call(e0))}</pre></div>'
            f'<p style="font-size:12.5px;color:var(--muted)">Every endpoint on this connection is listed on the '
            f'<a href="/catalog/{_esc_html(e0["platform"])}">{_esc_html((cat.platforms.get(e0["platform"]) or {}).get("label") or e0["platform"])} shelf</a>.</p>'
            + '</div></section>')
    else:
        inner = [f'<p>treg.to does not choose for you. It hands {_esc_html(agent_name)} this comparison, with the '
                 f'price shown before any call, and {_esc_html(agent_name)} picks. Or you <b>tell it how</b>: '
                 '"cheapest", "most reliable", "the one that takes what I have", or a provider by name.</p>']
        if headline:
            cheap_cards = "".join(
                f'<div class="card"><h4>Cheapest per {_esc_html(u)}</h4>'
                f'<p>{_logo(cheapest_by_unit[u]["domain"], cheapest_by_unit[u]["name"])}'
                f'<b>{_esc_html(cheapest_by_unit[u]["name"])}</b> at {_esc_html(money(cheapest_by_unit[u]["usd"]))}'
                + (f' &middot; {_esc_html(cheapest_by_unit[u]["platform_label"])}' if form == "platforms" else "")
                + '</p></div>' for u in units)
            inner.append(f'<h3 id="cheapest">{_esc_html(spec.get("q_cheapest", "Which is cheapest?"))}</h3>'
                         f'<div class="cards">{cheap_cards}</div>')
            if len(units) > 1:
                inner.append('<blockquote>Those units are not interchangeable: one call can return many results, '
                             'so compare on the unit you will actually be billed in.</blockquote>')
        if reliable:
            rel_rows = "".join(
                f'<tr><td><b>{_logo(p["domain"], p["name"])}{_esc_html(p["name"])}</b></td>'
                f'<td>{pct(p["ok_rate"])}</td><td>{ms(p["p50"])}</td><td style="color:var(--muted2)">{p["samples"]} calls</td></tr>'
                for p in reliable[:6])
            inner.append(f'<h3 id="reliable">{_esc_html(spec.get("q_reliable", "Which is the most reliable?"))}</h3>'
                         '<div class="tablewrap"><table><thead><tr><th>Provider</th><th>Success</th><th>Median</th>'
                         f'<th>Sample</th></tr></thead><tbody>{rel_rows}</tbody></table></div>'
                         '<blockquote>Measured on treg.to traffic: real calls, real inputs, and sample sizes differ '
                         'by provider. Live reliability, not a controlled benchmark.</blockquote>')
        inner.append(f'<h3 id="compare">{_esc_html(spec.get("q_compare", "How do they compare?"))}</h3>')
        if form == "platforms":
            for plat in platforms:
                rows_ = sorted([p for p in provs if p["platform_label"] == plat],
                               key=lambda p: (p["usd"] is None, p["usd"] or 0))
                inner.append(f'<h4 data-platform-group="{_esc_html(plat)}">{_esc_html(plat)}</h4>' + prov_table(rows_))
        else:
            inner.append(prov_table(sorted(provs, key=lambda p: (p["usd"] is None, p["usd"] or 0))))
        if headline and headline["cheapest_ep"]:
            shelves = ", ".join(
                f'<a href="/catalog/{_esc_html(sl)}">{_esc_html((cat.platforms.get(sl) or {}).get("label") or sl)}</a>'
                for sl in sorted({p["platform"] for p in provs}))
            inner.append(
                '<h3>Run one</h3>'
                f'<div class="sample"><div class="sbar">the cheapest verified call</div>'
                f'<pre>{_esc_html(_uc_call(headline["cheapest_ep"]))}</pre></div>'
                f'<p style="font-size:12.5px;color:var(--muted)">Swap the id for any provider above. '
                f'All {len(eps)} endpoints behind this job, with their parameters and captured responses, '
                f'are on the {shelves} shelf.</p>')
        inner.append(
            '<h3>How these numbers are made</h3>'
            '<div class="who">'
            '<div><b>Prices</b>Each provider&rsquo;s own published rate, converted to US dollars for one chargeable '
            'event of the unit they bill in. treg.to adds $0.000. Where a provider bills in credits, the conversion '
            'uses the rate on their public pricing page'
            + (f', last checked {_esc_html(latest_verified)}.' if latest_verified else '.') + '</div>'
            '<div><b>Success rate</b>treg.to&rsquo;s own served calls over the last 30 days: 2xx counts as a success, '
            '5xx and timeouts as a failure. A 4xx is excluded, because it usually means the caller sent bad '
            'parameters and one bad query should not make a healthy endpoint look broken.</div>'
            '<div><b>What this is not</b>A controlled benchmark. These are real calls with real inputs, so sample '
            'sizes and the difficulty of what was asked differ by provider. Treat the rates as live reliability, '
            'not a like-for-like test.</div>'
            '<div><b>Verified</b>The date treg.to last called the endpoint end to end and confirmed the shape of '
            'its response and the price it charged.</div>'
            '</div>')
        sections.append(f'<section id="bts"><div class="wrap"><div class="seclab">Behind the scenes</div>'
                        f'<h2>What {_esc_html(agent_name)} sees before it calls</h2>'
                        + "".join(inner) + '</div></section>')

    voices_html = "".join(
        f'<h3>{_esc_html(head)}</h3>'
        f'<blockquote>&ldquo;{_esc_html(quote)}&rdquo; '
        f'<a href="{_esc_html(url)}" rel="nofollow noopener" target="_blank">{_esc_html(who)}</a></blockquote>'
        f'<p><b>What this page can do about it:</b> {_esc_html(answer)}</p>'
        for head, quote, who, url, answer in spec.get("voices", []))
    voices_section = ('<section id="voices"><div class="wrap"><div class="seclab">From the field</div>'
                      '<h2>What people actually struggle with</h2>'
                      f'<p>{_esc_html(spec.get("voices_intro", ""))}</p>{voices_html}</div></section>'
                      if spec.get("voices") else "")
    notes = "".join(f'<h4>{_esc_html(x.split(".")[0])}.</h4><p>{_esc_html(x.split(".", 1)[1].strip())}</p>'
                    if "." in x else f"<p>{_esc_html(x)}</p>" for x in spec["notes"])
    def _related_card(lbl: str) -> str:
        href, owner = _related_link(lbl, agent_slug)
        return (f'<a class="card" href="{href}"><h4>{_esc_html(lbl)}</h4>'
                f'<p>Another job in {_esc_html((owner or cat_label).lower())}.</p></a>')

    related = "".join(_related_card(lbl) for lbl in spec.get("related", ()))
    faq_html = "".join(f'<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>' for q, a in spec["faq"])

    # The "instead of" anchor: what the same job costs on subscriptions from the providers on this
    # page whose plan prices are recorded in marketing/landing/_facts.md, against a real run here.
    # Only sourced figures are named; with none, the anchor is the catalog's own spread.
    plans = [(p["name"], agent_pages.PLAN_PRICES[p["id"]]) for p in provs
             if p["id"] in agent_pages.PLAN_PRICES]
    pricewall = ""
    if headline:
        run_n = 100
        run_cost = headline["usd"] * run_n
        if plans:
            plans = sorted(plans, key=lambda t: -t[1])[:2]
            old_total = sum(v for _, v in plans)
            old_note = " + ".join(f"{k} ${v}/mo" for k, v in plans) + ", at list"
            old_v, old_k = f"${old_total}/mo", "instead of"
        else:
            dearest = max((p for p in provs if p["usd"]), key=lambda p: p["usd"])
            old_total = dearest["usd"] * run_n
            old_note = f"{dearest['name']}, the dearest here, for the same {run_n}"
            old_v, old_k = f"${old_total:,.2f}", "the wide end"
        pricewall = (
            '<section id="cost"><div class="wrap"><div class="seclab">The economics</div>'
            f'<h2>What {run_n} of these actually costs</h2>'
            '<div class="pricewall">'
            f'<div class="pw old"><div class="k">{old_k}</div><div class="v">{old_v}</div>'
            f'<div class="s">{_esc_html(old_note)}</div></div>'
            '<div class="arrow">&rarr;</div>'
            f'<div class="pw new"><div class="k">you pay</div><div class="v">${run_cost:,.2f}</div>'
            f'<div class="s">{run_n} &times; {_esc_html(money(headline["usd"]))} at {_esc_html(headline["name"])}, '
            'metered per call</div></div></div>'
            '<p style="font-size:12.5px;color:var(--muted)">Subscription figures are provider list prices recorded in '
            'treg.to&rsquo;s own catalog grid; per-call prices are what treg.to charges today, with $0.000 added.</p>'
            '</div></section>')

    body = (
        '<div class="hero"><div class="wrap">'
        f'<div class="trust" style="margin:0 0 18px"><a href="/">treg.to</a> / <a href="/use-cases">Use cases</a> / '
        f'<a href="/use-cases#{agent_pages.category_slug(cat_label)}">{_esc_html(cat_label)}</a></div>'
        f'<div class="kicker">{n} providers &middot; {hero_price} &middot; $0.000 markup</div>'
        f'<h1>{_esc_html(spec["sentence"])}</h1>'
        f'<div class="lede">{_esc_html(lede)}</div>'
        '<div class="ctas">'
        f'<a class="candy" href="/app?ref=uc-{_esc_html(job_slug)}">Start free</a>'
        '<a class="ghostbtn" href="#bts">See the comparison</a></div>'
        f'<div class="trust">$1.00 of free credit on every new team &middot; no provider signup &middot; no card</div>'
        f'<div class="subline">{n_ver} of {len(eps)} endpoints on this page are live-verified against the provider.</div>'
        f'{provstrip}</div></div>'

        + pricewall +
        '<section id="ask"><div class="wrap"><div class="seclab">Try it</div>'
        f'<h2>What&rsquo;s the best way to ask {_esc_html(agent_name)}?</h2>'
        f'<div class="steplabel"><span class="n">1</span><b>Set your agent up, once</b></div>'
        + promptbox("in your agent's chat", setup)
        + f'<div class="steplabel"><span class="n">2</span><b>Ask for the job</b></div>'
        + promptbox("the prompt", spec["prompt"])
        + f'<div class="provstrip"><div class="pl">works in</div><div class="ptiles">{agent_icons}</div></div>'
        + f'<h3>Why this prompt works</h3><div class="cards">{why_cards}</div>'
        + (f'<div class="sample"><div class="sbar">{_esc_html(agent_name)}</div>'
           f'<img src="{_esc_html(spec["result_image"])}" alt="{_esc_html(agent_name)} answering" '
           'style="display:block;width:100%"/></div>' if spec.get("result_image") else "")
        + '</div></section>'

        '<section id="why"><div class="wrap"><div class="seclab">Why treg.to</div>'
        '<h2>Why go through treg.to</h2>'
        f'<div class="cards">{treg_cards}</div></div></section>'

        + "".join(sections) + voices_section

        + '<section id="notes"><div class="wrap"><div class="seclab">The detail</div>'
          f'<h2>What actually differs</h2>{notes}</div></section>'

        + f'<section id="what"><div class="wrap"><div class="seclab">Background</div>'
          f'<h2>{_esc_html(spec.get("what_is_heading", "What is this?"))}</h2>'
          f'<p>{_esc_html(spec["what_is"])}</p></div></section>'

        + f'<section id="faq"><div class="wrap"><div class="seclab">Questions</div>'
          f'<h2>Before you start</h2>{faq_html}</div></section>'

        + (f'<section id="related"><div class="wrap"><div class="seclab">Related</div>'
           f'<h2>Other jobs your agent can do</h2><div class="cards">{related}</div></div></section>' if related else "")
        + (f'<section id="workflows"><div class="wrap"><div class="seclab">Run the full sequence</div>'
           f'<h2>Workflows that use this job</h2><div class="cards">'
           + "".join(f'<a class="card" href="/workflows/{_esc_html(ws)}"><h3>{_esc_html(wsent)}</h3>'
                     f'<p>One prompt, every step priced, with the receipt from a real run.</p></a>'
                     for ws, wsent in job_workflows)
           + '</div></div></section>' if job_workflows else "")
        + _COPY_JS)
    ld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Use cases", "item": base + "/use-cases"},
            {"@type": "ListItem", "position": 3, "name": cat_label,
             "item": f"{base}/use-cases#{agent_pages.category_slug(cat_label)}"},
            {"@type": "ListItem", "position": 4, "name": spec["sentence"],
             "item": f"{base}/use-cases/{job_slug}"}]},
        {"@context": "https://schema.org", "@type": "ItemList", "name": title, "numberOfItems": len(provs),
         "itemListElement": [{"@type": "ListItem", "position": i, "name": p["name"],
                              "url": f"{base}/use-cases/{job_slug}#compare"}
                             for i, p in enumerate(provs, 1)]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in spec["faq"]]},
    ]
    return _page(title, desc[:300], f"/use-cases/{job_slug}", body, ld,
                 head_extra=_MD_ALT.format(href=f"{base}/use-cases/{job_slug}.md"),
                 css="usecase.css")


@app.get("/use-cases", include_in_schema=False)
async def use_cases_hub():
    """The hub the spokes hang from. A sitemap is not a crawl path: before this existed, the only
    link into a use-case page was one row on one agent page's menu."""
    if not _hosted():
        raise HTTPException(status_code=404, detail="not found")
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    _, agent_name = _uc_agent()
    by_cat: dict[str, list[str]] = {}
    for j, spec in agent_pages.USE_CASE_PAGES.items():
        label = _job_category(spec["label"])
        caps = _use_case_caps(spec["label"])
        eps = [e for cid in caps for e in cat.for_capability(cid) if _pub(e)]
        nprov = len({e["provider"] for e in eps})
        prices = [cv["usd"] for e in eps if (cv := cat.cost_view(e.get("cost"), e.get("provider"))) and cv["usd"]]
        meta = (f"{nprov} provider{'s' if nprov != 1 else ''} &middot; from {_esc_html(_usd_short(min(prices)))}"
                if prices else "free on your own account")
        blurb = spec["lede"].format(n=nprov, agent=agent_name,
                                    cheapest=_usd_short(min(prices)) if prices else "free")
        by_cat.setdefault(label, []).append(
            f'<a class="pcard" href="/use-cases/{j}"><h3>{_esc_html(spec["sentence"])}</h3>'
            f'<p>{_esc_html(blurb[:140])}</p><div class="meta">{meta}</div></a>')
    blocks = "".join(f'<section class="cat"><h2 id="{_anchor(c)}">{_esc_html(c)}</h2>'
                     f'<div class="grid">{"".join(v)}</div></section>' for c, v in by_cat.items())
    body = (
        '<main class="wrap"><div class="phead">'
        '<div class="crumbs"><a href="/">treg.to</a> / <a href="/use-cases">Use cases</a></div>'
        '<h1>What you can have your agent do</h1>'
        '<p class="lede">One page per job: the prompt that works, what the call costs, and every provider '
        'that does it. All of it through one treg.to key, at the provider&rsquo;s own rate with $0.000 markup.</p>'
        '</div>' + blocks
        + '<section class="cat"><h2>Everything else</h2><div class="cap"><p style="margin:0">These are the jobs '
          'written up so far. The full menu is on the agent pages, and the whole catalog is at '
          '<a href="/catalog">/catalog</a>. The multi-step versions are at <a href="/workflows">/workflows</a>.'
          '</p></div></section></main>')
    ld = [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
        {"@type": "ListItem", "position": 2, "name": "Use cases", "item": base + "/use-cases"}]}]
    return _page("What you can have your agent do | treg.to",
                 "One page per job: the prompt that works in ChatGPT or Claude, what the call costs, and "
                 "every provider that does it, compared. One treg.to key, no markup.",
                 "/use-cases", body, ld)


# ------------------------------------------------------------------ /workflows/<slug>

def _wf_use_case_link(cap: str, agent_slug: str) -> str:
    """Where a workflow step links: the use-case page for the menu row that carries this
    capability, or the row's category anchor on the agent page when no page is written."""
    for category, jobs in agent_pages.USE_CASES:
        for lbl, caps in jobs:
            if cap in caps:
                return (_use_case_page_for(lbl)
                        or f"/agents/{agent_slug}#{agent_pages.category_slug(category)}")
    return f"/agents/{agent_slug}"


async def _wf_steps(cat, observations: endpoint_stats.EndpointObservationReader,
                    spec: dict, agent_slug: str) -> list[dict]:
    """One dict per step, priced live from the catalog: the endpoint the worked run used, its
    price per billing unit, how many providers do the step, and the observed stats when any."""
    out = []
    for name, cap, asks, ep_id, why in spec["steps"]:
        eps = [e for e in cat.for_capability(cap) if _pub(e)]
        used = next((e for e in eps if e["id"] == ep_id), None)
        cv = cat.cost_view(used.get("cost"), used.get("provider")) if used else None
        usd = cv["usd"] if cv and cv["usd"] else None
        unit = _UNIT_WORDS.get(((used or {}).get("cost") or {}).get("type"), "call")
        st = (await _observed_or_empty(observations, [ep_id])).get(ep_id) or {} if used else {}
        prov = used["provider"] if used else ep_id.split(".")[0]
        out.append({
            "name": name, "cap": cap, "asks": asks, "why": why, "ep": used, "ep_id": ep_id,
            "provider": prov, "provider_name": _provider_display(prov),
            "domain": agent_pages.PROVIDER_DOMAINS.get(prov),
            "usd": usd, "unit": unit, "providers": len({e["provider"] for e in eps}),
            "ok_rate": st.get("ok_rate") if st.get("samples") else None,
            "p50": st.get("p50_ms") if st.get("samples") else None,
            "samples": st.get("samples") or 0,
            "link": _wf_use_case_link(cap, agent_slug),
        })
    return out


_WF_CSS = """
<style>
.wftable td,.wftable th{vertical-align:top}
.wftable td.n{font-family:var(--mono);color:var(--muted2);white-space:nowrap}
.wftable .why{display:block;font-size:12.5px;color:var(--muted);margin-top:4px}
.wftable .asks{color:var(--muted)}
.wftotal{margin:14px 0 0;padding:12px 14px;border:1px solid var(--line,rgba(0,0,0,.12));border-radius:10px;font-size:14px}
.wftotal b{font-family:var(--mono)}
.receipt{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;margin:12px 0 18px;font-size:14.5px}
.receipt dt{color:var(--muted)}
.receipt dd{margin:0;font-family:var(--mono)}
</style>"""


@app.get("/workflows/{slug}.csv", include_in_schema=False)
async def workflow_csv(slug: str):
    """The CSV of the run the page reports, hand-recorded from that run. 404 when no file."""
    key = next((k for k in agent_pages.WORKFLOWS if k == slug.lower()), None)
    if key is None or not _hosted():
        raise HTTPException(status_code=404, detail="unknown workflow")
    f = Path(agent_pages.__file__).parent / "workflow_runs" / f"{key}.csv"
    if not f.exists():
        raise HTTPException(status_code=404, detail="no run recorded")
    return FileResponse(f, media_type="text/csv", filename=f"{key}.csv",
                        headers={"Cache-Control": "public, max-age=600"})


@app.get("/workflows/{slug}.md", include_in_schema=False)
@app.get("/workflows/{slug}", include_in_schema=False)
async def workflow_page(request: Request, slug: str,
                        observations: endpoint_stats.EndpointObservationReader = Depends(
                            _endpoint_observation_reader)):
    """One workflow: the sequence a person runs, as ONE prompt. A use-case page answers one job;
    this chains several, with a per-step price pulled live from the catalog, a receipt and CSV from
    a real run (hand-recorded in `agent_pages.WORKFLOWS`, dated), and the failure modes. `.md`
    serves the same page as Markdown. Hosted-only, like the use-case pages."""
    as_md = request.url.path.endswith(".md")
    raw = slug[:-3] if slug.endswith(".md") else slug
    key = next((k for k in agent_pages.WORKFLOWS if k == raw.lower()), None)
    if key is None or not _hosted():
        raise HTTPException(status_code=404, detail="unknown workflow")
    if raw != key:
        return RedirectResponse(f"/workflows/{key}" + (".md" if as_md else ""), status_code=301)
    wf_slug = key
    spec = agent_pages.WORKFLOWS[key]
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    agent_slug, agent_name = _uc_agent()
    steps = await _wf_steps(cat, observations, spec, agent_slug)
    run = spec["run"]
    n_steps = len(steps)
    n_prov = len({s["provider"] for s in steps})
    rows_in = int(run.get("rows_in") or 0)
    once = set(spec.get("once") or ())
    for s in steps:  # how many times the step's endpoint is called on a full-hit run
        s["calls"] = 1 if s["ep_id"] in once else rows_in
    worst = sum((s["usd"] or 0) * s["calls"] for s in steps)
    setup = agent_pages.SETUP_LINE.format(base=base)

    def money(x):
        return _usd_short(x)

    def pct(x):
        return f"{round(x * 100)}%" if x is not None else ""

    def ms(x):
        return (f"{x/1000:.1f}s" if x >= 1000 else f"{int(x)}ms") if x else ""

    title = spec["title"].format(n=n_steps, steps=n_steps)
    lede = spec["lede"].format(n=n_steps, steps=n_steps)
    desc = _serp_desc(f"{spec['sentence']}. {n_steps} steps through one treg.to key, priced before "
                      f"each call, with a real run's receipt.")

    if as_md:
        md = [f"# {spec['sentence']}", "", lede, "",
              "## Try it", "",
              f"Setup line (paste into any agent): `{setup}`", "",
              f'Then ask: "{spec["prompt"]}"', ""]
        md += [f"- **{t}** {d}" for t, d in spec["prompt_why"]]
        md += ["", "## The steps", "",
               "| # | Step | What the agent asks | Provider used | Price | Success rate |", "|---|---|---|---|---|---|"]
        for i, s in enumerate(steps, 1):
            price = f"{money(s['usd'])} per {s['unit']}" if s["usd"] else "no dollar rate published"
            rel = f"{pct(s['ok_rate'])} over {s['samples']} calls, {ms(s['p50'])} median" if s["samples"] else "not yet measured"
            md.append(f"| {i} | {s['name']} | {s['asks']} | {s['provider_name']} (`{s['ep_id']}`, {s['providers']} providers, "
                      f"{base}{s['link']}) | {price} | {rel} |")
        md += [""] + [f"- {s['name']}: {s['why']}" for s in steps]
        md += ["", f"At the rates above, {rows_in} rows where every call hits comes to ${worst:,.2f}. The receipt below is what it actually cost, and why it differs.", ""]
        md += [f"## What it actually cost", "", f"Run on {run['date']}.", ""]
        md += [f"- {k}: {v}" for k, v in run["receipt"]]
        md += [""] + list(run["narrative"])
        md += ["", f"Download the CSV of this run: {base}{run['csv']}", ""]
        md += ["## Why go through treg.to", ""] + [f"- **{t}** {d}" for t, d in agent_pages.WHY_TREG]
        md += ["", "## Where it goes wrong", ""]
        for h, p in spec["failure_modes"]:
            md += [f"**{h}** {p}", ""]
        md += ["## Before you start", ""]
        for q, a in spec["faq"]:
            md += [f"**{q}** {a}", ""]
        md += ["## Related", ""]
        for lbl in spec["related"]:
            href, _owner = _related_link(lbl, agent_slug)
            md.append(f"- {lbl}: {base}{href}")
        md += ["", f"HTML version: {base}/workflows/{wf_slug}"]
        return PlainTextResponse("\n".join(md), media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=600"})

    # ---------------------------------------------------------------- html (use-case skin)
    seen, ptiles = set(), []
    for s in steps:
        if s["domain"] and s["provider"] not in seen:
            seen.add(s["provider"])
            ptiles.append(f'<span class="ptile" title="{_esc_html(s["provider_name"])}">{_logo(s["domain"], s["provider_name"])}</span>')
    provstrip = (f'<div class="provstrip"><div class="pl">used in this run</div>'
                 f'<div class="ptiles">{"".join(ptiles)}</div></div>' if ptiles else "")
    agent_icons = "".join(
        f'<span class="ptile" title="{_esc_html(label)}">'
        f'<img src="https://unpkg.com/@lobehub/icons-static-png@latest/light/{icon}.png" alt="{_esc_html(label)}" loading="lazy"/></span>'
        for aid, label, icon in agent_pages.AGENT_ICONS[:6])

    def promptbox(label: str, text: str) -> str:
        return ('<div class="promptbox"><div class="ph">'
                f'<span>{_esc_html(label)}</span>'
                f'<button class="copybtn" data-copy="{_esc_html(text)}">copy</button></div>'
                f'<pre>{_esc_html(text)}</pre></div>')

    why_cards = "".join(f'<div class="card"><h4>{_esc_html(t)}</h4><p>{_esc_html(d)}</p></div>'
                        for t, d in spec["prompt_why"])
    treg_cards = "".join(f'<div class="card"><h4>{_esc_html(t)}</h4><p>{_esc_html(d)}</p></div>'
                         for t, d in agent_pages.WHY_TREG)

    def price_cell(s: dict) -> str:
        if s["usd"]:
            return f'{_esc_html(money(s["usd"]))} <span style="color:var(--muted2)">per {s["unit"]}</span>'
        return '<span style="color:var(--muted2)">no dollar rate published</span>'

    def rel_cell(s: dict) -> str:
        if not s["samples"]:
            return '<span style="color:var(--muted2)">not yet measured</span>'
        return (f'{pct(s["ok_rate"])} <span style="color:var(--muted2)">({s["samples"]} calls'
                + (f', {ms(s["p50"])} median' if s["p50"] else "") + ')</span>')

    step_rows = "".join(
        f'<tr data-step="{i}">'
        f'<td class="n">{i}</td>'
        f'<td><b>{_esc_html(s["name"])}</b><span class="why">{_esc_html(s["why"])}</span></td>'
        f'<td class="asks">{_esc_html(s["asks"])}</td>'
        f'<td><a href="{_esc_html(s["link"])}">{_logo(s["domain"], s["provider_name"])}{_esc_html(s["provider_name"])}</a>'
        f'<span class="why">{s["providers"]} provider{"s" if s["providers"] != 1 else ""} do this step</span></td>'
        f'<td>{price_cell(s)}</td>'
        f'<td>{rel_cell(s)}</td>'
        '</tr>' for i, s in enumerate(steps, 1))
    steps_table = ('<div class="tablewrap"><table class="wftable"><thead><tr>'
                   '<th>#</th><th>Step</th><th>What the agent asks</th><th>Provider used</th><th>Price</th><th>Success rate</th>'
                   f'</tr></thead><tbody>{step_rows}</tbody></table></div>'
                   f'<div class="wftotal">At the rates above, {rows_in} rows where every call hits comes to <b>${worst:,.2f}</b>'
                   f'<span style="color:var(--muted)"> ({" + ".join(f"{s["calls"]} &times; {_esc_html(money(s["usd"]))}" for s in steps if s["usd"])}). '
                   'The receipt below is what it actually cost.</span></div>')

    receipt = "".join(f'<dt>{_esc_html(k)}</dt><dd>{_esc_html(v)}</dd>' for k, v in run["receipt"])
    narrative = "".join(f'<p>{_esc_html(p)}</p>' for p in run["narrative"])
    failures = "".join(f'<h3>{_esc_html(h)}</h3><p>{_esc_html(p)}</p>' for h, p in spec["failure_modes"])
    faq_html = "".join(f'<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>' for q, a in spec["faq"])

    def _related_card(lbl: str) -> str:
        href, owner = _related_link(lbl, agent_slug)
        return (f'<a class="card" href="{href}"><h4>{_esc_html(lbl)}</h4>'
                f'<p>One step of this workflow, on its own{(", in " + _esc_html(owner.lower())) if owner else ""}.</p></a>')
    related = "".join(_related_card(lbl) for lbl in spec.get("related", ()))

    body = (
        '<div class="hero"><div class="wrap">'
        f'<div class="trust" style="margin:0 0 18px"><a href="/">treg.to</a> / <a href="/workflows">Workflows</a> / '
        f'{_esc_html(spec["sentence"])}</div>'
        f'<div class="kicker">{n_steps} steps &middot; {n_prov} providers &middot; $0.000 markup</div>'
        f'<h1>{_esc_html(spec["sentence"])}</h1>'
        f'<div class="lede">{_esc_html(lede)}</div>'
        '<div class="ctas">'
        f'<a class="candy" href="/app?ref=wf-{_esc_html(wf_slug)}">Start free</a>'
        '<a class="ghostbtn" href="#run">See the receipt</a></div>'
        '<div class="trust">$1.00 of free credit on every new team &middot; no provider signup &middot; no card</div>'
        f'{provstrip}</div></div>'

        '<section id="ask"><div class="wrap"><div class="seclab">Try it</div>'
        f'<h2>One prompt runs the whole thing</h2>'
        f'<div class="steplabel"><span class="n">1</span><b>Set your agent up, once</b></div>'
        + promptbox("in your agent's chat", setup)
        + f'<div class="steplabel"><span class="n">2</span><b>Ask for the list</b></div>'
        + promptbox("the prompt", spec["prompt"])
        + f'<div class="provstrip"><div class="pl">works in</div><div class="ptiles">{agent_icons}</div></div>'
        + f'<h3>Why this prompt works</h3><div class="cards">{why_cards}</div>'
        + '</div></section>'

        + '<section id="steps"><div class="wrap"><div class="seclab">The steps</div>'
          f'<h2>What {_esc_html(agent_name)} calls, and what each call costs</h2>'
          '<p>Prices are the provider&rsquo;s own rate, read from the catalog when this page loads, with $0.000 '
          'added by treg.to. Success rates are treg.to&rsquo;s own served calls over the last 30 days.</p>'
          + steps_table + '</div></section>'

        + '<section id="run"><div class="wrap"><div class="seclab">The receipt</div>'
          '<h2>What it actually cost</h2>'
          f'<p style="color:var(--muted)">Run on {_esc_html(run["date"])}, {rows_in} companies in.</p>'
          f'<dl class="receipt">{receipt}</dl>{narrative}'
          f'<p><a class="ghostbtn" href="{_esc_html(run["csv"])}">Download the CSV of this run</a></p>'
          '</div></section>'

        + '<section id="why"><div class="wrap"><div class="seclab">Why treg.to</div>'
          '<h2>Why go through treg.to</h2>'
          f'<div class="cards">{treg_cards}</div></div></section>'

        + '<section id="failures"><div class="wrap"><div class="seclab">The detail</div>'
          f'<h2>Where it goes wrong</h2>{failures}</div></section>'

        + '<section id="faq"><div class="wrap"><div class="seclab">Questions</div>'
          f'<h2>Before you start</h2>{faq_html}</div></section>'

        + (f'<section id="related"><div class="wrap"><div class="seclab">Related</div>'
           f'<h2>Each step on its own</h2><div class="cards">{related}</div></div></section>' if related else "")
        + _COPY_JS)
    ld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Workflows", "item": base + "/workflows"},
            {"@type": "ListItem", "position": 3, "name": spec["sentence"],
             "item": f"{base}/workflows/{wf_slug}"}]},
        {"@context": "https://schema.org", "@type": "HowTo", "name": spec["sentence"],
         "description": desc,
         "step": [{"@type": "HowToStep", "position": i, "name": s["name"], "text": s["asks"],
                   "url": f"{base}/workflows/{wf_slug}#steps"} for i, s in enumerate(steps, 1)]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in spec["faq"]]},
    ]
    return _page(title, desc[:300], f"/workflows/{wf_slug}", body, ld,
                 head_extra=_MD_ALT.format(href=f"{base}/workflows/{wf_slug}.md") + _WF_CSS,
                 css="usecase.css")


@app.get("/workflows", include_in_schema=False)
async def workflows_hub(observations: endpoint_stats.EndpointObservationReader = Depends(
        _endpoint_observation_reader)):
    """The hub the workflow pages hang from: one card per workflow, priced per row from the catalog."""
    if not _hosted():
        raise HTTPException(status_code=404, detail="not found")
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    agent_slug, _agent_name = _uc_agent()
    cards = []
    for slug, spec in agent_pages.WORKFLOWS.items():
        steps = await _wf_steps(cat, observations, spec, agent_slug)
        # Per row: a once-per-run step (the list page) is spread over the run's rows.
        rows_in = int(spec["run"].get("rows_in") or 0) or 1
        once = set(spec.get("once") or ())
        per_row = sum(((s["usd"] or 0) / rows_in) if s["ep_id"] in once else (s["usd"] or 0) for s in steps)
        n = len(steps)
        meta = f"{n} steps &middot; from {_esc_html(_usd_short(per_row))} per row" if per_row else f"{n} steps"
        blurb = spec["lede"].format(n=n, steps=n)
        cards.append(f'<a class="pcard" href="/workflows/{slug}"><h3>{_esc_html(spec["sentence"])}</h3>'
                     f'<p>{_esc_html(blurb[:140])}</p><div class="meta">{meta}</div></a>')
    body = (
        '<main class="wrap"><div class="phead">'
        '<div class="crumbs"><a href="/">treg.to</a> / <a href="/workflows">Workflows</a></div>'
        '<h1>Workflows your agent can run from one prompt</h1>'
        '<p class="lede">A use-case page answers one job. A workflow is the sequence a person actually runs: '
        'one prompt, a price per step read live from the catalog, and the receipt and CSV of a real run. '
        'All of it through one treg.to key, at the provider&rsquo;s own rate with $0.000 markup.</p>'
        f'</div><section class="cat"><div class="grid">{"".join(cards)}</div></section>'
        '<section class="cat"><h2>Everything else</h2><div class="cap"><p style="margin:0">The single-job '
        'versions are at <a href="/use-cases">/use-cases</a>, and the whole catalog is at '
        '<a href="/catalog">/catalog</a>.</p></div></section></main>')
    ld = [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
        {"@type": "ListItem", "position": 2, "name": "Workflows", "item": base + "/workflows"}]}]
    return _page("Workflows your agent can run from one prompt | treg.to",
                 "Multi-step jobs as one prompt: a price per step read live from the catalog, and the "
                 "receipt and CSV of a real run. One treg.to key, no markup.",
                 "/workflows", body, ld)


@app.get("/catalog.css", include_in_schema=False)
async def catalog_css():
    """The shared skin for /catalog, /catalog/<slug> and /docs — the landing's tokens, one copy."""
    f = _WEB_DIR / "catalog.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="catalog.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "public, max-age=600"})


def _provider_rows() -> list[dict]:
    """One row per provider, busiest first. Census rules match _platform_rows (browse surface
    only), so the provider grid and the platform grid can never disagree about inventory."""
    cat = catalog_store.load()
    rows = []
    for service in sorted({e["provider"] for e in cat.endpoints}):
        eps = [e for e in cat.for_provider(service) if _pub(e)]
        if not eps:
            continue
        rows.append({
            "service": service,
            "display": _provider_display(service),
            "endpoints": len(eps),
            "verified": len([e for e in eps if e["verified"]]),
            "capabilities": len({e["capability"] for e in eps if e["capability"]}),
            "platforms": sorted({e["platform"] for e in eps}),
            "price_from": min(
                (c for e in eps if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"]),
                key=lambda c: c["usd"],
                default=None,
            ),
        })
    rows.sort(key=lambda r: (-r["endpoints"], r["service"]))
    return rows


_AGENTS = [("ChatGPT", "openai.png"), ("Claude", "claude-color.png"),
           ("Claude Code", "claudecode-color.png"), ("Codex", "codex-color.png"),
           ("Cursor", "cursor.png"), ("Grok", "grok.png"), ("Gemini CLI", "gemini-color.png")]


_AGENT_CDN = "https://unpkg.com/@lobehub/icons-static-png@latest/light/"


def _agent_ptiles() -> str:
    return "".join(
        f'<span class="ptile" title="{n}"><img src="{_AGENT_CDN}{f}" alt="{n}" loading="lazy" '
        'onerror="this.parentNode.style.display=\'none\'"/></span>'
        for n, f in _AGENTS)


_TOOLS_CSS = """<style>
.fx{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;gap:12px;align-items:stretch;margin-top:22px}
@media(max-width:900px){.fx{grid-template-columns:1fr}.fx .ar{transform:rotate(90deg);justify-self:center}}
.fx .ar{color:var(--muted2);align-self:center;font-size:16px}
.fp{background:var(--surface);border-radius:14px;box-shadow:0 1px 2px rgba(0,0,0,.05),0 6px 18px rgba(0,0,0,.04);padding:14px 16px}
.fp .tag{font:600 10px var(--mono);letter-spacing:.14em;color:var(--muted2);margin-bottom:9px}
.fp .agrow{display:flex;gap:6px;margin-bottom:9px}
.fp .agrow .ptile{width:28px;height:28px;border-radius:8px}
.fp .agrow .ptile img{width:16px;height:16px}
.fp .t{display:flex;justify-content:space-between;gap:10px;font:12px var(--mono);color:var(--ink);padding:3.5px 0}
.fp .t i{color:var(--green);font-style:normal}
.fp.dk{background:var(--inverse);color:#e8e8e2}
.fp.dk .b{display:flex;gap:8px;align-items:center;font:600 13.5px var(--mono);margin-bottom:10px}
.fp.dk .c{background:rgba(255,255,255,.08);border-radius:8px;padding:9px 11px;font:12px var(--mono);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fp.dk .c em{color:#7ec8ec;font-style:normal}.fp.dk .c i{color:#6fd39a;font-style:normal}
.fp.dk .c{margin-top:7px}
.fp.dk .cr{font:11px var(--mono);color:#9a9a92;padding:4px 2px 0 11px}
.fp.dk .cr i{color:#6fd39a;font-style:normal}
.fp .ph2{display:flex;align-items:center;gap:9px;padding-bottom:9px;border-bottom:1px solid var(--panel2);margin-bottom:9px}
.fp .ph2 img{width:26px;height:26px;border-radius:7px}
.fp .ph2 b{font-size:15px}
.fp .bdg{margin-left:auto;font:600 10px var(--mono);letter-spacing:.1em;color:var(--teal);
  background:var(--panel);border-radius:6px;padding:3px 7px}
.fp .sm{color:var(--muted);font-size:12.5px}
.fp .ct{margin-top:8px;font:11.5px var(--mono);color:var(--muted2)}
details.tl{background:var(--surface);border-radius:14px;box-shadow:0 1px 2px rgba(0,0,0,.05),0 6px 18px rgba(0,0,0,.04);margin:12px 0;padding:2px 18px}
details.tl summary{cursor:pointer;font-weight:600;padding:13px 0;font-size:14.5px}
details.tl[open] summary{border-bottom:1px solid var(--panel2)}
details.tl ul{margin:12px 0;padding-left:20px}
details.tl li{margin:9px 0;font-size:13.5px}
details.tl li small{color:var(--muted)}
details.tl li.more{font-style:italic;margin-top:14px}
details.tl li.more a{color:var(--link);text-decoration:none}
</style>"""


@app.get("/tools/{service}", include_in_schema=False)
async def tools_provider(service: str, db: AsyncSession = Depends(get_session)):
    """One provider's public page, in the use-case pages' skin (usecase.css): hero on the two
    measured terms — "{provider} api pricing" (what Search Console shows people typing) and
    "{provider} mcp" — the agent->treg->provider flow, setup (agent one-liner first), a prompt
    to try, why-treg cards, EVERY tool grouped by platform, alternatives and an FAQ that also
    feeds the FAQPage JSON-LD. The signed-in twin stays at /app/marketplace/<service>."""
    cat = catalog_store.load()
    all_eps = cat.for_provider(service)
    if not all_eps:
        raise HTTPException(status_code=404, detail=f"unknown provider {service!r}")
    # Fresh name on purpose: from here on the page prints the CATALOG's spelling of the provider,
    # never the request's. (Same idiom as the use-case pages; it is also what reads as a taint
    # kill to CodeQL, which cannot see _esc_html as a sanitizer.)
    svc = all_eps[0]["provider"]
    eps = [e for e in all_eps if _pub(e)] or [e for e in all_eps if e.get("kind") != "routed"]
    if not eps:
        # The first-party "treg" pseudo-provider is nothing but routed meta-rows. Without this a
        # self-referential /tools/treg page rendered (and reached the sitemap) — the fallback above
        # resurrects hidden kinds for providers whose real rows are all utility, never routed ones.
        raise HTTPException(status_code=404, detail=f"unknown provider {service!r}")
    display = _provider_display(svc)
    esc_d = _esc_html(display)
    base = get_settings().public_url.rstrip("/")
    reg = oauth_providers.get(svc)
    category = (getattr(reg, "category", "") or "") if reg else ""
    blurb = (getattr(reg, "summary", "") or "") if reg else ""
    base_api = (getattr(reg, "base_url", "") or "") if reg else ""
    docs_url = (getattr(reg, "docs_url", "") or "") if reg else ""
    prices = [c for e in eps if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"]]
    # Own-account vs metered is read off the INVENTORY, not the credential registry: nearly every
    # provider is in oauth_providers (that is how a team registers its own key), but only a
    # provider with no priced endpoint at all is genuinely connect-your-own-account.
    is_oauth = not prices
    cheapest = _price_label(min(prices, key=lambda c: c["usd"])) if prices else ""
    verified = len([e for e in eps if e["verified"]])
    plat_label = {sl: pl["label"] for sl, pl in cat.platforms.items()}
    groups: dict[str, list[dict]] = {}
    for e in eps:
        groups.setdefault(e["platform"], []).append(e)

    # Task lines are the provider's top capabilities, verbatim from the catalog's own descriptions.
    cap_counts: dict[str, int] = {}
    for e in eps:
        if e["capability"]:
            cap_counts[e["capability"]] = cap_counts.get(e["capability"], 0) + 1
    seen_desc: set[str] = set()
    task_lines: list[str] = []
    for c in sorted(cap_counts, key=lambda k: -cap_counts[k]):
        d = (cat.capabilities.get(c) or c).strip()
        if not d or d in seen_desc:
            continue
        seen_desc.add(d)
        task_lines.append(d[0].lower() + d[1:])
        if len(task_lines) == 3:
            break
    sample_eps = sorted([e for e in eps if e["verified"]], key=lambda e: len(e["id"])) or eps
    sample_id = sample_eps[0]["id"]
    badge = "YOUR ACCOUNT" if is_oauth else "NO SIGNUP"
    # The measured line: what treg.to has actually observed calling this provider. It is the one
    # thing a vendor's own pricing page cannot print, and it goes above the fold for that reason.
    obs = await _observed_or_empty(db, [e["id"] for e in eps])
    o_samples = sum(int(o.get("samples") or 0) for o in obs.values())
    # The provider-wide rate weights each endpoint's published rate by the calls that DECIDED it
    # (2xx + 5xx). `samples` still counts callers' 4xx, so weighting by it would let one team's
    # malformed requests drag a healthy provider down. Latency is the median of the endpoint
    # medians that cleared the successful-sample floor; `p50_ms` is the key endpoint_stats emits.
    o_ok = [o for o in obs.values() if o.get("ok_rate") is not None and (o.get("decided") or 0) > 0]
    o_ok_rate = (sum(o["ok_rate"] * o["decided"] for o in o_ok) / sum(o["decided"] for o in o_ok)) if o_ok else None
    o_p50s = sorted(o["p50_ms"] for o in obs.values() if o.get("p50_ms"))
    o_p50 = o_p50s[len(o_p50s) // 2] if o_p50s else None
    measured = ""
    if o_samples:
        measured = f"{o_samples:,} calls measured"
        if o_ok_rate is not None:
            measured += f" · {round(o_ok_rate * 100)}% ok"
        if o_p50:
            measured += f" · {(f'{o_p50/1000:.1f}s' if o_p50 >= 1000 else f'{int(o_p50)}ms')} median"
    used_in = _jobs_by_provider().get(svc, [])
    demo_eps = []
    _seen_caps: set[str] = set()
    for e in sample_eps:
        if e["capability"] in _seen_caps:
            continue
        _seen_caps.add(e["capability"])
        demo_eps.append(e)
        if len(demo_eps) == 3:
            break

    kicker = (f"{len(eps)} tools · your own account · never metered" if is_oauth
              else f"{len(eps)} tools · from {_esc_html(cheapest)} · $0.000 markup")
    if measured:
        kicker += f" · {_esc_html(measured)}"
    lede = (f"{_esc_html(blurb)} Connect your own {esc_d} account once and your agent uses it "
            "from then on, through one treg.to token. Calls on your own connection are never metered."
            if is_oauth else
            f"{_esc_html(blurb)} {len(eps)} tools for your agent through one treg.to key, priced "
            f"at the provider's own rate{' from ' + _esc_html(cheapest) if cheapest else ''}, with no {esc_d} signup.")
    h1_text = (f"{esc_d}: connect your own account" if is_oauth
               else (f"{esc_d}: {len(eps)} tools from {_esc_html(cheapest)}" if cheapest
                     else f"{esc_d}: {len(eps)} tools"))
    hero = (
        '<div class="hero"><div class="wrap">'
        '<div class="trust" style="margin:0 0 18px"><a href="/">treg.to</a> / '
        '<a href="/catalog">Catalog</a> / ' + esc_d + "</div>"
        f'<div class="kicker">{kicker}</div>'
        f"<h1>{h1_text}</h1>"
        f'<div class="lede">{lede}</div>'
        f'<div class="ctas"><a class="candy" href="/app?ref=tool-{_esc_html(svc)}">Start free</a>'
        f'<a class="ghostbtn" href="#tools">See all {len(eps)} tools</a>'
        + (f'<a class="ghostbtn" href="{_esc_html(docs_url)}" target="_blank" rel="noopener">API docs ↗</a>'
           if docs_url else "") + "</div>"
        '<div class="trust">$1.00 of free credit on every new team · no provider signup · no card</div>'
        + (f'<div class="subline">{verified} of {len(eps)} tools on this page are live-verified '
           "against the provider.</div>" if verified else "")
        + f'<div class="provstrip"><div class="pl">works in</div><div class="ptiles">{_agent_ptiles()}</div></div>'
        "</div></div>")

    flow = (
        '<section id="flow"><div class="wrap"><div class="seclab">What your agent can now do</div>'
        f"<h2>{esc_d}, one prompt away</h2>"
        '<div class="fx">'
        '<div class="fp"><div class="tag">AGENT</div>'
        f'<div class="agrow">{_agent_ptiles()}</div>'
        + "".join(f'<div class="t"><span>{_esc_html(t)}</span><i>✓</i></div>' for t in task_lines)
        + "</div>"
        '<div class="ar">→</div>'
        '<div class="fp dk"><div class="b"><span>▚</span> treg</div>'
        + "".join(
            f'<div class="c">$ treg call <em>{_esc_html(e["id"])}</em></div>'
            f'<div class="cr"><i>✓ 200</i>'
            + (f" · {_esc_html(pl)}"
               if (pl := _price_label(cat.cost_view(e.get("cost"), e.get("provider")))) else "")
            + "</div>"
            for e in demo_eps)
        + '<div class="c" style="opacity:.55">$ _</div></div>'
        '<div class="ar">→</div>'
        '<div class="fp"><div class="ph2">'
        f'<img src="/logos/{_esc_html(svc)}.svg" alt="" aria-hidden="true" '
        'onerror="this.style.display=\'none\'"/>'
        f'<b>{esc_d}</b><span class="bdg">{badge}</span></div>'
        f'<div class="sm">{_esc_html(blurb) or esc_d + " through one treg.to token."}</div>'
        f'<div class="ct">⚒ {len(eps)} TOOLS'
        + ("" if is_oauth else " · metered per call") + "</div></div>"
        "</div>"
        + (f'<p style="font-size:12.5px;color:var(--muted);margin-top:12px">{_esc_html(category)}'
           f"{' · ' if category and base_api else ''}<code>{_esc_html(base_api)}</code></p>"
           if category or base_api else "")
        + "</div></section>")

    setup = (
        '<section id="setup"><div class="wrap"><div class="seclab">Set up</div>'
        f"<h2>Set up {esc_d} in Claude Code, Codex or any agent</h2>"
        '<div class="steplabel"><span class="n">1</span><b>Give this to your agent</b></div>'
        '<div class="promptbox"><div class="ph"><span>in your agent&#x27;s chat</span>'
        f'<button class="copybtn" data-copy="set up treg — {_esc_html(base)}/llms.txt">copy</button></div>'
        f"<pre>set up treg — {_esc_html(base)}/llms.txt</pre></div>"
        '<div class="steplabel"><span class="n">2</span><b>Or add the MCP server yourself</b></div>'
        '<div class="promptbox"><div class="ph"><span>claude code</span>'
        f'<button class="copybtn" data-copy="claude mcp add --transport http treg {_esc_html(base)}/mcp">copy</button></div>'
        f"<pre>claude mcp add --transport http treg {_esc_html(base)}/mcp</pre></div>"
        '<div class="cards">'
        f'<div class="card"><h4>ChatGPT</h4><p>Settings → Connectors → add <code>{_esc_html(base)}/mcp</code> '
        "as a custom connector.</p></div>"
        f'<div class="card"><h4>Grok bot</h4><p>Add <code>{_esc_html(base)}/mcp</code> as a remote MCP '
        "connector in its tool settings.</p></div>"
        f'<div class="card"><h4>Claude Desktop</h4><p>Settings → Connectors → Add custom connector → '
        f"<code>{_esc_html(base)}/mcp</code>.</p></div>"
        f'<div class="card"><h4>Cursor, Codex, any MCP client</h4><p>Point it at '
        f"<code>{_esc_html(base)}/mcp</code> (HTTP transport).</p></div>"
        f'<div class="card"><h4>CLI</h4><p><code>curl -fsSL {_esc_html(base)}/install.sh | sh</code></p></div>'
        '<div class="card"><h4>Plain HTTP</h4><p>LangChain, CrewAI or any code: '
        "<code>/call/&lt;tool-id&gt;</code> with a Bearer token. No SDK.</p></div>"
        "</div></div></section>")

    prompt = (f"Using treg, {task_lines[0]}. Show me the price first." if task_lines
              else f"Using treg, call {display}. Show me the price first.")
    tryit = (
        '<section id="ask"><div class="wrap"><div class="seclab">Try it</div>'
        "<h2>What&#x27;s the best way to ask?</h2>"
        '<div class="promptbox"><div class="ph"><span>the prompt</span>'
        f'<button class="copybtn" data-copy="{_esc_html(prompt)}">copy</button></div>'
        f"<pre>{_esc_html(prompt)}</pre></div>"
        '<h3>Run one directly</h3>'
        '<div class="sample"><div class="sbar">'
        + ("a live-verified call" if sample_eps[0]["verified"] else "a call") + "</div>"
        "<pre>curl -H \"Authorization: Bearer $TREG_TOKEN\" \\\n"
        f"  \"{_esc_html(base)}/call/{_esc_html(sample_id)}\"</pre></div>"
        "</div></section>")

    alt_names = sorted({e["provider"] for e in cat.endpoints
                        if _pub(e) and e["capability"] in cap_counts and e["provider"] != svc})
    why = (
        '<section id="why"><div class="wrap"><div class="seclab">Why treg.to</div>'
        f"<h2>Why call {esc_d} through treg.to</h2>"
        '<div class="cards">'
        + ("<div class=\"card\"><h4>Your account, held safely</h4><p>Connect once; treg.to keeps the "
           "credential server-side and injects it per call. No key on any machine.</p></div>"
           if is_oauth else
           f"<div class=\"card\"><h4>No {esc_d} signup</h4><p>Eligible tools run on treg.to's key, "
           "metered per call from a prepaid balance.</p></div>")
        + '<div class="card"><h4>Price before the call</h4><p>The provider&#x27;s own rate, $0.000 '
        'markup. <a href="/pricing">How billing works</a>.</p></div>'
        '<div class="card"><h4>No subscription, no seats</h4><p>Charged per call. $1.00 free per '
        "new team, no card to start.</p></div>"
        f'<div class="card"><h4>Your own {esc_d} key is free</h4><p>Register it and those calls are '
        "never metered. Your key always wins.</p></div>"
        '<div class="card"><h4>Switch by changing a word</h4><p>Another provider is a different '
        "word in the prompt, not a new integration.</p></div>"
        '<div class="card"><h4>One key, the whole catalog</h4><p>The same token calls '
        + (_esc_html(", ".join(_provider_display(a) for a in alt_names[:3])) if alt_names
           else "every provider in the catalog")
        + f" and {_catalog_census()[0] - len(eps):,} other tools.</p></div>"
        "</div></div></section>")

    tool_blocks = []
    max_shown = 8
    for i, (slug, items) in enumerate(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))):
        lis = []
        shown = sorted(items, key=lambda e: (not e.get("verified"), e["id"]))[:max_shown]
        for e in shown:
            price = _price_label(cat.cost_view(e.get("cost"), e.get("provider")))
            bits = [b for b in ("live-verified" if e.get("verified") else "", _esc_html(price)) if b]
            lis.append(f"<li><b>{_esc_html(e['name'])}</b>"
                       + (f" · <small>{' · '.join(bits)}</small>" if bits else "")
                       + f"<br/><small>{_esc_html(e.get('summary') or '')} "
                         f"<code>{_esc_html(e['id'])}</code></small></li>")
        if len(items) > max_shown:
            lis.append(f'<li class="more"><a href="/catalog/{_esc_html(slug)}">See all {len(items)} '
                       f'{_esc_html(plat_label.get(slug, slug))} tools on the catalog →</a></li>')
        tool_blocks.append(
            f'<details class="tl"{" open" if i == 0 else ""}>'
            f'<summary><a href="/catalog/{_esc_html(slug)}">{_esc_html(plat_label.get(slug, slug))}</a>'
            f" · {len(items)} tools</summary><ul>{''.join(lis)}</ul></details>")
    tools_sec = (
        '<section id="tools"><div class="wrap"><div class="seclab">The shelf</div>'
        f"<h2>All {len(eps)} {esc_d} tools</h2>{''.join(tool_blocks)}"
        '<p style="font-size:12.5px;color:var(--muted)">Reliability badges come from live traffic '
        "through treg.to, not a controlled benchmark.</p></div></section>")

    alt_sec = ""
    if alt_names:
        tiles = "".join(
            f'<a class="ptile" title="{_esc_html(_provider_display(a))}" href="/tools/{_esc_html(a)}">'
            f'<img src="/logos/{_esc_html(a)}.svg" alt="{_esc_html(_provider_display(a))}" '
            'onerror="this.parentNode.style.display=\'none\'"/></a>'
            for a in alt_names[:12])
        alt_sec = (
            '<section id="alts"><div class="wrap"><div class="seclab">Related</div>'
            "<h2>Same jobs, other providers</h2>"
            f"<p>These providers answer some of the same capabilities as {esc_d}. The platform "
            "pages show them on one row with rate and coverage; choosing is yours, treg.to does "
            "not route between providers automatically.</p>"
            f'<div class="provstrip"><div class="pl">also on the catalog</div>'
            f'<div class="ptiles">{tiles}</div></div></div></section>')

    if is_oauth:
        faq_items = [
            (f"Do I need a {display} account?",
             f"Yes. This is an own-account connection: you sign in to {display} once and your "
             "agent uses that connection through your treg.to token. Calls on it are never metered."),
        ]
    else:
        faq_items = [
            (f"Do I need a {display} account?",
             f"No. Eligible tools run on treg.to's key and the call is metered from your team's "
             f"prepaid balance, priced up front. If your team registers its own {display} key, "
             "that key always wins and those calls are never metered."),
            ("What does a call cost?",
             f"Each tool on this page shows its rate{'; the cheapest is ' + cheapest if cheapest else ''}. "
             "The rate is the provider's own and treg.to adds no markup; it is billed per call "
             f"from a prepaid balance. It is not {display}'s subscription pricing, which is on "
             "their own site. New teams start with $1.00 of free credit."),
        ]
    faq_items += [
        (f"How do I add {display} to Claude Code?",
         f"Run: claude mcp add --transport http treg {base}/mcp. One MCP server carries "
         f"{display} and the rest of the catalog."),
        ("Which frameworks does it work with?",
         "Anything that speaks MCP (Claude Code, Claude Desktop, ChatGPT, Codex, Cursor, Grok) "
         "and anything that can make an HTTP request (LangChain, CrewAI, LlamaIndex, plain code)."),
        (f"Is this the official {display} MCP server?",
         f"No. treg.to serves {display}'s real API through its own metered proxy: the request "
         f"is the provider's own, the credential is injected server-side, and the answer is "
         f"relayed verbatim. The official {display} channels are linked above."),
    ]
    faq = ('<section id="faq"><div class="wrap"><div class="seclab">Questions</div>'
           "<h2>Before you start</h2>"
           + "".join(f"<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>" for q, a in faq_items)
           + "</div></section>")

    copy_js = ("<script>document.querySelectorAll('.copybtn').forEach(function(b){"
               "b.addEventListener('click',async function(){try{await navigator.clipboard.writeText("
               "b.dataset.copy);b.textContent='copied';setTimeout(function(){b.textContent='copy'},1400)"
               "}catch(e){}})});</script>")

    # "Used in": the job pages this provider answers. Contextual, intent-matched links from a page
    # Google already indexes into the ones it had never fetched (see `_jobs_by_provider`).
    used_sec = ""
    if used_in:
        used_sec = ('<section id="used-in"><div class="wrap"><div class="seclab">Used in</div>'
                    f"<h2>Jobs {esc_d} does, compared with the other providers</h2>"
                    '<div class="cards">'
                    + "".join(f'<a class="card" href="/use-cases/{_esc_html(js)}"><h3>{_esc_html(jsent)}</h3>'
                              f"<p>Every provider that does this job, side by side: price per billing unit, "
                              f"measured success rate and speed.</p></a>" for js, jsent in used_in)
                    + "</div></div></section>")

    body = _TOOLS_CSS + hero + flow + setup + tryit + why + tools_sec + used_sec + alt_sec + faq + copy_js

    if is_oauth:
        title = f"{display}: connect your own account | treg.to"
        desc = (f"Use {display} from Claude Code, ChatGPT or any MCP agent: {len(eps)} tools "
                "through one treg.to token. Calls on your own connection are never metered.")
    else:
        # The title leads with the pricing intent: Search Console shows "{provider} api pricing" is
        # what reaches these pages ("linkedin api pricing", "1688 api pricing" — the site's one
        # non-brand click), and the number is the part no vendor page prints.
        # `cheapest` already names its unit ("$0.00245/result"), so the title does not say "per
        # call" beside it — a per-result price is not a per-call one.
        # `cheapest` carries its own billing unit ("$0.00245/result", "$0.0089/call"), so the copy
        # never says "per call" next to it: a per-result or per-success rate is not a per-call one.
        title = (f"{display} API pricing: from {cheapest}, no signup | treg.to" if cheapest
                 else f"{display} API pricing, no signup | treg.to")
        if len(title) > _TITLE_MAX:
            title = (f"{display} API pricing: from {cheapest} | treg.to" if cheapest
                     else f"{display} API pricing | treg.to")
        desc = (f"{display} API pricing at the provider's own rate, with no {display} signup: {len(eps)} tools "
                f"{'from ' + cheapest + ' ' if cheapest else ''}through one treg.to key or MCP server"
                f"{', ' + measured if measured else ''}. Use it from Claude Code, ChatGPT or any agent.")

    ld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Catalog", "item": base + "/catalog"},
            {"@type": "ListItem", "position": 3, "name": display,
             "item": f"{base}/tools/{svc}"}]},
        {"@context": "https://schema.org", "@type": "ItemList",
         "name": f"{display} tools on treg.to", "numberOfItems": len(groups),
         "itemListElement": [
             {"@type": "ListItem", "position": i, "name": plat_label.get(sl, sl),
              "url": f"{base}/catalog/{sl}"}
             for i, sl in enumerate(sorted(groups), 1)]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faq_items]},
        {"@context": "https://schema.org", "@type": "HowTo",
         "name": f"Set up {display} for an AI agent via treg.to",
         # The steps mirror the VISIBLE setup section in order — schema that describes a
         # different flow than the page shows is the mismatch Google treats as a violation.
         "step": [
             {"@type": "HowToStep", "position": 1, "name": "Give your agent the setup line",
              "text": f"set up treg — {base}/llms.txt"},
             {"@type": "HowToStep", "position": 2, "name": "Or add the MCP server yourself",
              "text": f"claude mcp add --transport http treg {base}/mcp"},
             {"@type": "HowToStep", "position": 3, "name": f"Call {display}",
              "text": f"Ask your agent, or call {base}/call/{sample_id} over HTTP."}]},
    ]
    return _page(title, _serp_desc(desc), f"/tools/{svc}", body, ld,
                 nav_current="/catalog", css="usecase.css")


_PV_CSS = """<style>
.pv-sub{color:var(--muted,#6b6b66);font-size:16px;max-width:48ch;margin:12px 0 18px}
.pv-ctas{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.pv-btn{border-radius:10px;padding:10px 18px;font-weight:600;font-size:14px;text-decoration:none;display:inline-block}
.pv-btn.p{background:#191917;color:#fff}
.pv-btn.g{border:1px solid var(--line,#e6e6df);background:#fff;color:inherit;font-family:var(--mono,ui-monospace);font-weight:500;font-size:12.5px}
.pv-why{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:14px 0}
.pv-why div{background:#fff;border:1px solid var(--line,#e6e6df);border-radius:12px;padding:14px 16px;font-size:13.5px;color:#4a4a46}
.pv-why b{display:block;margin-bottom:5px;color:#191917;font-size:14px}
</style>"""


@app.get("/pricing", include_in_schema=False)
async def pricing_page():
    """The one canonical answer to "what does treg.to charge" - the page every rate table links,
    so a provider's per-call rate can never be mistaken for treg.to's fees (or for the provider's
    own subscription pricing). Everything here restates commitments that are already public:
    no markup is terms §08, and every number renders from the catalog."""
    base = get_settings().public_url.rstrip("/")
    rows = _platform_rows()
    priced = [r for r in rows if r["price_from"]][:8]
    lis = "".join(
        f'<li><b><a href="/catalog/{_esc_html(r["slug"])}">{_esc_html(r["label"])}</a></b>'
        f' · {r["endpoints"]} tools · from <code>{_esc_html(_price_label(r["price_from"]))}</code></li>'
        for r in priced)
    faq_items = [
        ("Is there a subscription?",
         "No. You top up a prepaid balance and each catalog call is metered against it, priced "
         "before you call. No seats, no monthly minimum. New teams start with $1.00 of free credit."),
        ("Does treg.to add a markup?",
         "No. A metered call is billed at the provider's own rate; adding no markup is a public "
         "commitment in the terms. treg.to is not the provider's pricing page either: providers "
         "sell their own subscriptions on their own sites, and those are linked, not restated."),
        ("What is never metered?",
         "Anything that is yours: calls on your team's own provider keys (your key always wins), "
         "your team's own registered tools and skills, and your own connected accounts (Google "
         "Analytics, Search Console, Google Ads, Business Profile and the rest)."),
        ("What happens when the balance runs out?",
         "Metered calls stop with a clear error until you top up. Calls on your own keys and your "
         "own tools are unaffected."),
        ("How do I see a rate before calling?",
         "Every tool page on this site shows its rate next to the tool, and the catalog API "
         "returns it with the tool's parameters. Rates are stamped with their source and when "
         "they were last checked."),
    ]
    faq = ("<h2>Frequently asked questions</h2>"
           + "".join(f"<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>" for q, a in faq_items))
    body = (f'<main class="wrap">{_PV_CSS}<div class="phead">'
            '<div class="crumbs"><a href="/">treg</a> / pricing</div>'
            "<h1>Pricing</h1>"
            '<p class="pv-sub">A prepaid balance, metered per call at the provider\'s own rate, '
            "with no markup. The first $1.00 is free. Anything that is yours - your keys, your "
            "tools, your connected accounts - is never metered.</p>"
            '<div class="pv-ctas"><a class="pv-btn p" href="/app">Start free — $1.00 credit</a>'
            '<a class="pv-btn g" href="/catalog">Browse the catalog</a></div>'
            '<div class="facts"><span><b>$1.00</b> free to start</span><span><b>0%</b> markup</span>'
            "<span>no seats</span><span>billed per call</span></div></div>"
            '<section class="cat"><div class="prose">'
            "<h2>How a call is billed</h2>"
            '<div class="pv-why">'
            "<div><b>Catalog calls, metered</b>Eligible tools run on treg.to's key and the call is "
            "metered from your prepaid balance at the provider's own rate, shown before you call. "
            "No markup; that promise is in the terms.</div>"
            "<div><b>Your own key always wins</b>Register your team's key for a provider and "
            "treg.to uses it instead. Those calls are never metered.</div>"
            "<div><b>Your own tools and accounts</b>Tools a teammate registered and your own "
            "connected accounts are yours; calls on them are never metered.</div>"
            "<div><b>Runs dry, fails loud</b>When the balance is empty, metered calls stop with a "
            "clear error until you top up. Your own-key calls keep working.</div></div>"
            "<h2>Example rates, by platform</h2>"
            "<p>Rendered from the live catalog; every tool page carries its own rate.</p>"
            f"<ul>{lis}</ul>"
            "<p><small>Rates are each provider's own, metered per call through treg.to. A "
            "provider's subscription pricing lives on its own site.</small></p>"
            f"{faq}</div></section></main>")
    ld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Pricing", "item": base + "/pricing"}]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faq_items]},
    ]
    return _page("Pricing — pay per call, no markup, first $1.00 free | treg.to",
                 "How treg.to charges: a prepaid balance metered per call at the provider's own "
                 "rate with no markup. Your own keys, tools and accounts are never metered.",
                 "/pricing", body, ld)


# ---- the API reference ------------------------------------------------------------------------
# Prose first, because the schema cannot say the load-bearing part: what /call/ actually does. Kept
# short and factual — the tutorial teaches, this page is the reference a reader lands on from search.
_DOCS_INTRO = """
<h2>How a call works</h2>
<p>You make the <b>real upstream request</b> — the provider's own path, its own parameters, its own
response. treg injects the credential server-side and relays the answer verbatim. Nothing here
models a provider's API, which is why an upstream change does not break us and why the caller never
holds a secret.</p>
<pre class="call">curl -H "Authorization: Bearer $TREG_TOKEN" \\
  "{BASE}/call/moz.web.url.metrics"</pre>
<p>Prefix any catalogued endpoint id with <code>/call/</code>. If your team has its own key for that
provider, treg uses it and the call is <b>not metered</b>; otherwise eligible endpoints are served on
treg's key and metered against your prepaid balance at the provider's own rate.</p>

<h2>Finding an endpoint</h2>
<p>Search by what you want to <i>do</i>, not by vendor: <code>GET /catalog/search?q=backlinks</code>.
When several providers can do the same job, <code>/catalog/platforms/{slug}</code> lists them side by
side with measured success rate, speed and price. <b>Choosing is yours</b> — treg compares, but it
does not route between providers automatically and does not fail over.</p>
<p>The whole catalog is also browsable as pages: <a href="/catalog">/catalog</a>.</p>

<h2>Other ways in</h2>
<p><a href="/llms.txt">/llms.txt</a> is the file to point a coding agent at — it teaches the whole
protocol in one fetch. <code>curl -fsSL {BASE}/install.sh | sh</code> installs the CLI. The MCP
endpoint is at <code>{BASE}/mcp</code>. An interactive console for everything below lives at
<a href="/docs/api">/docs/api</a>.</p>

<h2>Endpoints</h2>
<p>Authenticated requests carry <code>Authorization: Bearer &lt;token&gt;</code> (or
<code>X-Treg-Token</code>). The catalog routes are open and need no token.</p>
"""


@app.get("/docs", include_in_schema=False)
async def docs_page():
    """The API reference, rendered server-side from the OpenAPI schema.

    Replaces the stock Swagger UI at this path (now /docs/api), which was a script shell — the
    landing page linked "api" here and a crawler that followed it found an empty document.
    """
    base = get_settings().public_url.rstrip("/")
    schema = app.openapi()

    def rank(path: str) -> tuple:
        """The proxy first, then the catalog, then the rest alphabetically. Sorting purely by path
        opened the reference on /admin/* — super-admin plumbing, and the worst possible first
        impression of the API on a page built to be someone's search result."""
        return (0 if path.startswith("/call/") else 1 if path.startswith("/catalog") else 2, path)

    # Auth travels the same way on every route; naming it on all 135 rows is noise, and the page
    # says it once above. `/admin/*` is super-admin only — still in openapi.json, not advertised here.
    _PLUMBING = {"x-treg-token", "treg_session", "authorization"}
    ops = []
    for path in sorted(schema.get("paths", {}), key=rank):
        if path.startswith("/admin"):
            continue
        for method, op in sorted(schema["paths"][path].items()):
            if method.lower() == "head":     # implied by GET; see `_openapi_without_head`
                continue
            params = ", ".join(p["name"] for p in op.get("parameters", []) or []
                               if p["name"].lower() not in _PLUMBING)
            summary = op.get("summary") or ""
            # FastAPI takes the description from the docstring; only the first paragraph belongs on
            # a reference index, and the rest is written for maintainers rather than callers.
            desc = (op.get("description") or "").strip().split("\n\n")[0].replace("\n", " ")
            ops.append(
                f'<div class="op"><div class="sig"><span class="verb">{_esc_html(method.upper())}</span>'
                f'<code>{_esc_html(path)}</code></div>'
                + (f"<p>{_esc_html(summary or desc)}</p>" if (summary or desc) else "")
                + (f'<div class="params">{_esc_html(params)}</div>' if params else "")
                + "</div>")

    body = f"""<main class="wrap">
<div class="phead">
  <div class="crumbs"><a href="/">treg</a> / api</div>
  <h1>API reference</h1>
  <p class="lede">One base URL, one token. Call any of {len(ops)} documented operations, or proxy a
  real request to any of 2,630 catalogued provider endpoints through <code>/call/</code>.</p>
  <div class="facts">
    <span>base <b>{_esc_html(base)}</b></span>
    <span><b>Bearer</b> token auth</span>
    <span><a href="/openapi.json">openapi.json</a></span>
    <span><a href="/docs/api">interactive console</a></span>
  </div>
</div>
<section class="cat">
  <div class="prose">{_DOCS_INTRO.replace("{BASE}", _esc_html(base))}</div>
  {"".join(ops)}
</section>
</main>"""
    ld = [{"@context": "https://schema.org", "@type": "TechArticle",
           "headline": "treg API reference",
           "description": "How to call 2,630 provider API endpoints through one treg token.",
           "url": f"{base}/docs"}]
    return _page("API reference — call any tool through one endpoint | treg",
                 "The treg HTTP API: proxy a real request to any of 2,630 catalogued provider "
                 "endpoints through /call/, with the credential injected server-side. Plus the "
                 "catalog, org, billing and tool-management routes.",
                 "/docs", body, ld, nav_current="/docs")


site_router = APIRouter()
app = site_router


def _esc_html(s: str) -> str:
    """The stdlib escaper, not a hand-rolled replace() chain.

    Same four substitutions as before plus `'` -> `&#x27;`, so every call site is at least as safe.
    The reason to delegate is not correctness but legibility to tooling: static analysis models
    `html.escape` as an XSS sanitizer and cannot know that a private chain of `.replace()` calls is
    one, so every escaped value stayed 'tainted' and the real sinks were buried in false positives.
    """
    return _html.escape(str(s), quote=True)


@app.get("/", include_in_schema=False)
async def landing(request: Request, treg_session: str = Cookie(default=""),
                  db: AsyncSession = Depends(get_session)):
    """Serve the marketing landing at the root. Any query string (invite links, OAuth returns,
    tour deep-links) belongs to the SPA, so those requests fall through to the dashboard —
    the landing is only the clean, parameterless front door. A signed-in visitor belongs on
    the dashboard, so a live session redirects to /app instead of re-showing the pitch.

    `?ref=<code>` is the ONE exception, and it has to be: a referral link's whole job is to show a
    stranger the pitch. Falling through to the SPA would send someone who has never heard of treg
    to an empty dashboard shell — so a lone `ref` counts as parameterless, and the code is parked in
    a cookie on the way past. It is only redeemed much later, when they create their first team.
    """
    page = _WEB_DIR / "landing.html"
    ref = referrals.normalize_code(request.query_params.get("ref", ""))
    # Only `ref` may be present. Anything else alongside it belongs to the SPA, and a referral code
    # is not a reason to hijack an invite or an OAuth return.
    ref_only = set(request.query_params.keys()) <= {"ref"}
    if page.exists() and (not request.query_params or (ref and ref_only)):
        if treg_session and await _user_from_session(treg_session, db):
            return RedirectResponse("/app", status_code=302)
        # Read-and-substitute rather than a bare FileResponse: the canonical, og:url and og:image
        # are `{BASE}`-templated so they name the serving host. Hardcoded, a self-hosted registry
        # would tell crawlers its front page really lives on treg.to.
        html = page.read_text(encoding="utf-8").replace(
            "{BASE}", get_settings().public_url.rstrip("/"))
        # The footer's hub links point at hosted-only pages; a self-hosted landing drops them.
        if not _hosted():
            html = re.sub(r"<!--hosted-->.*?<!--/hosted-->", "", html, flags=re.S)
        resp = HTMLResponse(html, headers={"Cache-Control": "no-cache"})
        if ref:
            _remember_referral(resp, request, ref)
        return resp
    return await dashboard(request, treg_session, db)


@app.get("/app", include_in_schema=False)
async def dashboard(
    request: Request, treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
):
    """Serve the single-file dashboard (same-origin, so it calls this API directly).

    Also the place a parked OAuth authorization resumes. Every browser sign-in door — GitHub, Google,
    the email code — ends here, so honouring the cookie at this ONE point covers all of them, rather
    than threading a return value through five handlers that each finish differently (two redirect,
    one answers JSON).

    In frictionless local mode the dashboard opens ALREADY SIGNED IN: with no valid session we
    attach one for the machine's single user, so `curl … | sh` reaches a working dashboard without
    an account. Only reachable when `single_user_ok` holds (local sqlite + loopback URL), so this
    can never hand a session to a stranger on a real deploy.
    """
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    signed_in = await _user_from_session(treg_session, db)
    # A parked authorization resumes here, but ONLY once the user is actually signed in — otherwise
    # this would bounce them back to /oauth/authorize, which would bounce them here again.
    if signed_in and (parked := _take_oauth_return(request)) is not None:
        resume = RedirectResponse(parked, status_code=302)
        resume.delete_cookie(OAUTH_RETURN_COOKIE)
        return resume
    resp = FileResponse(index, headers={"Cache-Control": "no-cache"})
    if not signed_in:
        owner = await _local_owner(db)
        if owner is not None:
            resp.set_cookie(sess.COOKIE, sess.make(owner.id, token_version=owner.token_version),
                            httponly=True, samesite="lax",
                            secure=_is_https(request),
                            max_age=sess.TTL_SECONDS)
    return resp


def _spa_with_og(kind: str, name: str):
    """Serve the SPA at a shareable detail path (/app/skills/x, /app/tools/x) with per-resource
    og/twitter meta so link unfurls show what was shared. The meta echoes only the URL's own
    name segment — no DB read, so an unauthenticated crawler learns nothing it didn't send."""
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    label = "skill" if kind == "skills" else "tool"
    safe = _esc_html(name)
    meta = (
        f"<title>{safe} · Treg</title>\n"
        f'<meta property="og:title" content="{safe} — shared {label}"/>\n'
        f'<meta property="og:description" content="A {label} shared via Treg. '
        f'Sign in to preview it and get the one-command install."/>\n'
        f'<meta name="twitter:card" content="summary"/>'
    )
    # Match WHATEVER title the page carries, not one exact string. It was pinned to
    # `<title>tools-registry</title>`, the page says `<title>treg</title>`, so the replacement
    # silently did nothing and every shared link unfurled blank — a rename in the dashboard must
    # not be able to switch this off without a word.
    html, hits = re.subn(r"<title>.*?</title>", lambda _m: meta, index.read_text(encoding="utf-8"),
                         count=1, flags=re.IGNORECASE | re.DOTALL)
    if not hits:  # no title at all: still emit the meta rather than serve a bare page
        html = html.replace("<head>", "<head>\n" + meta, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/app/marketplace/{service}", include_in_schema=False)
async def dashboard_marketplace(
    service: str, request: Request, treg_session: str = Cookie(default=""),  # noqa: ARG001 — the SPA reads the path itself
    db: AsyncSession = Depends(get_session),
):
    """One integration's page. Served as the plain SPA: unlike /app/skills/<x> there is no og meta
    to add, because this view is only meaningful to a signed-in member of the org. A signed-out
    visitor is sent to the provider's PUBLIC page instead — /tools/<service> is the same subject
    with the member actions replaced by sign-in CTAs (and it is the URL crawlers get)."""
    if not treg_session:
        # Redirect on the CATALOG's spelling of the provider, never the request's: an unknown
        # service 404s here rather than bouncing into a 404, and the redirect target is a value
        # we own (which is also what keeps this off CodeQL's url-redirection list).
        known = next((r["service"] for r in _provider_rows() if r["service"] == service), None)
        if known is None:
            raise HTTPException(status_code=404, detail=f"unknown provider {service!r}")
        return RedirectResponse(f"/tools/{known}", status_code=302)
    return await dashboard(request, treg_session, db)


@app.get("/app/skills/{name}", include_in_schema=False)
async def dashboard_skill_page(name: str):
    return _spa_with_og("skills", name)


@app.get("/app/tools/{name}", include_in_schema=False)
async def dashboard_tool_page(name: str):
    return _spa_with_og("tools", name)


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    """Agent-readable overview (llms.txt convention) — an AI agent that fetches this learns the
    whole registry: the call protocol, discovery, auth, CLI, skills, and links to the tutorial/docs.
    The serving domain is templated in so links stay correct across deploys."""
    f = _WEB_DIR / "llms.txt"
    if not f.exists():
        raise HTTPException(status_code=404, detail="llms.txt not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(_strip_routed(f.read_text(encoding="utf-8")).replace("{BASE}", base),
                             media_type="text/plain; charset=utf-8")


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """Crawler policy. `{BASE}`-templated like llms.txt, so a self-hosted registry advertises its own
    sitemap rather than treg.to's."""
    f = _WEB_DIR / "robots.txt"
    if not f.exists():
        raise HTTPException(status_code=404, detail="robots.txt not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "max-age=3600"})


# The outcome landing pages: one per vertical, the destinations for search ads and the organic
# `/use-cases/` cluster. Their COPY is generated from marketing/landing/*.md — never hand-edit
# the HTML in web/, it is overwritten by that build. The slug is the public URL and is quoted in
# live ad campaigns, so treat this map as an API: add freely, never rename or remove without a
# redirect.
_USE_CASES = {
    "seo-data-for-ai-agents": "usecase-seo.html",
    "lead-enrichment-for-ai-agents": "usecase-enrichment.html",
    "social-trend-research-for-ai-agents": "usecase-social.html",
    "competitor-ad-research-for-ai-agents": "usecase-ads.html",
    "company-research-for-ai-agents": "usecase-company.html",
}


# The pages a crawler should know about. Everything here must answer 200 to a GET — a sitemap that
# lists a redirect or a 404 is worse than no sitemap, so `tests/test_seo.py` walks every entry.
# Deliberately absent: /contact and /help (alias URLs for the one support.html), /vendor-listing.md
# (the text/plain twin of /vendor-listing), /login (302s to /app), /app* (authenticated SPA),
# /connect-demo (noindex by design), and the shell installers.
_SITEMAP_PAGES: tuple[tuple[str, str, str], ...] = (
    # (path, source file for lastmod — "" means use the catalog's, priority)
    ("/", "landing.html", "1.0"),
    ("/catalog", "", "0.9"),
    ("/pricing", "", "0.8"),
    ("/tutorial", "tutorial.html", "0.8"),
    ("/docs", "", "0.7"),
    ("/resources", "resources.html", "0.8"),
    ("/vendor-listing", "vendor-listing.md", "0.5"),
    ("/support", "support.html", "0.4"),
    ("/connectors/claude", "claude-connector.html", "0.6"),
    ("/people-search", "people-search.html", "0.8"),
    ("/grokbot", "grokbot.html", "0.8"),
    ("/fable", "fable-gtm.html", "0.8"),
    ("/terms", "terms.html", "0.2"),
    ("/privacy", "privacy.html", "0.2"),
    # The outcome pages. Listed WITHOUT a trailing slash on purpose: `/use-cases/<slug>/` 307s to
    # this form, and a sitemap that lists a redirect is worse than no sitemap. Their canonical tags
    # match these exactly. `_USE_CASES` is the one source for the set, so a new page is listed the
    # moment it is routed, and `tests/test_seo.py` will fail if one stops answering 200.
    *(
        (f"/use-cases/{slug}", name, "0.8")
        for slug, name in _USE_CASES.items()
    ),
)


@lru_cache(maxsize=1)
def _catalog_mtime() -> str:
    """The newest mtime under the catalog directory, as a sitemap `lastmod` date. The catalog is
    read-only and changes only on deploy, so one scan per process is enough."""
    newest = 0.0
    for f in (Path(catalog_store.__file__).parent / "catalog").rglob("*.yaml"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:  # noqa: PERF203 -- a file vanishing mid-scan is not worth failing the sitemap
            continue
    return _iso_day(newest)


def _iso_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else ""


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    """Generated, not bundled: 80 of its URLs are the catalog's platform shelves, which move with the
    catalog rather than with a checked-in file. Every URL is absolute on `public_url` so a self-host
    publishes its own pages, and so the copy served on a legacy host still names the canonical one."""
    base = get_settings().public_url.rstrip("/")
    cat_day = _catalog_mtime()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']

    def add(path: str, lastmod: str, priority: str) -> None:
        out.append("<url>")
        out.append(f"<loc>{_esc_html(base + path)}</loc>")
        if lastmod:
            out.append(f"<lastmod>{lastmod}</lastmod>")
        out.append(f"<priority>{priority}</priority>")
        out.append("</url>")

    for path, src, priority in _SITEMAP_PAGES:
        day = cat_day
        if src:
            f = _WEB_DIR / src
            day = _iso_day(f.stat().st_mtime) if f.exists() else ""
        add(path, day, priority)
    for row in _platform_rows():
        add(f"/catalog/{row['slug']}", cat_day, "0.6")
    for prow in _provider_rows():
        add(f"/tools/{prow['service']}", cat_day, "0.5")
    # The agent pages exist only on the hosted deployment (see `_hosted`); their lastmod follows the
    # hand-written copy, which is what changes between deploys.
    if _hosted():
        copy_day = _iso_day(Path(agent_pages.__file__).stat().st_mtime)
        add("/agents", copy_day, "0.8")
        for slug in agent_pages.AGENTS:
            add(f"/agents/{slug}", copy_day, "0.8")
        add("/use-cases", copy_day, "0.8")
        for j in agent_pages.USE_CASE_PAGES:
            add(f"/use-cases/{j}", copy_day, "0.7")
        add("/workflows", copy_day, "0.8")
        for w in agent_pages.WORKFLOWS:
            add(f"/workflows/{w}", copy_day, "0.7")
    out.append("</urlset>")
    return Response("\n".join(out), media_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": "max-age=3600"})


# IndexNow (Bing, Yandex, Seznam, Naver share the feed): the key file the protocol needs on this
# host. Not a secret — the protocol only checks that the key named in a submission is served from
# the same host, so a submission cannot claim someone else's URLs. `scripts/indexnow_submit.py`
# pushes every sitemap URL through it after a deploy.
INDEXNOW_KEY = "7c2e4a91b5d3f8e6treg2026"


@app.get(f"/{INDEXNOW_KEY}.txt", include_in_schema=False)
async def indexnow_key():
    return Response(INDEXNOW_KEY, media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/install.sh", include_in_schema=False)
async def install_sh():
    """`curl -fsSL {BASE}/install.sh | sh` — installs the treg CLI and points it at this server.
    The serving domain is templated in so it targets whichever host is live (dev box or the real
    domain after deploy)."""
    f = _WEB_DIR / "install.sh"
    if not f.exists():
        raise HTTPException(status_code=404, detail="install.sh not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base), media_type="text/x-shellscript; charset=utf-8")


@app.get("/selfhost.sh", include_in_schema=False)
async def selfhost_sh():
    """`curl -fsSL {BASE}/selfhost.sh | sh` — run your OWN registry locally, with no account.

    Different from install.sh, which only installs the CLI and points it at THIS server. This one
    brings up a server on the caller's machine in single-user mode, so they land on a dashboard that
    is already signed in. Value first, account later."""
    f = _WEB_DIR / "selfhost.sh"
    if not f.exists():
        raise HTTPException(status_code=404, detail="selfhost.sh not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/x-shellscript; charset=utf-8")


def routed_discovery_on() -> bool:
    """`TREG_ROUTED_DISCOVERY` — see catalog_store.group_routed. The agent-facing files honour it
    too: a deployment that hides routed rows from search must not keep TEACHING agents to call
    them, or the docs and the catalog disagree and the agent trusts the docs."""
    return str(get_settings().routed_discovery).strip().lower() not in ("off", "0", "false", "no")


def _strip_routed(text: str) -> str:
    """Remove the `<!--routed-->…<!--/routed-->` blocks (and, when kept, just the markers)."""
    if routed_discovery_on():
        return text.replace("<!--routed-->\n", "").replace("\n<!--/routed-->", "")
    return re.sub(r"<!--routed-->.*?<!--/routed-->\n?", "", text, flags=re.S)


def _serve_md(name: str) -> PlainTextResponse:
    """Serve a bundled markdown file as inline text (so "open in new tab" shows it, not a download),
    with the serving domain templated in. Backs the 'copy markdown' buttons on the docs pages."""
    f = _WEB_DIR / name
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(_strip_routed(f.read_text(encoding="utf-8")).replace("{BASE}", base),
                             media_type="text/plain; charset=utf-8")


@app.get("/quickstart.md", include_in_schema=False)
async def quickstart_md():
    """The quick-start as raw markdown — copy it or open it in a tab and use it anywhere."""
    return _serve_md("quickstart.md")


@app.get("/tutorial.md", include_in_schema=False)
async def tutorial_md():
    """The full tutorial as raw markdown (mirrors the interactive /tutorial)."""
    return _serve_md("tutorial.md")


@app.get("/tutorial-import-shell.md", include_in_schema=False)
async def tutorial_import_shell_md():
    """Focused tutorial: CLI auto-import (`treg upload clis`) + shell mode (`treg shell`) + the
    local-run security sandbox. Linked from the main tutorial."""
    return _serve_md("tutorial-import-shell.md")


@app.get("/tutorial-access.md", include_in_schema=False)
async def tutorial_access_md():
    """Focused tutorial: per-member team access control (which tools a member may use + the local-run
    toggle). Linked from the main tutorial."""
    return _serve_md("tutorial-access.md")


@app.get("/vendor-listing", include_in_schema=False)
@app.get("/vendor-listing.md", include_in_schema=False)
async def vendor_listing_md(request: Request):
    """Vendor listing instructions — what a vendor's coding agent reads before raising a PR that
    adds their API to the catalog. Linked from the dashboard's "List your API" modal."""
    resp = _serve_md("vendor-listing.md")
    # Two URLs, one document. `text/plain` cannot carry a <link rel=canonical>, so the duplicate is
    # suppressed with the header equivalent: /vendor-listing is the indexed one (it is what the
    # sitemap lists), /vendor-listing.md keeps serving agents and stays out of the index.
    if request.url.path.endswith(".md"):
        resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.get("/integrate.md", include_in_schema=False)
async def integrate_md():
    """The BUILDER skill: how to put treg inside your own product and bill your own customers for it.

    Distinct from `skill.md`, which teaches an agent to USE treg. This one is pasted into a builder's
    repo and pointed at their coding agent, so it leads with the per-customer billing model — the
    part that changes how the plumbing is written, and therefore has to be read before any of it is.
    """
    return _serve_md("integrate.md")


@app.get("/skill.md", include_in_schema=False)
async def skill_md():
    """The OFFICIAL treg Claude skill (3 personas), {BASE}-templated to this server.
    install.sh drops it into ~/.claude/skills/treg/ so agents learn treg at CLI install."""
    return _serve_md("skill.md")


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """The ▚ brand mark. Served at both paths so browsers that auto-request /favicon.ico stop 404ing."""
    ico = _WEB_DIR / "favicon.svg"
    if not ico.exists():
        raise HTTPException(status_code=404, detail="favicon not bundled")
    return FileResponse(ico, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/tutorial.js", include_in_schema=False)
async def tutorial_js():
    """The shared interactive-tutorial data + highlighter (window.TREG_TUTORIAL / tregHL).
    Loaded by both the dashboard Help view and the standalone tutorial page, so they never drift."""
    js = _WEB_DIR / "tutorial.js"
    if not js.exists():
        raise HTTPException(status_code=404, detail="tutorial.js not bundled")
    # no-cache, like index.html and the landing: the page includes this as a bare `<script
    # src="/tutorial.js">` with no version query, so without the header a browser keeps serving the
    # tutorial from before the last deploy until someone hard-refreshes.
    return FileResponse(js, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/legal.css", include_in_schema=False)
async def legal_css():
    """The shared skin for /terms and /privacy (landing-page tokens, one copy)."""
    f = _WEB_DIR / "legal.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="legal.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "no-cache"})


def _legal_page(name: str) -> HTMLResponse:
    page = _WEB_DIR / name
    if not page.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    # `{BASE}`-substituted rather than sent as a plain FileResponse, so each page's canonical and
    # og:url name the host actually serving it. A hardcoded treg.to would tell a self-hosted
    # registry's crawler that the real page lives on someone else's domain.
    base = get_settings().public_url.rstrip("/")
    html = page.read_text(encoding="utf-8").replace("{BASE}", base)
    # no-cache: a legal page must not be served stale after we publish an update.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/terms", include_in_schema=False)
async def terms_page():
    """Terms of Service for the HOSTED registry (self-hosted instances are governed by LICENSE)."""
    return _legal_page("terms.html")


@app.get("/privacy", include_in_schema=False)
async def privacy_page():
    """Privacy policy. Also the URL given to OAuth providers at app-registration/verification time
    (Google requires a reachable privacy policy carrying the Limited Use disclosure), so this path
    is effectively public API — don't rename it without updating the provider consoles."""
    return _legal_page("privacy.html")


@app.get("/connectors/claude", include_in_schema=False)
async def claude_connector_page():
    """Setup, scope, billing, privacy, and removal instructions for the Claude connector."""
    return _legal_page("claude-connector.html")


@app.get("/adtrack.js", include_in_schema=False)
async def adtrack_js():
    """First-party ad-click capture (see the file itself): sets the `treg_ad` cookie that
    `_ad_attribution_from` reads at signup. No Google script, no third-party request."""
    headers = {"Cache-Control": "no-cache"}
    if not adsconv.enabled():
        # The page keeps one static script include, but an unconfigured/self-hosted deployment must
        # not collect an advertising cookie at all. A stale cookie is also ignored at signup below.
        return Response(content="", media_type="application/javascript", headers=headers)
    f = _WEB_DIR / "adtrack.js"
    if not f.exists():
        raise HTTPException(status_code=404, detail="adtrack.js not bundled")
    # no-cache, same reasoning as tutorial.js: served as a bare `<script src="/adtrack.js">` with no
    # version query, so without this header a browser would keep an ad-window-stale copy after a fix.
    return FileResponse(f, media_type="application/javascript", headers=headers)


@app.get("/gtag.js", include_in_schema=False)
async def gtag_js():
    """Google Ads tag for CLIENT-SIDE website conversion tracking. Exposes `tregSignupConversion()`
    for the dashboard to call on new-account signup success.

    This is the **website** conversion (action 7745505287 "treg Signup (web)") that fires to the
    Ads UI SIGNUP goal. It is NOT a duplicate of the server-side adsconv upload conversions, which
    go to different action IDs (signup 7723667014, first_call 7723667017, paid 7723667020) via the
    Data Manager API. Both coexist: client gives Google a real-time website signal; server uploads
    durable attributed conversions for bidding."""
    headers = {"Cache-Control": "no-cache"}
    if not adsconv.enabled():
        return Response(content="", media_type="application/javascript", headers=headers)
    f = _WEB_DIR / "gtag.js"
    if not f.exists():
        raise HTTPException(status_code=404, detail="gtag.js not bundled")
    return FileResponse(f, media_type="application/javascript", headers=headers)


@app.get("/sitetrack.js", include_in_schema=False)
async def sitetrack_js():
    """First-touch traffic-source capture (`treg_utm` cookie, always on — first-party, no PII) plus
    the PostHog bootstrap, with the public project key templated in. Loaded by every public page
    so the visitor's FIRST hop — the one carrying `utm_*` and the referrer — is the one analytics
    sees; before this the landing page loaded nothing and every signup looked `$direct`. With no
    `TREG_POSTHOG_KEY` the analytics half is inert (empty key) and only the cookie half runs."""
    f = _WEB_DIR / "sitetrack.js"
    if not f.exists():
        raise HTTPException(status_code=404, detail="sitetrack.js not bundled")
    s = get_settings()
    js = (f.read_text(encoding="utf-8")
          .replace("{POSTHOG_KEY}", s.posthog_key if s.posthog_key else "")
          .replace("{POSTHOG_HOST}", s.posthog_host.rstrip("/") if s.posthog_key else ""))
    # no-cache, same reasoning as adtrack.js: a bare `<script src>` with no version query.
    return Response(content=js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/grokbot", include_in_schema=False)
async def grokbot_page():
    """Landing page for the treg plugin inside Grok Bot ("Grok Bot for Outreach") — a scroll
    animatic of the Bot working a lead list through treg. Indexed like /people-search: canonical,
    OG meta, listed in the sitemap."""
    page = _WEB_DIR / "grokbot.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="grokbot.html not bundled")
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/fable", include_in_schema=False)
async def fable_page():
    """Landing page for the Claude Fable 5.1 + treg launch ("Run your GTM from the terminal"):
    one prompt, the market read, four agents, four results, then the catalog and the bill.
    Indexed like /grokbot: canonical, in the sitemap, no-cache so edits land on refresh."""
    page = _WEB_DIR / "fable-gtm.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="fable-gtm.html not bundled")
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/people-search", include_in_schema=False)
async def people_search_page():
    """Landing page for the people-search launch ("Claude for people search") — the destination the
    launch film points viewers at. A first-class page: canonical, in the sitemap, indexed. Its asset
    paths are RELATIVE (media/…, logos/…) so the same file previews from file:// — which only works
    while this route stays slashless; a /people-search/ variant would re-root them."""
    page = _WEB_DIR / "people-search.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="people-search.html not bundled")
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/resources", include_in_schema=False)
async def resources_page():
    """The hub for the outcome pages. It exists for two reasons beyond navigation: without it the
    `/use-cases/*` pages are orphans that no crawler reaches, and it gives the footer one durable
    link instead of five that grow every time a page is added."""
    page = _WEB_DIR / "resources.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="resources.html not bundled")
    # Read-and-substitute for {BASE} templating like the use-case pages.
    base = get_settings().public_url.rstrip("/")
    content = page.read_text(encoding="utf-8").replace("{BASE}", base)
    return HTMLResponse(content, headers={"Cache-Control": "no-cache"})


@app.get("/usecase.css", include_in_schema=False)
async def usecase_css():
    """The shared skin for /use-cases/* (landing-page tokens, one copy — same deal as legal.css)."""
    f = _WEB_DIR / "usecase.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="usecase.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "no-cache"})




public_docs_router = APIRouter()
app = public_docs_router


def _skill_frontmatter() -> dict[str, str]:
    """The bundled skill's frontmatter, read at request time rather than duplicated in code — the
    description is what drives discovery in every registry, and a second copy of it would drift."""
    f = _WEB_DIR / "skill.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail="skill.md not bundled")
    text = f.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise HTTPException(status_code=404, detail="skill.md has no frontmatter")
    out: dict[str, str] = {}
    for line in text.split("---", 2)[1].strip().splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


@app.get("/.well-known/skills/index.json", include_in_schema=False)
async def well_known_skills_index():
    """Advertise treg's own skill under the agentskills.io well-known convention.

    This makes THIS host a first-class skill source: an agent that supports the standard can install
    treg from treg.to directly, with no directory, no review queue and no third party in the middle
    — the same skill the plugins ship and `install.sh` drops, reached by whoever asks the domain.
    """
    fm = _skill_frontmatter()
    return JSONResponse({"skills": [{
        "name": fm.get("name", "treg"),
        "description": fm.get("description", ""),
        "files": ["SKILL.md"],
    }]})


@app.get("/.well-known/skills/treg/SKILL.md", include_in_schema=False)
async def well_known_skill_md():
    """The skill itself, at the path `index.json` promises. Deliberately the same `_serve_md` the
    canonical `/skill.md` uses, so `{BASE}` is templated to the serving host here too — a self-hosted
    registry advertises ITSELF, not treg.to."""
    return _serve_md("skill.md")


@app.get("/connect-demo", include_in_schema=False)
async def connect_demo_page():
    """A page that PRETENDS to be someone else's app, so the OAuth flow can be seen end to end.

    It uses only public endpoints — register, authorize, token, revoke, and /mcp/ — with nothing
    privileged about being served from treg's own domain. The point is to watch the whole dance in a
    browser before trusting it inside ChatGPT, where a failure surfaces as a shrug rather than an
    error message.
    """
    if not get_settings().connect_demo_enabled:
        raise HTTPException(status_code=404, detail="connect demo is not enabled")
    return _legal_page("connect-demo.html")


@app.get("/connect-demo/callback", include_in_schema=False)
async def connect_demo_callback():
    """Where treg sends the browser back. Hands the code to the opener and closes."""
    if not get_settings().connect_demo_enabled:
        raise HTTPException(status_code=404, detail="connect demo is not enabled")
    return _legal_page("connect-demo-callback.html")


@app.get("/support", include_in_schema=False)
@app.get("/contact", include_in_schema=False)
@app.get("/help", include_in_schema=False)
async def support_page():
    """How to get help. Three paths for one page because people guess differently, and because a
    plugin-directory listing must give a Support URL that resolves — a 404 there reads as an
    abandoned product. Like `/privacy`, this path is effectively public API once it is filed with a
    directory or an OAuth console: don't rename it without updating them."""
    return _legal_page("support.html")


@app.get("/tutorial", include_in_schema=False)
async def tutorial_page():
    """Standalone shareable interactive tutorial (same STEPS[] as the dashboard Help view)."""
    page = _WEB_DIR / "tutorial.html"
    if not page.exists():
        return HTMLResponse("<h3>Tutorial not bundled.</h3>")
    # Stamp the tutorial.js URL with the bundle version. `no-cache` alone is not enough: a browser
    # that cached the file BEFORE that header existed applies a heuristic lifetime and never
    # revalidates, so an edited tutorial silently keeps serving the old steps (cost an hour to find).
    js = _WEB_DIR / "tutorial.js"
    stamp = int(js.stat().st_mtime) if js.exists() else 0   # tutorial.js's OWN mtime: _app_version()
    html = page.read_text(encoding="utf-8").replace(       # hashes index.html and would not move
        'src="/tutorial.js"', f'src="/tutorial.js?v={stamp}"')
    html = html.replace("{BASE}", get_settings().public_url.rstrip("/"))  # canonical + og:url
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# Provider logos, resolved by convention: /logos/<service>.svg, matching `service` in
# oauth_providers.py. Keyed off the name the registry already has, so adding a provider needs no
# second registration step — drop the file in and it appears. Public and unauthenticated: they are
# brand marks, not data, and the dashboard renders them before the caller is known.
_LOGO_DIR = _WEB_DIR / "logos"


# Demo recordings — the plugin-directory submission requires a publicly reachable video URL, and
# hosting it ourselves means no third-party account decides whether reviewers can watch it.
_MEDIA_DIR = _WEB_DIR / "media"


# The interactive dashboard tour (matted screenshots) — served + its WebP images, at /dashboard-tour/.
_TOUR_DIR = _WEB_DIR / "tour"


# Third-party front-end libraries, vendored rather than pulled from a CDN at page load. The
# dashboard is a single hand-written Vue file with no bundler, so Vue arrives as a plain <script>
# — and while that script came from unpkg.com, any network that cannot reach unpkg rendered the
# signed-in dashboard as a blank page (issue #137: a mainland-China visitor, ERR_CONNECTION_CLOSED,
# then `Vue is not defined`). Serving it ourselves means the dashboard depends on exactly one
# origin: whoever served the page can serve its runtime. It also closes the supply-chain hole in
# the old floating `vue@3` tag, which let whatever npm published next run in an authed session.
# Filenames carry their version, so a bump is a visible one-line change and caches never collide.
_VENDOR_DIR = _WEB_DIR / "vendor"
