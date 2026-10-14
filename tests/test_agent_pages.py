"""The agent, use-case and workflow pages (`/agents`, `/use-cases`, `/workflows`).

Their copy is not pinned here. These tests hold what a broken page would hide: every advertised
capability exists, internal links resolve, routing and redirects hold, and a self-hosted registry
never serves the treg.to-only pages.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg import agent_pages
from treg.domain.catalog import store as catalog_store
from treg.api import app
from treg.config import get_settings


def test_every_use_case_capability_exists_in_the_catalog():
    """A job the catalog cannot do must not be advertised, and a renamed capability must fail here
    rather than silently drop a row from the page."""
    cat = catalog_store.load()
    missing = [cid for _, jobs in agent_pages.USE_CASES for _, caps in jobs for cid in caps
               if not cat.for_capability(cid)]
    assert not missing, missing


@pytest.mark.parametrize("path,status,location", [
    ("/agents/clippy", 404, None),
    ("/use-cases/teleport", 404, None),
    ("/use-cases/nope/teleport", 404, None),
    # the nested form is kept ONLY as a 301 for the URLs that already shipped
    ("/use-cases/anything/find-professional-emails", 301, "/use-cases/find-professional-emails"),
    # the five ad landing pages keep their flat URLs; nesting must not shadow them
    ("/use-cases/lead-enrichment-for-ai-agents", 200, None),
])
async def test_page_routing(clients: AsyncClient, path: str, status: int, location: str | None):
    r = await clients.get(path, follow_redirects=False)
    assert r.status_code == status, (path, r.status_code)
    if location:
        assert r.headers["location"] == location


@pytest.mark.parametrize("path", [
    "/agents/chatgpt", "/agents", "/workflows/find-and-verify-a-lead-list", "/workflows",
])
async def test_hosted_only_pages_404_on_a_self_hosted_registry(monkeypatch, path: str):
    """The install copy and worked runs describe treg.to's own hosted listing and grant, true of
    treg.to, false of every self-hosted registry. So off the reference hosts the page 404s and
    leaves the sitemap."""
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get(path)).status_code == 404
            assert f"{path}<" not in (await c.get("/sitemap.xml")).text
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("field", ["label", "related"])
def test_no_dead_internal_link(field: str):
    """A spoke's label and every `related` label must match a job in USE_CASES exactly, or the
    agent page cannot link to it and the rendered link is a 404. The four ad landing pages already
    taught us that a dead internal link is never noticed."""
    menu = {lbl for _c, jobs in agent_pages.USE_CASES for lbl, _ in jobs}
    for slug, spec in agent_pages.USE_CASE_PAGES.items():
        labels = [spec["label"]] if field == "label" else spec["related"]
        for label in labels:
            assert label in menu, (slug, label)


# ------------------------------------------------------------------ use-case pages (the spokes)

USECASE = "/use-cases/find-professional-emails"


async def test_non_canonical_casing_redirects_to_the_one_spelling(clients: AsyncClient):
    """Lookups are case-insensitive, but the request's own bytes must never be rendered into the
    canonical / alternate / breadcrumb (CodeQL py/reflective-xss) — and `/agents/ChatGPT` serving a
    200 with a canonical to itself is a duplicate page. One 301 to the lowercase slug instead."""
    r = await clients.get("/agents/ChatGPT", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/agents/chatgpt"
    r = await clients.get("/agents/ChatGPT.md", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/agents/chatgpt.md"
    job = next(iter(agent_pages.USE_CASE_PAGES))
    r = await clients.get(f"/use-cases/{job.upper()}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/use-cases/{job}"
    assert (await clients.get("/agents/<script>")).status_code == 404


async def test_a_provider_with_no_dollar_rate_is_not_labelled_free(clients: AsyncClient):
    """`free` and `no published rate` are different facts. Semrush prices the SERP and ranked
    keyword jobs in pre-bought API units, so its `cost_view` carries no USD, and the price cell
    read "free, your own account" for what is in fact the dearest option on the page."""
    cat = catalog_store.load()
    page = "/use-cases/google-results-for-a-keyword"
    eps = [e for e in cat.for_capability("google.serp.organic")
           if e["kind"] not in catalog_store.HIDDEN_KINDS]
    unpriced = [e for e in eps
                if not ((c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"])]
    assert unpriced, "this test needs a row the provider publishes no dollar rate for"
    assert all((e.get("cost") or {}).get("type") != "free" for e in unpriced)
    for text in ((await clients.get(page)).text, (await clients.get(page + ".md")).text):
        assert "no dollar rate published" in text
        assert "free, your own account" not in text
        assert "own account, free" not in text


# ------------------------------------------------------------------ workflow pages (/workflows)

WORKFLOW = "/workflows/find-and-verify-a-lead-list"


async def test_the_prose_pages_consult_the_shared_observation_reader(clients: AsyncClient, monkeypatch):
    """The use-case and workflow pages read observed stats through the process-wide reader, like
    the catalog routes. Wiring a raw DB session in instead fails silently — the session has no
    `get_many`, the degrade-to-empty guard eats the AttributeError, and every page quietly loses
    its reliability numbers while the logs fill with tracebacks."""
    calls: list[list[str]] = []

    class Reader:
        async def get_many(self, endpoint_ids):
            ids = list(endpoint_ids)
            calls.append(ids)
            return {i: {"samples": 4321, "ok_rate": 1.0, "p50_ms": 40, "p95_ms": 90,
                        "last_ok_days": 0} for i in ids}

    monkeypatch.setattr(app.state, "endpoint_observation_reader", Reader())

    html = (await clients.get(USECASE)).text
    assert calls and calls[0], "the use-case page never consulted the observation reader"
    assert "4321 calls" in html

    calls.clear()
    r = await clients.get(WORKFLOW)
    assert r.status_code == 200, r.text[:300]
    assert calls, "the workflow page never consulted the observation reader"
    assert "4321 calls" in r.text

    calls.clear()
    assert (await clients.get("/workflows")).status_code == 200
    assert calls, "the workflows hub never consulted the observation reader"


async def test_workflow_page_csv_carries_no_pii_and_casing_redirects(clients: AsyncClient):
    csv = await clients.get(WORKFLOW + ".csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert csv.text.startswith("company,domain,person_found,email_source,verify")
    # Real people: the published copy carries outcomes per row, never a name, title or address.
    assert "@" not in csv.text and "person,title,email" not in csv.text
    loud = "/workflows/" + WORKFLOW.rsplit("/", 1)[1].upper()
    r = await clients.get(loud, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == WORKFLOW
    r = await clients.get(loud + ".md", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == WORKFLOW + ".md"
    assert (await clients.get("/workflows/teleport")).status_code == 404
    assert (await clients.get("/workflows/teleport.csv")).status_code == 404


def test_every_workflow_step_capability_and_endpoint_exist():
    """A step names a capability and the endpoint the worked run used. Both must be in the catalog,
    or the page prices a step from nothing."""
    from treg.routers import web
    cat = catalog_store.load()
    for key, spec in agent_pages.WORKFLOWS.items():
        for name, cap, _asks, ep_id, _why in spec["steps"]:
            if cap == "decision":
                # a judgement step is priced from DECISION_STEPS, the one table that carries its rate
                dec = agent_pages.DECISION_STEPS[ep_id]
                assert dec["usd"] and dec["unit"] and dec["link"].startswith("/"), (key, name, ep_id)
                continue
            # the same filter the page applies: a routed meta-row would render as provider "treg"
            # with no price and drop out of the live total
            eps = [e for e in cat.for_capability(cap) if web._pub(e)]
            assert eps, (key, name, cap)
            assert ep_id in {e["id"] for e in eps}, (key, name, ep_id)


async def test_pages_off_the_shared_shell_carry_adtrack(clients: AsyncClient):
    """Every page rendered through `_page()` must load `/adtrack.js` exactly once.

    Without it no `treg_ad` cookie is set, so `org.ad_gclid` stays NULL and `adsconv.queue()`
    no-ops: a paid click can sign up and make its first call and Google never hears about it,
    silently. `_page()` is the one shell, so one of its callers stands for all of them.

    `/sitetrack.js` is deliberately NOT asserted here; see `_page()`'s docstring.
    """
    r = await clients.get("/use-cases/verify-an-email")
    assert r.status_code == 200
    assert r.text.count('<script src="/adtrack.js"></script>') == 1


async def test_possessive_slug_redirects_hold_in_every_shape_they_were_live(clients: AsyncClient):
    """The five renamed slugs 301 from the flat form, the .md form, and the nested form that
    shipped first — the nested handler used to reject a renamed slug before consulting the map,
    turning the promised 301 into a 404. The old slugs also leave the sitemap with the rename."""
    sitemap = (await clients.get("/sitemap.xml")).text
    for old, new in agent_pages.USE_CASE_REDIRECTS.items():
        assert new in agent_pages.USE_CASE_PAGES, (old, new)
        r = await clients.get(f"/use-cases/{old}", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == f"/use-cases/{new}", old
        r = await clients.get(f"/use-cases/{old}.md", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == f"/use-cases/{new}.md", old
        r = await clients.get(f"/use-cases/anything/{old}", follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == f"/use-cases/{new}", old
        assert f"/use-cases/{old}<" not in sitemap and f"{old}</loc>" not in sitemap, old
        assert f"{get_settings().public_url.rstrip("/")}/use-cases/{new}" in sitemap, new


async def test_routed_rows_never_surface_a_provider_named_treg(clients: AsyncClient):
    """PR #242's `kind: routed` meta-rows delegate to children that are already listed, so on any
    public surface they double-count and print a vendor named "treg" (the brand is treg.to, and
    treg is not a vendor). `_pub` is the one filter every public page reads, and the provider grid
    feeds both the sitemap and /catalog's prerender — so /tools/treg must not exist."""
    from treg.routers.web import _provider_rows, _pub
    cat = catalog_store.load()
    routed = [e for e in cat.endpoints if e.get("kind") == "routed"]
    assert routed, "no routed rows in the catalog — retire this test's premise"
    assert not any(_pub(e) for e in routed)
    assert "treg" not in {r["service"] for r in _provider_rows()}
    assert (await clients.get("/tools/treg")).status_code == 404
    assert "/tools/treg<" not in (await clients.get("/sitemap.xml")).text


async def test_every_workflow_csv_route_serves(clients: AsyncClient):
    for slug in agent_pages.WORKFLOWS:
        r = await clients.get(f"/workflows/{slug}.csv")
        assert r.status_code == 200, (slug, r.status_code)
        assert r.headers["content-type"].startswith("text/csv"), slug


# ------------------------------------------------------------------ agent pages that are their own page

def _visible_words(page: str) -> list[str]:
    """The words a reader sees: text nodes outside <script> and <style>, via the stdlib parser."""
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.skip, self.parts = 0, []

        def handle_starttag(self, tag, attrs):
            self.skip += tag in ("script", "style")

        def handle_endtag(self, tag):
            self.skip -= tag in ("script", "style") and self.skip > 0

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    p = _Text()
    p.feed(page)
    return " ".join(p.parts).split()


async def test_focused_agent_pages_are_not_the_template_with_a_name_swapped(clients: AsyncClient):
    """The first agent pages were one template with the name swapped, 95-98% identical, and Google
    left all but one "Discovered - currently not indexed". An agent with `focus` and a `guide` must
    read as its own page: well under that overlap with the template page."""
    import difflib
    base = _visible_words((await clients.get("/agents/chatgpt")).text)
    focused = [a for a, s in agent_pages.AGENTS.items() if s.get("focus") and s.get("guide")]
    assert {"claude-code", "hermes"} <= set(focused)
    for agent in focused:
        words = _visible_words((await clients.get(f"/agents/{agent}")).text)
        ratio = difflib.SequenceMatcher(None, base, words, autojunk=False).ratio()
        assert ratio < 0.75, (agent, round(ratio, 3))


async def test_agent_guides_name_real_endpoints_and_live_links(clients: AsyncClient):
    """A guide is hand-written, so nothing derives it from the catalog. Every `treg call <id>` it
    prints must exist, and every internal link it carries must answer, or the page teaches a call
    that 404s."""
    import re
    ids = {e["id"] for e in catalog_store.load().endpoints}
    for agent, spec in agent_pages.AGENTS.items():
        for g in spec.get("guide") or ():
            for eid in re.findall(r"treg call ([\w.\-]+)", g["html"]):
                assert eid in ids, (agent, eid)
            for href in re.findall(r'href="(/[^"#]*)', g["html"]):
                r = await clients.get(href)
                assert r.status_code == 200, (agent, href, r.status_code)
        for category in spec.get("focus") or ():
            assert category in agent_pages.CATEGORY_BLURBS, (agent, category)


async def test_a_focused_agent_page_still_reaches_every_category(clients: AsyncClient):
    """`focus` prints two categories in full; the rest must still be one click away, in the HTML and
    in the .md twin, so the page stays the map of what the agent can do."""
    for agent, spec in agent_pages.AGENTS.items():
        if not spec.get("focus"):
            continue
        page = (await clients.get(f"/agents/{agent}")).text
        md = (await clients.get(f"/agents/{agent}.md")).text
        for category, _jobs in agent_pages.USE_CASES:
            slug = agent_pages.category_slug(category)
            if category in spec["focus"]:
                assert f'id="{slug}"' in page, (agent, category)
            else:
                assert f'href="/use-cases#{slug}"' in page, (agent, category)
                assert f"/use-cases#{slug})" in md, (agent, category)


def test_the_hermes_page_and_treg_mcp_install_agree_on_the_server(monkeypatch):
    """`treg mcp install` detects Hermes but only prints a manual hint (the light CLI writes no YAML);
    the page shows the full block. Both must name the same MCP URL and the same config shape, and
    the page must use Hermes's registered tool names (mcp__<server>__<tool>, double underscores)."""
    import re
    from treg import mcp_install
    monkeypatch.setitem(mcp_install.MANUAL_AGENTS["hermes"], "marker", lambda: True)
    # only=["hermes"] skips every auto-written agent, so nothing on this machine is touched
    out = mcp_install.install_mcp(base_url="https://treg.to", token="t", only=["hermes"])
    assert out["results"] == []
    (display, how), = out["manual"]
    assert display == "Hermes" and "~/.hermes/config.yaml" in how and "mcp_servers" in how and "Bearer" in how
    spec = agent_pages.AGENTS["hermes"]
    block = spec["guide"][0]["html"]
    assert "mcp_servers:" in block and f'url: "{out["mcp_url"]}"' in block and "Bearer" in block
    steps = " ".join(spec["install_steps"])
    assert re.findall(r"mcp__treg__\w+", steps) and not re.findall(r"mcp_treg_\w+", steps)


def test_agent_titles_fit_a_search_result():
    """A title past ~65 characters is cut in the result page; the census count is formatted in,
    so check with today's numbers."""
    from treg.routers.web import _catalog_census
    n_eps, n_plats = _catalog_census()
    for agent, spec in agent_pages.AGENTS.items():
        title = spec["title"].format(n=f"{n_eps:,}", p=str(n_plats))
        assert len(title) <= 65, (agent, len(title), title)


async def test_a_focused_page_keeps_every_category_anchor(clients: AsyncClient):
    """Collapsing a category into the "Everything else" table must not break links to
    /agents/<agent>#<category> that worked when the full table was there."""
    from treg.routers.web import _anchor
    for agent, spec in agent_pages.AGENTS.items():
        if not spec.get("focus"):
            continue
        page = (await clients.get(f"/agents/{agent}")).text
        for category, _jobs in agent_pages.USE_CASES:
            assert f'id="{_anchor(category)}"' in page, (agent, category)


# ------------------------------------------------------------------ lead-list workflow, /jev, Monid

LEAD_LIST = "/workflows/find-and-verify-a-lead-list"


async def test_the_lead_list_page_names_claude_and_carries_its_tested_section(clients: AsyncClient):
    """The page targets "ai lead generation" and the Claude phrasing, uses Claude Code as its example
    agent (other workflows keep the default), and its hand-written description and "tested" section
    reach both the HTML and the .md twin."""
    import json
    import re
    spec = agent_pages.WORKFLOWS["find-and-verify-a-lead-list"]
    page = (await clients.get(LEAD_LIST)).text
    title = re.search(r"<title>(.*?)</title>", page).group(1)
    assert "AI lead generation" in title and "Claude" in title and len(title) <= 65, title
    desc = re.search(r'name="description" content="([^"]*)"', page).group(1)
    # the run's figures must survive _serp_desc's cut, so the whole sentence has to fit
    assert "50 companies, 20 verified contacts, $0.12 each" in desc and len(desc) <= 155, desc
    assert "What Claude Code calls" in page and "What ChatGPT calls" not in page
    other = next(s for s in agent_pages.WORKFLOWS if s != "find-and-verify-a-lead-list")
    assert "What Claude Code calls" not in (await clients.get(f"/workflows/{other}")).text
    assert 'id="tested"' in page and "$0.0056" in page
    md = (await clients.get(LEAD_LIST + ".md")).text
    assert "## What lead generation in Claude costs: recorded data costs" in md and "work-email-finding-bench)" in md
    for row in ("| Tool | Per correct email | Exact match |", "| treg.to | $0.0056 | 90.4% |",
                "| Clay | $0.0395 | 89.7% |", "| Freckle | $0.0427 | 90.1% |", "| Deepline | $0.0924 | 86.6% |",
                "| This workflow, 23 Sep 2026, 50 companies in | $0.12 per verified contact ($2.33 metered, 20 contacts) |"):
        assert row in md, row  # the .md twin keeps the tables as tables, cell for cell
    faqs = [q for q, _a in spec["faq"]]
    assert "Can Claude do lead generation?" in faqs
    ld = [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)]
    faq_ld = next(x for x in (y for b in ld for y in (b if isinstance(b, list) else [b])) if x.get("@type") == "FAQPage")
    assert [q["name"] for q in faq_ld["mainEntity"]] == faqs


async def test_jev_page_embeds_the_walkthrough_and_links_the_lead_list(clients: AsyncClient):
    """/jev carries the walkthrough video as a click-to-load poster with VideoObject markup, links
    the lead-list run from the buyer-signal recipe, and keeps its visible FAQ and FAQPage in step."""
    import html as html_mod
    import json
    import re
    page = (await clients.get("/jev")).text
    assert 'data-yt="o4Vi5uBZYH0"' in page and "<iframe" not in page.split('id="walkthrough"')[1][:2000]
    # the site sends no referrer and YouTube's player refuses to start without one (error 153)
    assert "f.referrerPolicy = 'strict-origin-when-cross-origin'" in page
    blocks = [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)]
    video = next(b for b in blocks if b.get("@type") == "VideoObject")
    assert video["uploadDate"].startswith("2026-09-21") and video["duration"] == "PT14M29S"
    assert all(c["startOffset"] < c["endOffset"] for c in video["hasPart"])
    assert f'href="{LEAD_LIST}"' in page and (await clients.get(LEAD_LIST)).status_code == 200
    faq = next(b for b in blocks if b.get("@type") == "FAQPage")
    faq_html = page.split('id="faq"', 1)[1].split("</section>", 1)[0]
    visible = [html_mod.unescape(q) for q in re.findall(r"<summary>(.*?)</summary>", faq_html)]
    assert [q["name"] for q in faq["mainEntity"]] == visible  # same questions, same order, both ways
    assert {"How do I use jev in Claude Code, Codex, Hermes or OpenClaw?",
            "Can I use jev for lead generation?"} <= set(visible)


async def test_no_served_page_mentions_monid(clients: AsyncClient):
    """Monid is never named on a treg.to page: not in a comparison, a bench table or copy. The
    overflow code that routes through it is backend and never rendered."""
    import re
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "src" / "treg" / "web"
    for f in web.rglob("*"):
        if f.suffix in {".html", ".md", ".txt", ".js", ".css", ".json"}:
            assert "monid" not in f.read_text(encoding="utf-8", errors="ignore").lower(), str(f)
    # Every page the sitemap lists, plus the .md twins of the pages that have one. Catalog and
    # provider pages are generated from the catalog, which has no such provider.
    sitemap = (await clients.get("/sitemap.xml")).text
    paths = sorted({re.sub(r"^https?://[^/]+", "", u) for u in re.findall(r"<loc>([^<]+)</loc>", sitemap)})
    paths = [p for p in paths if not p.startswith(("/catalog/", "/tools/"))]
    paths += [p + ".md" for p in paths if p.startswith(("/agents/", "/workflows/", "/use-cases/"))]
    paths += ["/gtm-engineering.md", "/llms.txt"]
    assert "/blog/work-email-finding-bench" in paths and "/gtm-engineering" in paths
    for path in paths:
        r = await clients.get(path)
        if r.status_code == 200:
            assert "monid" not in r.text.lower(), path
