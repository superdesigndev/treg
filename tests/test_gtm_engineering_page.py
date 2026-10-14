"""The GTM-engineering hub: a hand-written page that links the seven job pages, the workflows and
the public skills. It is only useful if every link on it resolves, it can be measured, and it
describes treg.to only where treg.to serves it."""

from __future__ import annotations

import json
import re

from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.config import get_settings

PATH = "/gtm-engineering"


async def test_hub_is_served_canonical_and_measurable(clients: AsyncClient):
    r = await clients.get(PATH)
    assert r.status_code == 200
    html = r.text
    assert '<link rel="canonical" href="' in html and f'{PATH}"/>' in html
    assert "{BASE}" not in html and "{ENDPOINTS}" not in html and "{PROVIDERS}" not in html
    # Hand-written pages are the ones PostHog sees; the hub exists partly to be measured.
    # The agent name in the H1 rotates in the browser; what a crawler reads must already be whole.
    assert '<span id="agName">Claude Code</span>' in html
    assert re.search(r"<h1>.*GTM engineering playbook.*Claude Code.*</h1>", html, re.S)
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert len(title.replace("&amp;", "&")) <= 62, title
    assert '<script src="/sitetrack.js"></script>' in html
    assert '<script src="/adtrack.js"></script>' in html
    kinds = []
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        kinds.append(json.loads(block)["@type"])
    assert kinds == ["BreadcrumbList", "Article", "FAQPage"]


async def test_every_internal_link_on_the_hub_resolves(clients: AsyncClient):
    """The hub is a page of links; a dead one is the failure nobody notices."""
    html = (await clients.get(PATH)).text
    # /app is the signed-in dashboard every Start button points at, not a page of this site.
    links = sorted({h for h in re.findall(r'href="(/[^"#?]*)', html)
                    if not h.startswith("//") and h != "/app"})
    assert "/workflows/find-and-verify-a-lead-list" in links and "/people-search" in links
    for href in links:
        r = await clients.get(href)
        assert r.status_code == 200, href


async def test_hub_is_linked_from_its_pillars_and_listed(clients: AsyncClient):
    for page in ("/", "/resources", "/people-search", "/leads-signals", "/blog", "/use-cases",
                 "/use-cases/lead-enrichment-for-ai-agents", "/workflows"):
        assert f'href="{PATH}"' in (await clients.get(page)).text, page
    assert f"{PATH}<" in (await clients.get("/sitemap.xml")).text
    # Hosted, the bundled pages and llms.txt keep the links with the markers unwrapped.
    for page in ("/resources", "/people-search", "/leads-signals", "/grokbot", "/fable",
                 "/use-cases/lead-enrichment-for-ai-agents", "/llms.txt"):
        r = await clients.get(page)
        assert r.status_code == 200 and "/gtm-engineering" in r.text, page
        assert "hosted-->" not in r.text, page


async def test_hub_404s_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get(PATH)).status_code == 404
            assert (await c.get(PATH + ".md")).status_code == 404
            assert f"{PATH}<" not in (await c.get("/sitemap.xml")).text
            # Nothing served on a self-hosted registry may send a reader (or an agent) to the 404.
            for page in ("/", "/resources", "/people-search", "/leads-signals", "/grokbot", "/fable",
                         "/use-cases/lead-enrichment-for-ai-agents", "/llms.txt"):
                r = await c.get(page)
                assert r.status_code == 200, page
                assert "/gtm-engineering" not in r.text, page
                assert "hosted-->" not in r.text, page
    finally:
        get_settings.cache_clear()


async def test_markdown_twin_carries_every_chapter(clients: AsyncClient):
    """The Markdown copy is hand-kept; a chapter added to the page and not to the twin fails here."""
    import html as _html
    page = (await clients.get(PATH)).text
    md = await clients.get(PATH + ".md")
    assert md.status_code == 200
    assert md.headers.get("x-robots-tag") == "noindex"
    assert "{BASE}" not in md.text
    norm = lambda s: re.sub(r"[“”\"]", '"', _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()
    headings = [norm(h) for h in re.findall(r'<h3 class="ct">(.*?)</h3>', page, re.S)]
    assert len(headings) >= 20
    md_headings = {norm(h) for h in re.findall(r"^## (.+)$", md.text, re.M)}
    missing = [h for h in headings if h not in md_headings]
    assert not missing, missing
    assert f'rel="alternate" type="text/markdown" href="' in page


async def test_studies_publish_results_without_naming_the_databases(clients: AsyncClient):
    """The three studies ship their headline figures inside their own sections, on the page and in
    the twin; their FAQ answers match the JSON-LD word for word; and no catalog provider is named in
    any study block: the claims are about stored records, signals and verdicts, not a vendor ranking."""
    import html as _html
    import json
    from pathlib import Path
    import treg
    page = (await clients.get(PATH)).text
    md = (await clients.get(PATH + ".md")).text

    def block(text, start, end):
        i = text.index(start)
        return text[i:text.index(end, i)]

    html_blocks = {
        "found-vs-deliverable": block(page, 'id="found-vs-deliverable"', '<ol class="play">'),
        "job-changes": block(page, 'id="job-changes"', '<ol class="play">'),
        "test-your-signals": block(page, 'id="test-your-signals"', '<ol class="play">'),
    }
    md_blocks = {
        "found-vs-deliverable": block(md, "Study, 7 Oct 2026: found is not deliverable", "Rule:"),
        "job-changes": block(md, "### Job changes", "Rule:"),
        "test-your-signals": block(md, "### Test a timing signal", "Rule:"),
    }
    expected = {
        "found-vs-deliverable": ["32 deliverable", "53%", "16 risky", "8 invalid", "4 unknown"],
        "job-changes": ["148", "68%", "64%", "69%", "84 of 139", "49 of"],
        "test-your-signals": ["57", "54", "30%", "24%", "47%", "48%", "26%", "54%"],
    }
    for key, figures in expected.items():
        assert f'href="#{key}"' in page
        for figure in figures:
            assert figure in html_blocks[key], (key, figure)
            assert figure in md_blocks[key], (key, figure)

    catalog = Path(treg.__file__).parent / "catalog"
    providers = {p.name.split(".")[0] for p in catalog.glob("*.yaml")} - {"adapters", "linkedin", "you"}  # the source the study names, and a common word
    studies = " ".join(list(html_blocks.values()) + list(md_blocks.values())).lower()
    named = sorted(p for p in providers if re.search(rf"\b{re.escape(p)}\b", studies))
    assert not named, named

    faq = next(json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
               if '"FAQPage"' in b)
    schema = {e["name"]: e["acceptedAnswer"]["text"] for e in faq["mainEntity"]}
    visible = {_html.unescape(q): _html.unescape(re.sub(r"<[^>]+>", "", a))
               for q, a in re.findall(r"<summary>([^<]*)</summary><div class=\"body\">(.*?)</div>", page, re.S)}
    assert 'id="what-is-a-gtm-engineer"' in page and "GTM engineer, RevOps or sales ops?" in page
    for q in ("What is the difference between a GTM engineer and RevOps?",
              "How accurate is contact data after someone changes jobs?",
              "Do hiring or news signals predict that a startup is about to raise?"):
        assert visible[q] == schema[q], q
