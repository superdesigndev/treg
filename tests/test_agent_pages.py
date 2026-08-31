"""The per-agent pages (`/apps/<agent>`): "I use ChatGPT — what can my agent do now?"

Everything on them is a projection of the catalog plus one hand-written install block, so the tests
assert the projection (counts, categories, rows) against `catalog_store.load()` rather than against
literals, and assert the crawler plumbing the shell is supposed to guarantee — canonical, robots
reachability, sitemap membership, FAQ schema that matches the visible page.
"""

from __future__ import annotations

import html as html_mod
import json
import re

import pytest
from httpx import ASGITransport, AsyncClient

from treg import agent_pages
from treg.domain.catalog import store as catalog_store
from treg.api import app
from treg.config import get_settings


def _base() -> str:
    return get_settings().public_url.rstrip("/")


def _ld(html: str) -> list[dict]:
    return [json.loads(m) for m in
            re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


async def test_chatgpt_page_is_served_with_the_crawler_essentials(clients: AsyncClient):
    r = await clients.get("/agents/chatgpt")
    assert r.status_code == 200, r.text[:300]
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert f'<link rel="canonical" href="{_base()}/agents/chatgpt"/>' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "ChatGPT" in title and "treg.to" in title
    assert "noindex" not in html


async def test_chatgpt_page_counts_come_from_the_catalog(clients: AsyncClient):
    """The title's tool count is computed, never typed — the landing, llms.txt and the schema had
    drifted to three different numbers before this rule existed."""
    cat = catalog_store.load()
    # Mirrors web._pub: routed meta-rows delegate to children already counted, so the
    # advertised total excludes them along with the hidden kinds.
    n = sum(1 for e in cat.endpoints
            if e["kind"] not in catalog_store.HIDDEN_KINDS and e.get("kind") != "routed")
    html = (await clients.get("/agents/chatgpt")).text
    assert f"{n:,}" in re.search(r"<title>(.*?)</title>", html).group(1)


async def test_chatgpt_page_hero_rotates_through_the_roles(clients: AsyncClient):
    """"for SEO experts / social media managers / SDRs …" — the first role is server-rendered on
    the roleline under the keyword H1, so a crawler reads a complete phrase; the rest ride in
    the JSON block for the JS."""
    html = (await clients.get("/agents/chatgpt")).text
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)
    # The H1 carries the term and the promise, never a persona: "…for SEO experts" read as the
    # page's audience to a crawler. The wheel lives on its own line below.
    assert "ChatGPT Connector" in h1 and "call" in h1
    assert html_mod.escape(agent_pages.ROLES[0]) not in h1
    roleline = re.search(r'<div class="roleline">(.*?)</div>', html, re.S).group(1)
    assert html_mod.escape(agent_pages.ROLES[0]) in roleline
    # only ONE role server-rendered — the rest are appended by JS from the json block
    assert html_mod.escape(agent_pages.ROLES[1]) not in roleline
    more = json.loads(re.search(r'<script type="application/json" id="roles-more">(.*?)</script>', html, re.S).group(1))
    assert tuple(more) == agent_pages.ROLES[1:]


async def test_chatgpt_page_lists_the_curated_use_cases_by_category(clients: AsyncClient):
    """The page's value is a buyer's menu: plain-words jobs under buyer categories, each backed
    by the capabilities that do it — never a row per endpoint."""
    html = (await clients.get("/agents/chatgpt")).text
    for category, jobs in agent_pages.USE_CASES:
        assert f'<section id="{re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")}"' in html, category
        for label, caps in jobs:
            assert html_mod.escape(label, quote=True) in html, label
            for cid in caps:
                assert f'data-cap="{cid}"' in html, cid
    assert 'data-endpoint="' not in html


def test_every_use_case_capability_exists_in_the_catalog():
    """A job the catalog cannot do must not be advertised, and a renamed capability must fail here
    rather than silently drop a row from the page."""
    cat = catalog_store.load()
    missing = [cid for _, jobs in agent_pages.USE_CASES for _, caps in jobs for cid in caps
               if not cat.for_capability(cid)]
    assert not missing, missing


async def test_chatgpt_page_install_block_has_the_setup_line(clients: AsyncClient):
    html = (await clients.get("/agents/chatgpt")).text
    assert "set up treg" in html and "treg.to/llms.txt" in html
    # never a CTA into the authenticated app: a logged-out /app visit bounces to the landing
    body = html.split("<body>", 1)[1].split("<footer>", 1)[0]
    assert 'href="/app"' not in body.replace('href="/agents/', "")


async def test_chatgpt_page_faq_schema_matches_the_visible_page(clients: AsyncClient):
    """Google treats schema that claims something the page does not say as a violation."""
    html = (await clients.get("/agents/chatgpt")).text
    faqs = [b for b in _ld(html) if b.get("@type") == "FAQPage"]
    assert len(faqs) == 1
    qs = faqs[0]["mainEntity"]
    assert len(qs) >= 3
    for q in qs:
        assert q["name"] in html, q["name"]
    types = {b.get("@type") for b in _ld(html)}
    assert {"SoftwareApplication", "BreadcrumbList"} <= types


async def test_unknown_agent_404s(clients: AsyncClient):
    assert (await clients.get("/agents/clippy")).status_code == 404


async def test_agent_page_is_in_the_sitemap_and_reachable_by_robots(clients: AsyncClient):
    """`Disallow: /app` is a prefix rule: /agents/… must not sit under it (that is why the pages are
    not at /apps/…), and nothing else in robots.txt may block them."""
    sitemap = (await clients.get("/sitemap.xml")).text
    assert f"{_base()}/agents/chatgpt" in sitemap
    robots = (await clients.get("/robots.txt")).text
    assert not any(line.strip() in ("Disallow: /agents", "Disallow: /agents/", "Disallow: /use-cases")
                   for line in robots.splitlines())
    assert (await clients.head("/agents/chatgpt")).status_code == 200


async def test_agent_pages_are_hosted_only(monkeypatch):
    """The install copy describes treg.to's own hosted listing and grant — true of treg.to, false of
    every self-hosted registry. So off the reference hosts the page 404s and leaves the sitemap."""
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get("/agents/chatgpt")).status_code == 404
            assert (await c.get("/agents")).status_code == 404
            assert "/agents" not in (await c.get("/sitemap.xml")).text
    finally:
        get_settings.cache_clear()


async def test_agents_hub_lists_every_agent(clients: AsyncClient):
    """The nav's "Agents" link pointed at /agents/claude-code because no hub existed, and the bare
    /agents URL 404ed while pages linked toward it. One card per client, the nav points here, and
    the sitemap carries it."""
    r = await clients.get("/agents")
    assert r.status_code == 200, r.text[:200]
    html = r.text
    for slug, spec in agent_pages.AGENTS.items():
        assert f'href="/agents/{slug}"' in html, slug
        assert spec["name"] in html, slug
    assert 'href="/agents"' in (await clients.get("/agents/chatgpt")).text  # nav + breadcrumb parent
    assert f"<loc>{_base()}/agents</loc>" in (await clients.get("/sitemap.xml")).text


# ------------------------------------------------------------------ use-case pages (the spokes)

USECASE = "/use-cases/find-professional-emails"


async def test_use_case_page_is_served_with_the_crawler_essentials(clients: AsyncClient):
    r = await clients.get(USECASE)
    assert r.status_code == 200, r.text[:300]
    html = r.text
    assert f'<link rel="canonical" href="{_base()}{USECASE}"/>' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "treg.to" in title and "providers" in title and "API" in title
    assert "noindex" not in html


async def test_use_case_page_answers_the_four_questions_in_order(clients: AsyncClient):
    """The reader does one thing (the prompt); everything else is what the agent sees before it
    calls. Each part is headed as the question people ask, answered in its first sentence."""
    html = (await clients.get(USECASE)).text
    order = ["best way to ask", "Why go through treg.to",
             "Which email finder API is cheapest", "How do the providers compare"]
    idx = [html.find(html_mod.escape(q, quote=False)) for q in order]
    assert all(i > 0 for i in idx), list(zip(order, idx))
    assert idx == sorted(idx), "sections out of order"
    # the honest product claim, and the reader's lever
    assert "does not choose for you" in html or "doesn't choose for you" in html
    assert "tell it how" in html


async def test_use_case_page_compares_one_row_per_provider_with_every_endpoint_collapsed(clients: AsyncClient):
    cat = catalog_store.load()
    # Routed endpoints (kind:routed) are filtered out of the comparison table so they don't appear
    # as a fake "treg" provider row. Match that filter here.
    eps = [e for e in cat.for_capability("people.email.find")
           if e["kind"] not in catalog_store.HIDDEN_KINDS and e.get("kind") != "routed"]
    provs = {e["provider"] for e in eps}
    assert len(provs) >= 2
    html = (await clients.get(USECASE)).text
    for p in provs:
        assert f'data-provider="{p}"' in html, p
    # the page decides between PROVIDERS; the endpoint list belongs on the catalog shelf, which the
    # page links to. One runnable call stays as proof.
    assert "treg call " in html
    assert 'href="/catalog/' in html
    # up to the agent page's category anchor, or to a written sibling page: a related card
    # resolves to the page once one exists, so this stopped being an anchor the day the last
    # related label on this page got its own page.
    assert 'href="/agents/chatgpt#' in html or 'href="/use-cases/' in html


async def test_reliability_section_appears_only_with_traffic(clients: AsyncClient):
    """With no call history the section is omitted (an empty promise is worse than none). The
    copy that would print per-vendor rates lives behind that check."""
    html = (await clients.get(USECASE)).text
    assert "Which one is the most reliable" not in html
    import inspect
    from treg.routers.web import use_case_job_page
    src = inspect.getsource(use_case_job_page)
    assert "not a controlled benchmark" in src


async def test_use_case_page_faq_matches_the_visible_page(clients: AsyncClient):
    html = (await clients.get(USECASE)).text
    faqs = [b for b in _ld(html) if b.get("@type") == "FAQPage"]
    assert len(faqs) == 1
    for q in faqs[0]["mainEntity"]:
        assert html_mod.escape(q["name"], quote=True) in html, q["name"]
    assert "BreadcrumbList" in {b.get("@type") for b in _ld(html)}


async def test_use_case_page_prices_come_from_the_catalog(clients: AsyncClient):
    """No number on the page that the catalog did not produce: the lowest price in the title is the
    cheapest `cost_view` USD across the job's endpoints."""
    cat = catalog_store.load()
    eps = [e for e in cat.for_capability("people.email.find")
           if e["kind"] not in catalog_store.HIDDEN_KINDS]
    lowest = min(c["usd"] for e in eps
                 if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"])
    from treg.routers.web import _usd_short
    html = (await clients.get(USECASE)).text
    # the price sits in the hero kicker and the economics block, not the title: a title that fits a
    # search result has no room for it
    assert _usd_short(lowest) in html


async def test_unknown_use_case_404s(clients: AsyncClient):
    assert (await clients.get("/use-cases/teleport")).status_code == 404
    assert (await clients.get("/use-cases/nope/teleport")).status_code == 404
    # the nested form is kept ONLY as a 301 for the URLs that already shipped
    r = await clients.get("/use-cases/anything/find-professional-emails", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/use-cases/find-professional-emails"


async def test_use_case_page_is_in_the_sitemap_and_linked_from_the_agent_page(clients: AsyncClient):
    assert f"{_base()}{USECASE}" in (await clients.get("/sitemap.xml")).text
    assert f'href="{USECASE}"' in (await clients.get("/agents/chatgpt")).text
    assert (await clients.head(USECASE)).status_code == 200


async def test_legacy_flat_use_case_pages_still_answer(clients: AsyncClient):
    """The five ad landing pages keep their flat URLs; nesting must not shadow them."""
    assert (await clients.get("/use-cases/lead-enrichment-for-ai-agents")).status_code == 200


def test_every_use_case_page_is_a_row_on_the_menu():
    """A spoke's label must match a job in USE_CASES exactly, or the agent page cannot link to it
    and the page has no capabilities to render."""
    menu = {lbl for _c, jobs in agent_pages.USE_CASES for lbl, _ in jobs}
    for slug, spec in agent_pages.USE_CASE_PAGES.items():
        assert spec["label"] in menu, (slug, spec["label"])


# ------------------------------------------------------------------------------- markdown mirrors

async def test_pages_have_markdown_mirrors_for_agents_and_answer_engines(clients: AsyncClient):
    """`.md` serves the same page as plain Markdown, and the HTML declares it as an alternate."""
    for path in ("/agents/chatgpt", USECASE):
        html = (await clients.get(path)).text
        assert f'<link rel="alternate" type="text/markdown" href="{_base()}{path}.md"/>' in html
        r = await clients.get(path + ".md")
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/markdown")
        assert r.text.startswith("# ")
        assert "<div" not in r.text
    # the markdown lists the same jobs as the HTML menu
    md = (await clients.get("/agents/chatgpt.md")).text
    for _, jobs in agent_pages.USE_CASES:
        for label, _ in jobs:
            assert label in md, label


async def test_agent_page_rows_carry_logos_and_free_badges(clients: AsyncClient):
    html = (await clients.get("/agents/chatgpt")).text
    assert "favicons?domain=linkedin.com" in html
    assert "free, your account" in html               # Search Console etc. run on the team's own key
    assert "$0.000" in html                            # the no-markup promise, stated


async def test_no_em_dashes_in_the_hand_written_copy():
    """House style: no em-dashes in page copy. The setup line is the product's literal command and
    is the one exception (both the SETUP_LINE constant and its literal uses in install_steps)."""
    import inspect
    src = inspect.getsource(agent_pages)
    body = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#")
                     and "SETUP_LINE" not in l
                     and "set up treg —" not in l)  # the literal setup line in install_steps
    # docstrings are not page copy; strip the module docstring
    body = body.split('"""', 2)[-1]
    assert "—" not in body, [l for l in body.splitlines() if "—" in l][:3]


# --------------------------------------------------- template fitness for the other 65 jobs

SINGLE = "/use-cases/search-console-queries"
MULTI = "/use-cases/find-creators-by-keyword"


async def test_cheapest_is_only_claimed_within_one_billing_unit(clients: AsyncClient):
    """38 of the 66 jobs mix per-call, per-result and per-success endpoints. Sorting those by USD
    per chargeable event names the wrong winner (a $0.09 call returning 1,000 rows is not dearer
    than $0.0005 per row), so the page states a cheapest PER UNIT and says the units differ."""
    html = (await clients.get(USECASE)).text
    assert "per found" in html
    # the claim carries its unit, never a bare superlative
    assert re.search(r"[Cc]heapest per (found|call|result)", html), "no unit-scoped cheapest claim"


async def test_single_provider_job_uses_the_short_form(clients: AsyncClient):
    """One provider means there is nothing to compare: the three comparison questions must not
    render as three sections with one row each."""
    r = await clients.get(SINGLE)
    assert r.status_code == 200, r.text[:300]
    html = r.text
    assert "cheapest" not in html.lower()
    assert "How do the providers compare" not in html
    assert "Behind the scenes" not in html
    assert "How it works" in html                 # the short form's own section
    assert "your own account" in html.lower()     # and it says the job runs on the reader's key
    assert "treg call " in html


async def test_multi_platform_job_groups_by_platform_not_by_price(clients: AsyncClient):
    """19 jobs span several platforms. Instagram search and YouTube search are not alternatives to
    each other, so the page must not rank them against one another as if they were."""
    r = await clients.get(MULTI)
    assert r.status_code == 200, r.text[:300]
    html = r.text
    for label in ("Instagram", "TikTok", "YouTube"):
        assert f'data-platform-group="{label}"' in html or f'>{label}</h4>' in html, label
    assert not re.search(r"[Cc]heapest overall", html)


async def test_reliability_section_is_absent_when_there_is_no_traffic(clients: AsyncClient):
    """Most endpoints see no calls in a 30-day window. An empty promise is worse than no section."""
    html = (await clients.get(USECASE)).text
    assert "Which one is the most reliable" not in html   # no CallRecords in the test database


async def test_no_agent_or_job_specific_string_is_hardcoded_in_the_route():
    """Everything job-specific comes from the page spec, and the example agent from one constant,
    so writing page 2 is data entry."""
    import inspect
    from treg.routers.web import use_case_job_page
    src = inspect.getsource(use_case_job_page)
    for bad in ("email finder", "found addresses", "an address is found", "email address"):
        assert bad not in src.lower(), bad
    assert src.count("ChatGPT") == 0, "the example agent must come from DEFAULT_AGENT"


async def test_use_cases_hub_lists_every_written_page(clients: AsyncClient):
    """The breadcrumb pointed at an agent page because no hub existed. A sitemap is not a crawl path."""
    r = await clients.get("/use-cases")
    assert r.status_code == 200, r.text[:200]
    html = r.text
    for j, spec in agent_pages.USE_CASE_PAGES.items():
        assert f'href="/use-cases/{j}"' in html, j
        assert html_mod.escape(spec["sentence"], quote=True) in html or spec["label"] in html
    assert f"{_base()}/use-cases" in (await clients.get("/sitemap.xml")).text
    assert '<a href="/use-cases">' in (await clients.get(USECASE)).text   # breadcrumb points here


# Every page that ships, not a hand-kept list: this set grows by 59 as the use-case pages land, and
# a title that overflows is invisible in exactly the way nobody notices during review.
ALL_PAGES = ([f"/agents/{a}" for a in agent_pages.AGENTS] + ["/agents", "/use-cases", "/workflows"]
             + [f"/use-cases/{j}" for j in agent_pages.USE_CASE_PAGES]
             + [f"/workflows/{w}" for w in agent_pages.WORKFLOWS])


@pytest.mark.parametrize("path", ALL_PAGES)
async def test_titles_and_descriptions_fit_a_search_result(clients: AsyncClient, path: str):
    """Google prints roughly 60 characters of a title and 155 of a description; past that it cuts
    mid-word, and the cut usually lands on the part that would have made someone click."""
    html = (await clients.get(path)).text
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    desc = re.search(r'name="description" content="(.*?)"', html, re.S).group(1)
    assert len(title) <= 65, f"{path}: title {len(title)} chars: {title}"
    assert len(desc) <= 160, f"{path}: description {len(desc)} chars"
    assert desc.rstrip().endswith((".", "?", "!")), f"{path}: description cut mid-sentence: …{desc[-40:]}"


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
@pytest.mark.parametrize("key", list(agent_pages.USE_CASE_PAGES))
def test_no_use_case_page_ships_with_an_empty_section(key):
    """The template renders whatever the spec holds, so a missing field is a heading with nothing
    under it rather than an error. The counts are the house shape: 4 reasons, 3 notes, 4 FAQ."""
    spec = agent_pages.USE_CASE_PAGES[key]
    for field in ("label", "sentence", "title", "lede", "prompt", "what_is"):
        assert spec.get(field), (key, field)
    assert len(spec["prompt_why"]) == 4, (key, "prompt_why")
    assert len(spec["notes"]) == 3, (key, "notes")
    assert len(spec["faq"]) == 4, (key, "faq")
    assert len(spec["related"]) == 4, (key, "related")
    # the voices section is optional, but half of one is a heading over nothing
    assert bool(spec.get("voices")) == bool(spec.get("voices_intro")), (key, "voices without intro")
    for v in spec.get("voices") or ():
        heading, quote, who, url, answer = v
        assert all((heading, quote, who, url, answer)), (key, heading)
        assert url.startswith("https://"), (key, url)
        assert len(quote.split()) <= 25, (key, "quote over 25 words", quote)


def test_related_links_point_at_jobs_that_exist():
    """`related` is rendered as links to other pages. A label that is not on the menu is a 404, and
    the four ad landing pages already taught us that a dead internal link is never noticed."""
    menu = {lbl for _, jobs in agent_pages.USE_CASES for lbl, _ in jobs}
    for key, spec in agent_pages.USE_CASE_PAGES.items():
        for label in spec["related"]:
            assert label in menu, (key, label)


def test_related_cards_resolve_to_the_job_s_own_category():
    """Four categories carry fewer than five jobs, so `related` has to cross categories there.
    Resolving inside the current page's category sent those cards to the wrong anchor under a
    caption naming the wrong category, and no test noticed because the label still existed."""
    from treg.routers.web import _related_link, _use_case_page_for
    owner = {lbl: c for c, jobs in agent_pages.USE_CASES for lbl, _ in jobs}
    for key, spec in agent_pages.USE_CASE_PAGES.items():
        for lbl in spec["related"]:
            href, cat = _related_link(lbl, "chatgpt")
            assert cat == owner[lbl], (key, lbl, cat)
            assert href == (_use_case_page_for(lbl)
                            or f"/agents/chatgpt#{agent_pages.category_slug(owner[lbl])}"), (key, lbl)


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


# ------------------------------------------------------------------ the taxonomy, and its URLs

def test_one_axis_only_no_access_mode_categories():
    """Categories are cut by what the job is ABOUT. "Connect your own accounts" was cut by how you
    authenticate, and the mixed axis leaked: reviews appeared twice split by whose account they
    were, Search Console sat outside SEO, and one job existed twice under two names with identical
    capabilities. Running on the team's own key is a property (the FREE badge), never a category."""
    cats = [c for c, _ in agent_pages.USE_CASES]
    assert "Connect your own accounts" not in cats
    for c in cats:
        assert "your own account" not in c.lower() and "connect" not in c.lower(), c


def test_no_job_appears_in_two_categories_and_no_two_jobs_share_capabilities():
    """The duplicate that started this: "Google Ads and Meta Ads campaign performance" and "Your own
    campaign performance" were two menu rows with identical capabilities in two categories."""
    seen_label, seen_caps = {}, {}
    for c, jobs in agent_pages.USE_CASES:
        for lbl, caps in jobs:
            assert lbl not in seen_label, (lbl, c, seen_label.get(lbl))
            seen_label[lbl] = c
            key = tuple(sorted(caps))
            assert key not in seen_caps, (lbl, seen_caps.get(key), key)
            seen_caps[key] = lbl


def test_use_case_urls_are_flat_so_a_recut_never_moves_them():
    """Category is metadata, not a path segment. Composio files every blueprint at a flat
    /use-case/<slug> and renders the category as a chip; re-cutting their taxonomy costs nothing.
    Ours cost a round of redirects to learn the same thing, so the shape is now fixed."""
    for slug in agent_pages.USE_CASE_PAGES:
        assert "/" not in slug, slug


def test_every_grouped_job_is_on_its_category_menu():
    """CATEGORY_GROUPS gives the enrichment section its sub-headings. A group naming a job that is
    not on that category's menu renders a heading over nothing."""
    for category, groups in agent_pages.CATEGORY_GROUPS.items():
        menu = {lbl for c, jobs in agent_pages.USE_CASES if c == category for lbl, _ in jobs}
        assert menu, category
        grouped = [lbl for _, labels in groups for lbl in labels]
        for lbl in grouped:
            assert lbl in menu, (category, lbl)
        assert len(grouped) == len(set(grouped)), category
        assert set(grouped) == menu, (category, sorted(menu - set(grouped)))


def test_every_category_has_a_blurb_and_a_prompt():
    for c, _ in agent_pages.USE_CASES:
        assert agent_pages.CATEGORY_BLURBS.get(c), c
        assert agent_pages.CATEGORY_PROMPTS.get(c), c


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


async def test_workflow_page_is_served_with_the_crawler_essentials(clients: AsyncClient):
    r = await clients.get(WORKFLOW)
    assert r.status_code == 200, r.text[:300]
    html = r.text
    assert f'<link rel="canonical" href="{_base()}{WORKFLOW}"/>' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "treg.to" in title
    assert "noindex" not in html
    ld = _ld(html)
    howto = next(b for b in ld if b["@type"] == "HowTo")
    assert len(howto["step"]) == 5
    assert any(b["@type"] == "FAQPage" for b in ld)
    spec = agent_pages.WORKFLOWS["find-and-verify-a-lead-list"]
    md = await clients.get(WORKFLOW + ".md")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    for name, *_ in spec["steps"]:
        assert name in md.text, name
    assert md.text.rstrip().endswith(f"HTML version: {_base()}{WORKFLOW}")
    csv = await clients.get(WORKFLOW + ".csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert csv.text.startswith("company,domain,person_found,email_source,verify")
    # Real people: the published copy carries outcomes per row, never a name, title or address.
    assert "@" not in csv.text and "person,title,email" not in csv.text
    hub = await clients.get("/workflows")
    assert hub.status_code == 200 and f'href="{WORKFLOW}"' in hub.text
    loud = "/workflows/" + WORKFLOW.rsplit("/", 1)[1].upper()
    r = await clients.get(loud, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == WORKFLOW
    r = await clients.get(loud + ".md", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == WORKFLOW + ".md"
    assert (await clients.get("/workflows/teleport")).status_code == 404
    assert (await clients.get("/workflows/teleport.csv")).status_code == 404
    # the hubs cross-link, and the sitemap carries both
    assert 'href="/workflows"' in (await clients.get("/use-cases")).text
    sitemap = (await clients.get("/sitemap.xml")).text
    assert f"{_base()}/workflows" in sitemap and f"{_base()}{WORKFLOW}" in sitemap


async def test_workflow_pages_are_hosted_only(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.com")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get(WORKFLOW)).status_code == 404
            assert (await c.get("/workflows")).status_code == 404
    finally:
        get_settings.cache_clear()


def test_every_workflow_step_capability_and_endpoint_exist():
    """A step names a capability and the endpoint the worked run used. Both must be in the catalog,
    or the page prices a step from nothing."""
    cat = catalog_store.load()
    for key, spec in agent_pages.WORKFLOWS.items():
        for name, cap, _asks, ep_id, _why in spec["steps"]:
            eps = [e for e in cat.for_capability(cap) if e["kind"] not in catalog_store.HIDDEN_KINDS]
            assert eps, (key, name, cap)
            assert ep_id in {e["id"] for e in eps}, (key, name, ep_id)


@pytest.mark.parametrize("key", list(agent_pages.WORKFLOWS))
def test_no_workflow_ships_with_an_empty_section(key):
    spec = agent_pages.WORKFLOWS[key]
    for field in ("sentence", "title", "lede", "prompt"):
        assert spec.get(field), (key, field)
    assert len(spec["prompt_why"]) == 4, (key, "prompt_why")
    assert len(spec["steps"]) >= 3, (key, "steps")
    assert spec["run"]["receipt"], (key, "receipt")
    assert spec["run"]["narrative"], (key, "narrative")
    assert spec["run"].get("date") and spec["run"].get("csv"), (key, "run date/csv")
    assert len(spec["failure_modes"]) >= 4, (key, "failure_modes")
    assert len(spec["faq"]) == 4, (key, "faq")
    assert len(spec["related"]) == 4, (key, "related")
    menu = {lbl for _c, jobs in agent_pages.USE_CASES for lbl, _ in jobs}
    for lbl in spec["related"]:
        assert lbl in menu, (key, lbl)


def _walk_strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _walk_strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _walk_strings(v)


async def test_workflow_total_counts_a_once_per_run_step_once(clients: AsyncClient):
    """The list step is one Apollo page for the whole run, not one call per row. The worst-case
    total must say 1 × its price, or the receipt's 'why it differs' blames misses for arithmetic."""
    html = (await clients.get(WORKFLOW)).text
    spec = agent_pages.WORKFLOWS["find-and-verify-a-lead-list"]
    assert spec["once"] == ("apollo.companies.search",)
    assert "1 &times; $0.026" in html and "50 &times; $0.026" not in html


def test_workflow_copy_has_no_em_dashes():
    """Same house rule as the use-case pages; SETUP_LINE is the one exception and is not part of
    a workflow entry."""
    for key, spec in agent_pages.WORKFLOWS.items():
        for s in _walk_strings(spec):
            assert "—" not in s and "–" not in s, (key, s)


@pytest.mark.parametrize("path", [
    "/agents/chatgpt",
    "/agents",
    "/use-cases/verify-an-email",
    "/use-cases",
    "/workflows",
    "/docs",
])
async def test_pages_off_the_shared_shell_carry_adtrack(clients: AsyncClient, path: str):
    """Every page rendered through `_page()` must load `/adtrack.js` exactly once.

    Without it no `treg_ad` cookie is set, so `org.ad_gclid` stays NULL and `adsconv.queue()`
    no-ops — a paid click can sign up and make its first call and Google never hears about it,
    silently. The script lived only on the hand-written marketing HTML until 2026-08-30, which
    left the whole server-rendered surface unattributable.

    Scope is deliberately `_page()` callers, not "every server-rendered page": `_legal_page()`,
    `/dashboard-tour/` and the FastAPI Swagger shell at `/docs/api` render their own HTML and are
    not ad destinations. `/tutorial` is likewise out of scope — it is slated for removal.

    `/sitetrack.js` is deliberately NOT asserted here; see `_page()`'s docstring.
    """
    r = await clients.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    html = r.text
    assert html.count('<script src="/adtrack.js"></script>') == 1, (
        f"{path} must load adtrack.js exactly once")


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
        assert f"{_base()}/use-cases/{new}" in sitemap, new


async def test_own_account_pages_do_not_carry_the_metering_cards(clients: AsyncClient):
    """"One key, not 9 accounts" and "Already pay Hunter?" are false wherever the reader's own
    connected account does the job — the short own-account pages, and the YouTube comparisons
    whose official Data API row is $0.00 on the reader's own quota."""
    for path in ("/use-cases/search-console-queries", "/use-cases/video-details-views-and-stats"):
        html = (await clients.get(path)).text
        assert "Already pay Hunter" not in html, path
        assert "not 9 accounts" not in html, path
    # and a fully metered job keeps the full pitch
    assert "Already pay Hunter" in (await clients.get(USECASE)).text


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
