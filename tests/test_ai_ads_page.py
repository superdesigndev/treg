"""The AI ads playbook: a hand-written page on the /gtm-engineering layout. It is only useful if
every link on it resolves, it can be measured, its Markdown twin carries every step, and the
answer under the hero says the same thing in all three copies."""

from __future__ import annotations

import html as _html
import json
import re

from httpx import ASGITransport, AsyncClient

from treg.api import app
from treg.config import get_settings

PATH = "/ai-ads"


def _ld(page: str) -> list[dict]:
    return [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)]


async def test_page_is_served_canonical_and_measurable(clients: AsyncClient):
    r = await clients.get(PATH)
    assert r.status_code == 200
    page = r.text
    assert '<link rel="canonical" href="' in page and f'{PATH}"/>' in page
    assert "{BASE}" not in page and "{ENDPOINTS}" not in page and "{PROVIDERS}" not in page
    # The agent name in the H1 rotates in the browser; what a crawler reads must already be whole.
    assert '<span id="agName">Claude Code</span>' in page
    assert re.search(r"<h1>.*AI ads playbook.*Claude Code.*</h1>", page, re.S)
    title = re.search(r"<title>(.*?)</title>", page).group(1)
    assert len(title.replace("&amp;", "&")) <= 62, title
    assert '<script src="/sitetrack.js"></script>' in page
    assert [b["@type"] for b in _ld(page)] == ["BreadcrumbList", "Article", "FAQPage"]
    # House rule: no em dashes in page copy. The setup line is the one exception.
    assert "—" not in page and "&mdash;" not in page.replace("set up treg &mdash; https://treg.to/llms.txt", "")


async def test_every_internal_link_resolves(clients: AsyncClient):
    page = (await clients.get(PATH)).text
    # /app is the signed-in dashboard every Start button points at, not a page of this site.
    links = sorted({h for h in re.findall(r'href="(/[^"#?]*)', page) if not h.startswith("//") and h != "/app"})
    assert "/ugc" in links and "/use-cases/competitor-ad-research-for-ai-agents" in links
    for href in links:
        assert (await clients.get(href)).status_code == 200, href
    # Every in-page anchor points at something on the page.
    for anchor in sorted(set(re.findall(r'href="#([^"]+)"', page))):
        assert f'id="{anchor}"' in page, anchor


async def test_every_step_has_a_prompt_and_the_diagnostic_points_at_real_steps(clients: AsyncClient):
    page = (await clients.get(PATH)).text
    steps = re.findall(r'<section class="chapter" id="(ch\d+)" data-ch="\d+"', page)
    assert steps == [f"ch{n}" for n in range(12)]
    for n in range(1, 12):
        assert f'<pre id="p{n}">' in page, n
    for chs in re.findall(r'<label class="sym" data-ch="([^"]+)"', page):
        for ch in chs.split(","):
            assert ch in steps, ch
    # A step we have not run says so in its badge; the others carry the run date.
    badges = dict(re.findall(r'id="(ch\d+)" data-ch="\d+">\s*<div class="ch-k">.*?<span class="badge[^"]*">(.*?)</span>', page, re.S))
    assert all(badges[f"ch{n}"] == "Method" for n in (9, 10, 11))
    assert all(badges[f"ch{n}"].startswith("Run ") for n in range(1, 9))


async def test_page_is_linked_and_listed(clients: AsyncClient):
    for page in ("/resources", "/blog", "/llms.txt"):
        r = await clients.get(page)
        assert r.status_code == 200 and PATH in r.text, page
        assert "hosted-->" not in r.text, page
    assert f"{PATH}<" in (await clients.get("/sitemap.xml")).text


async def test_page_404s_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get(PATH)).status_code == 404
            assert (await c.get(PATH + ".md")).status_code == 404
            assert f"{PATH}<" not in (await c.get("/sitemap.xml")).text
            # Nothing served on a self-hosted registry may send a reader (or an agent) to the 404.
            for page in ("/resources", "/llms.txt"):
                r = await c.get(page)
                assert r.status_code == 200, page
                assert PATH not in r.text, page
    finally:
        get_settings.cache_clear()


async def test_markdown_twin_carries_every_step(clients: AsyncClient):
    """The Markdown copy is hand-kept; a step added to the page and not to the twin fails here."""
    page = (await clients.get(PATH)).text
    md = await clients.get(PATH + ".md")
    assert md.status_code == 200
    assert md.headers.get("x-robots-tag") == "noindex"
    assert "{BASE}" not in md.text and "—" not in md.text

    def norm(s):
        return re.sub(r"[“”‘’\"']", "'", _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()
    headings = [norm(h) for h in re.findall(r'<h3 class="ct">(.*?)</h3>', page, re.S)]
    assert len(headings) == 16
    md_headings = {norm(h) for h in re.findall(r"^## (.+)$", md.text, re.M)}
    assert not [h for h in headings if h not in md_headings]
    # Each prompt is the same text in both copies.
    for n in range(1, 12):
        prompt = _html.unescape(re.search(rf'<pre id="p{n}">(.*?)</pre>', page, re.S).group(1))
        # The page lets a prompt wrap to the screen; the twin keeps short lines.
        assert " ".join(prompt.split()) in " ".join(md.text.split()), n


async def test_the_answer_says_the_same_thing_in_all_three_copies(clients: AsyncClient):
    """"How do you make ads with Claude Code?" is written three times: the block under the hero, the
    first FAQPage answer and the Markdown twin. The other FAQ answers match their JSON-LD too."""
    page = (await clients.get(PATH)).text
    block = re.search(r'<section class="define" id="how-to-make-ads-with-claude-code">.*?<p>(.*?)</p>', page, re.S).group(1)
    visible = _html.unescape(re.sub(r"<[^>]+>", "", block)).strip()
    faq = next(b for b in _ld(page) if b["@type"] == "FAQPage")
    schema = {q["name"]: q["acceptedAnswer"]["text"] for q in faq["mainEntity"]}
    md = (await clients.get(PATH + ".md")).text
    assert visible == schema["How do you make ads with Claude Code?"]
    assert "## How do you make ads with Claude Code?\n\n" + visible + "\n" in md
    shown = {_html.unescape(q): _html.unescape(re.sub(r"<[^>]+>", "", a))
             for q, a in re.findall(r"<summary>([^<]*)</summary><div class=\"body\">(.*?)</div>", page, re.S)}
    assert len(shown) == len(schema) - 1
    for q, a in shown.items():
        assert a == schema[q], q
        assert a in md, q
