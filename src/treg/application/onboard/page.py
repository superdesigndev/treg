"""Read a company's homepage directly: its title, description, keywords and visible text, the tools
its code loads, the ad pixels on it and its square icon.

A homepage is what grounds a new user's first tasks, and a direct read takes about a second where
the catalog's page readers can take most of the setup's time budget. Every hop of the fetch passes
the call-time SSRF check (`infra.upstream.ssrf.host_is_public`), redirects are followed by hand so
each target is checked, and the body is bounded. A page that is mostly script (a client-rendered
app) yields little text; the caller then asks the catalog.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from ...infra.upstream import ssrf

MAX_BYTES = 600_000
HOPS = 4
_SKIP = {"script", "style", "noscript", "svg", "template"}
_UA = "Mozilla/5.0 (compatible; treg-onboarding; +https://treg.to)"

# What a site's own code says it runs on, matched in the raw HTML: a script host, an asset path or a
# generator tag. Only tools a marketer would name; a JavaScript framework is not a tool they use.
TOOLS: tuple[tuple[str, str], ...] = (
    ("Shopify", r"cdn\.shopify\.com|Shopify\.theme"),
    ("WordPress", r"/wp-content/|<meta[^>]+generator[^>]+WordPress"),
    ("Webflow", r"data-wf-site=|<meta[^>]+generator[^>]+Webflow"),
    ("Framer", r"framerusercontent\.com|<meta[^>]+generator[^>]+Framer"),
    ("Wix", r"static\.wixstatic\.com"),
    ("Squarespace", r"static1\.squarespace\.com"),
    ("HubSpot", r"js\.hs-scripts\.com|js\.hsforms\.net|js\.hs-analytics\.net"),
    ("Klaviyo", r"static\.klaviyo\.com"),
    ("Mailchimp", r"chimpstatic\.com|list-manage\.com"),
    ("Intercom", r"widget\.intercom\.io|intercomSettings"),
    ("Crisp", r"client\.crisp\.chat"),
    ("Zendesk", r"static\.zdassets\.com"),
    ("Calendly", r"assets\.calendly\.com"),
    ("Stripe", r"js\.stripe\.com"),
    ("Segment", r"cdn\.segment\.com"),
    ("Google Analytics", r"googletagmanager\.com/gtag/js\?id=G-|google-analytics\.com/(?:analytics|ga)\.js"),
    ("Google Tag Manager", r"googletagmanager\.com/gtm\.js"),
    ("PostHog", r"(?:us|eu)(?:-assets)?\.i\.posthog\.com|posthog\.init\("),
    ("Mixpanel", r"cdn\.mxpnl\.com"),
    ("Hotjar", r"static\.hotjar\.com"),
    ("Plausible", r"plausible\.io/js"),
)
# An ad platform's pixel on the site: the company measures (so most likely runs) ads there.
PIXELS: tuple[tuple[str, str], ...] = (
    ("Meta", r"connect\.facebook\.net/[^\"']*/fbevents\.js|fbq\(\s*['\"]init"),
    ("Google Ads", r"googleadservices\.com|gtag/js\?id=AW-|['\"]AW-\d{6,}"),
    ("TikTok", r"analytics\.tiktok\.com"),
    ("LinkedIn", r"snap\.licdn\.com"),
    ("X", r"static\.ads-twitter\.com"),
    ("Reddit", r"redditstatic\.com/ads"),
    ("Pinterest", r"s\.pinimg\.com/ct/"),
)


@dataclass
class Page:
    text: str = ""                                   # what the LLM reads: title, meta, visible text
    tools: list[str] = field(default_factory=list)  # TOOLS found in the code
    pixels: list[str] = field(default_factory=list) # PIXELS found in the code
    icon: str = ""                                   # the square icon (apple-touch-icon, else a large icon)
    plain_icon: str = ""                             # else a favicon of unknown size, still to be measured


def found(html: str, table: tuple[tuple[str, str], ...]) -> list[str]:
    return [name for name, pat in table if re.search(pat, html, re.I)]


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.text, self._skip, self._in_title = "", {}, [], 0, False
        self.icons: list[tuple[int, str]] = []   # (how good, href)
        self.plain: str = ""                       # a favicon of unknown size, a last resort

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): v or "" for k, v in attrs}
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description", "keywords", "og:title") and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "title":
            self._in_title = True
        elif tag == "link" and a.get("href"):
            rel = a.get("rel", "").lower().split()
            size = max((int(n) for n in re.findall(r"(\d+)x\d+", a.get("sizes", ""))), default=0)
            if "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
                self.icons.append((1000 + size, a["href"]))
            elif "icon" in rel and (size >= 96 or a["href"].lower().split("?")[0].endswith(".svg")):
                self.icons.append((size or 500, a["href"]))
            elif "icon" in rel and not size and a["href"].lower().split("?")[0].endswith(".png") and not self.plain:
                self.plain = a["href"]
        if tag in _SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())


def _parse(html: str) -> _Reader:
    r = _Reader()
    try:
        r.feed(html)
    except Exception:  # noqa: BLE001 - a malformed page still gives what was read before the fault
        pass
    return r


def parse(html: str, url: str) -> Page:
    r = _parse(html)
    icon = urljoin(url, max(r.icons)[1]) if r.icons else ""
    page = Page(text=_text(r), tools=found(html, TOOLS), pixels=found(html, PIXELS),
                icon=icon if icon.startswith("https://") else "")
    if not page.icon and r.plain and urljoin(url, r.plain).startswith("https://"):
        page.plain_icon = urljoin(url, r.plain)
    return page


PLAIN_ICON_MIN = 48     # px: smaller and the 52px mark would show it blurred


async def png_width(http: httpx.AsyncClient, url: str, *, timeout: float = 3.0) -> int:
    """A PNG's width from its header (the first 24 bytes), through the same SSRF check; 0 when unknown."""
    try:
        host = urlparse(url).hostname or ""
        if not url.startswith("https://") or not await asyncio.to_thread(ssrf.host_is_public, host):
            return 0
        async with http.stream("GET", url, timeout=timeout, follow_redirects=False, headers={"User-Agent": _UA}) as r:
            if r.status_code != 200:
                return 0
            head = b""
            async for chunk in r.aiter_bytes():
                head += chunk
                if len(head) >= 24:
                    break
    except (httpx.HTTPError, ValueError):
        return 0
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return 0
    return int.from_bytes(head[16:20], "big")


def _text(r: _Reader, limit: int = 4000) -> str:
    """The page as the LLM reads it: title, meta, then the visible text, whitespace collapsed."""
    lines = []
    if r.title.strip():
        lines.append("Title: " + re.sub(r"\s+", " ", r.title).strip())
    for key, label in (("description", "Description"), ("og:description", "Description"), ("keywords", "Keywords")):
        if key in r.meta and not any(r.meta[key] in line for line in lines):
            lines.append(f"{label}: {unescape(r.meta[key])}")
    body = re.sub(r"\s+", " ", " ".join(r.text)).strip()
    if body:
        lines.append(body)
    return "\n".join(lines)[:limit]


def visible_chars(summary: str) -> int:
    return len(summary.split("\n", 3)[-1]) if summary else 0


async def read(http: httpx.AsyncClient, site: str, *, timeout: float = 5.0) -> Page:
    """https://<site>, read; an empty Page when it cannot be read safely and quickly. With no large
    icon declared, a plain PNG favicon is measured and kept when it is big enough."""
    page = await _get(http, f"https://{site}", timeout) or Page()
    if not page.icon and page.plain_icon and await png_width(http, page.plain_icon) >= PLAIN_ICON_MIN:
        page.icon = page.plain_icon
    return page


async def exists(http: httpx.AsyncClient, url: str, *, timeout: float = 4.0) -> bool:
    """Whether `url` answers an HTML page (after safe redirects); the body is never read."""
    return await _get(http, url, timeout, probe=True) is not None


async def _get(http: httpx.AsyncClient, url: str, timeout: float, *, probe: bool = False) -> Page | None:
    """The page at `url`, or None when it cannot be read safely; `probe` stops at the headers."""
    page = await _fetch(http, url, timeout, probe)
    return page if page is None or probe else await asyncio.to_thread(parse, *page)


async def _fetch(http: httpx.AsyncClient, url: str, timeout: float, probe: bool):
    try:
        for _ in range(HOPS):
            host = urlparse(url).hostname or ""
            if urlparse(url).scheme not in ("http", "https") or not await asyncio.to_thread(ssrf.host_is_public, host):
                return None
            async with http.stream("GET", url, timeout=timeout, follow_redirects=False,
                                   headers={"User-Agent": _UA, "Accept": "text/html"}) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                    return None
                if probe:
                    return ()
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BYTES:
                        break
                return b"".join(chunks).decode(r.encoding or "utf-8", errors="replace"), url
    except (httpx.HTTPError, UnicodeError, ValueError):
        return None
    return None
