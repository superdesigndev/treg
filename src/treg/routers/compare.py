"""The comparison pages: `/compare` and `/compare/treg-vs-<slug>` (plus a `.md` twin of each page).

Every word comes from `compare_pages`; this module only lays it out. The pages describe treg.to's
own pricing and grant beside other companies' published terms, so like the agent pages they exist
on the hosted deployment only, and 404 on a self-hosted registry.
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from .. import compare_pages as cp
from ..config import get_settings
from ..domain.catalog import store as catalog_store
from .web import _GH, _esc_html, _fill_headline, _hosted, _provider_display

router = APIRouter()

_LOGO = ('<svg viewBox="0 0 512 512" aria-hidden="true"><rect width="512" height="512" rx="112" '
         'fill="#101010"/><rect x="111" y="111" width="140.5" height="140.5" rx="20" fill="#fff"/>'
         '<rect x="260.5" y="260.5" width="140.5" height="140.5" rx="20" fill="#fff"/></svg>')

def _e(s: str) -> str:
    return _esc_html(s)


def _np(text: str) -> bool:
    return text.startswith(cp.NOT_PUBLISHED) or text in ("Not applicable.",)


def _mono(key: str) -> str:
    c = cp.COMPETITORS[key]
    fill = " fill" if c.get("logo_fills") else ""
    return (f'<span class="mono logo{fill}" aria-hidden="true"><img src="{_e(c["logo"])}" alt="" '
            f'width="20" height="20" loading="lazy" decoding="async"></span>')


def _treg_mono() -> str:
    return f'<span class="mono treg" aria-hidden="true">{_LOGO}</span>'


def _respond(html: str, request: Request) -> Response:
    html = _fill_headline(html)
    etag = '"' + hashlib.sha256(html.encode("utf-8")).hexdigest()[:32] + '"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    sent = {t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")}
    if etag in sent:
        return Response(status_code=304, headers=headers)
    return HTMLResponse(html, headers=headers)


def _ld(blocks: list[dict]) -> str:
    # `<` as < so a stray "</script>" in copy cannot end the block; still valid JSON.
    return "\n".join('<script type="application/ld+json">'
                     + json.dumps(b, separators=(",", ":")).replace("<", "\\u003c") + "</script>"
                     for b in blocks)


def _shell(*, title: str, description: str, path: str, here: str, body: str, ld: list[dict],
           ref: str, modified: str, them_color: str = "#6366F1", nav: str = "") -> str:
    base = get_settings().public_url.rstrip("/")
    url = _e(base + path)
    t, d = _e(title), _e(description)
    soft = them_color + "24"   # the same colour at ~14% opacity, for the hero wash
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1"/>
<link rel="canonical" href="{url}"/>
<link rel="alternate" type="text/markdown" href="{url}.md"/>
<meta property="og:type" content="article"/>
<meta property="og:site_name" content="treg"/>
<meta property="og:locale" content="en_US"/>
<meta property="og:url" content="{url}"/>
<meta property="og:title" content="{t}"/>
<meta property="og:description" content="{d}"/>
<meta property="og:image" content="{base}/media/og.png"/>
<meta property="og:image:width" content="1200"/>
<meta property="og:image:height" content="630"/>
<meta property="og:image:alt" content="treg.to: one key for the whole tool catalog, priced per call"/>
<meta property="article:modified_time" content="{modified}"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:site" content="@treg_ai"/>
<meta name="twitter:title" content="{t}"/>
<meta name="twitter:description" content="{d}"/>
<meta name="twitter:image" content="{base}/media/og.png"/>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600&family=Inter:wght@400;500;600&family=Geist+Mono:wght@400;500&family=Geist+Pixel&display=swap">
<link rel="stylesheet" href="/media/compare/compare.css">
<style>:root{{--them:{_e(them_color)};--them-soft:{_e(soft)}}}</style>
{_ld(ld)}
</head>
<body>
<nav class="nav" id="nav">
  <span class="wordmark"><a href="/" aria-label="treg home">{_LOGO} treg</a> <span class="sl">/</span> <a class="here" href="/compare">Compare</a><span class="deep">{here}</span></span>
  <span class="nav-r">
    <span class="nav-links">{nav}<a href="/catalog">Catalog</a><a href="/pricing">Pricing</a><a href="{_GH}" target="_blank" rel="noopener">GitHub ↗</a></span>
    <a class="pill dark" href="/app?ref={_e(ref)}">Start free</a>
  </span>
</nav>
{body}
<footer class="bigfoot">
  <div class="bigfoot-in">
    <div class="bf-brand">
      <span class="bfb">{_LOGO} treg</span>
      <div class="note2">100% open source</div>
    </div>
    <nav class="bf-cols" aria-label="Site">
      <div class="bf-col"><div class="lab">Explore</div>
        <a href="/catalog">Catalog</a><a href="/compare">Compare</a><a href="/use-cases">Use cases</a><a href="/rankings">Rankings</a><a href="/showcase">Showcase</a><a href="/blog">Blog</a></div>
      <div class="bf-col"><div class="lab">Build</div>
        <a href="/tutorial">Docs</a><a href="/docs">API</a><a href="/llms.txt">llms.txt</a><a href="{_GH}" target="_blank" rel="noopener">GitHub ↗</a></div>
      <div class="bf-col"><div class="lab">Company</div>
        <a href="/pricing">Pricing</a><a href="/resources">Resources</a><a href="/support">Support</a><a href="/terms">Terms</a><a href="/privacy">Privacy</a></div>
    </nav>
  </div>
  <div class="bf-tag">© 2026 treg — OpenRouter for agent tools.</div>
</footer>
<script>
(function () {{
  var nav = document.getElementById('nav');
  addEventListener('scroll', function () {{ nav.classList.toggle('scrolled', scrollY > 8); }}, {{ passive: true }});
  var hero = document.querySelector('.hero');
  if (hero) requestAnimationFrame(function () {{ requestAnimationFrame(function () {{ hero.classList.add('in'); }}); }});
  var els = document.querySelectorAll('.rv');
  if (!('IntersectionObserver' in window)) {{ els.forEach(function (el) {{ el.classList.add('in'); }}); }}
  else {{
    var io = new IntersectionObserver(function (es) {{ es.forEach(function (e) {{ if (e.isIntersecting) {{ e.target.classList.add('in'); io.unobserve(e.target); }} }}); }}, {{ rootMargin: '0px 0px -8% 0px' }});
    els.forEach(function (el) {{ io.observe(el); }});
  }}
  document.querySelectorAll('.cmd-tabs button').forEach(function (b) {{
    b.addEventListener('click', function () {{
      var dev = b.getAttribute('data-tab') === 'dev';
      document.querySelectorAll('.cmd-tabs button').forEach(function (o) {{ o.setAttribute('aria-selected', String(o === b)); }});
      document.getElementById('cmdLine').hidden = dev; document.getElementById('cmdDev').hidden = !dev;
    }});
  }});
  var copy = document.getElementById('cmdCopy');
  if (copy) copy.addEventListener('click', function () {{
    var line = document.querySelector('.cmd .cline:not([hidden])');
    if (!line || !navigator.clipboard) return;
    navigator.clipboard.writeText(line.textContent.trim()).then(function () {{
      copy.textContent = 'copied ✓'; setTimeout(function () {{ copy.textContent = 'copy'; }}, 1600);
    }});
  }});
  /* the 0% band types its equation once when it scrolls in; the markup ships written out for
     crawlers and readers without JS */
  var band = document.getElementById('zeroband'), eq = document.getElementById('zeqtxt');
  if (band && eq && 'IntersectionObserver' in window && !matchMedia('(prefers-reduced-motion: reduce)').matches) {{
    var segs = [['provider rate ', 0], ['+ $0.000 markup', 1], [' = your price', 0]];
    var total = segs.reduce(function (n, x) {{ return n + x[0].length; }}, 0);
    var render = function (n) {{
      var h = '', c = 0;
      segs.forEach(function (x) {{ var t = x[0].slice(0, Math.max(0, Math.min(x[0].length, n - c))); h += x[1] ? '<b>' + t + '</b>' : t; c += x[0].length; }});
      eq.innerHTML = h;
    }};
    render(0);
    var bio = new IntersectionObserver(function (es) {{ es.forEach(function (e) {{
      if (!e.isIntersecting) return;
      bio.disconnect();
      var n = 0, tick = setInterval(function () {{ render(++n); if (n >= total) clearInterval(tick); }}, 38);
    }}); }}, {{ threshold: .5 }});
    bio.observe(band);
  }}
}})();
</script>
<script src="/sitetrack.js"></script>
<script src="/adtrack.js"></script>
<script src="/gtag.js"></script>
</body>
</html>"""


def _crumbs_ld(base: str, trail: list[tuple[str, str]]) -> dict:
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i, "name": name, "item": base + path}
        for i, (name, path) in enumerate(trail, 1)]}


def _cell(key: str, row: str, treg: bool) -> str:
    c = cp.COMPETITORS[key]
    if treg:
        return c.get("treg_cells", {}).get(row, cp.TREG_CELLS[row])
    return c["cells"].get(row, cp.NOT_PUBLISHED)


def _others(key: str, n: int = 6) -> list[str]:
    """Half from the page's own hub group, half from the others. Both halves start at an offset
    that moves with the page, so every comparison is linked from about as many others."""
    keys = list(cp.COMPETITORS)
    group = cp.COMPETITORS[key]["group"]
    ring = keys[keys.index(key) + 1:] + keys[:keys.index(key)]
    same = [k for k in ring if cp.COMPETITORS[k]["group"] == group]
    rest = [k for k in keys if cp.COMPETITORS[k]["group"] != group]
    if rest:
        pos = [k for k in keys if cp.COMPETITORS[k]["group"] == group].index(key)
        at = (pos * (n - n // 2)) % len(rest)
        rest = rest[at:] + rest[:at]
    picked = same[:n // 2]
    return picked + rest[:n - len(picked)]


def _card(key: str) -> str:
    c = cp.COMPETITORS[key]
    return (f'<a class="ccard" href="/compare/{cp.page_slug(key)}">'
            f'<span class="t"><span class="side">{_treg_mono()}treg</span><em>vs</em>'
            f'<span class="side">{_mono(key)}{_e(cp.short(key))}</span></span>'
            f'<p>{_e(c["sub"])}</p><span class="go">Read the comparison →</span></a>')


def _page_body(key: str, base: str) -> str:
    c = cp.COMPETITORS[key]
    name, short = c["name"], cp.short(key)
    spec_rows_us = "".join(f'<div><dt>{_e(lab)}</dt><dd>{_e(cp.TREG_SPEC[k])}</dd></div>'
                           for k, lab in cp.SPEC_LABELS)
    spec_rows_them = "".join(
        f'<div><dt>{_e(lab)}</dt><dd{" class=np" if _np(c["spec"][k]) else ""}>{_e(c["spec"][k])}</dd></div>'
        for k, lab in cp.SPEC_LABELS)
    note = (f'<div class="note rv"><b>Recent change.</b> <span>{_e(c["note"])}</span></div>'
            if c.get("note") else "")

    rows = []
    for group, items in cp.rows_for(key):
        rows.append(f'<tr class="grp"><th colspan="3" scope="colgroup">{_e(group)}</th></tr>')
        for row, label in items:
            us, them = _cell(key, row, True), _cell(key, row, False)
            rows.append(
                f'<tr><th scope="row">{_e(label)}</th>'
                f'<td class="us" data-who="treg">{_e(us)}</td>'
                f'<td class="{"np" if _np(them) else ""}" data-who="{_e(short)}">{_e(them)}</td></tr>')

    def li_pairs(items: list[tuple[str, str]]) -> str:
        return "".join(f'<li><span class="n">{i:02d}</span><p><b>{_e(a)}</b> {_e(b)}</p></li>'
                       for i, (a, b) in enumerate(items, 1))

    faq = "".join(f"<details><summary><h3>{_e(q)}</h3></summary><p>{_e(a)}</p></details>" for q, a in c["faq"])
    sources = "".join(
        f'<li><a href="{_e(u)}" rel="nofollow noopener" target="_blank">{_e(lab)}</a><small>{_e(u)}</small></li>'
        for lab, u in c["sources"])
    others = "".join(_card(k) for k in _others(key))
    pid = cp.catalog_provider(key)
    in_catalog = ""
    if catalog_store.load().for_provider(pid):
        shown = _e(_provider_display(pid))
        in_catalog = (f'<p class="incat">{shown} is in the treg catalog too: '
                      f'<a href="/tools/{_e(pid)}">see every {shown} tool on treg, with its price</a>.</p>')

    return f"""<header class="hero">
  <div class="crumbs"><a href="/">treg</a> / <a href="/compare">Compare</a> / treg vs {_e(short)}</div>
  <h1>treg <span class="vs">vs</span> {_e(short)}</h1>
  <p class="sub">{_e(c["sub"])}</p>
  <div class="checked"><i></i>Read from {_e(name)}'s public pages on {cp.checked_label(key)}</div>
  <div class="duel">
    <div class="spec us"><div class="who">{_treg_mono()}treg<small>treg.to</small></div><dl>{spec_rows_us}</dl></div>
    <div class="medal" aria-hidden="true">vs</div>
    <div class="spec them"><div class="who">{_mono(key)}{_e(short)}<small>{_e(c["site"].split("//")[1].split("/")[0].removeprefix("www."))}</small></div><dl>{spec_rows_them}</dl></div>
  </div>
  <div class="ctas"><a class="pill dark" href="/app?ref=compare-{cp.page_slug(key)}">Try treg free — $1.00 credit</a><a class="pill" href="#side-by-side">See the side by side ↓</a></div>
</header>

<nav class="jump" aria-label="On this page"><a href="#answer">Short answer</a><a href="#fit">Which one fits</a><a href="#side-by-side">Side by side</a><a href="#their-strengths">Where {_e(short)} does best</a><a href="#treg-differs">Where treg is the better choice</a><a href="#both">Using both</a><a href="#faq">FAQ</a><a href="#sources">Sources</a></nav>

<main>
<section class="sec" id="answer"><div class="wrap">
  <div class="answer rv"><h2 class="eyebrow">treg vs {_e(short)}: the short answer</h2><p>{_e(c["tldr"])}</p></div>
  {note}
</div></section>

<section class="sec" id="fit"><div class="wrap">
  <h2 class="rv">Which one fits</h2>
  <p class="lede rv">Different tools fit different jobs. Here is when each one is the better choice.</p>
  <div class="fits rv">
    <div class="fit them"><h3>{_mono(key)}Choose {_e(short)} if</h3><ul>{"".join(f"<li>{_e(x)}</li>" for x in c["fit_them"])}</ul></div>
    <div class="fit us"><h3>{_treg_mono()}Choose treg if</h3><ul>{"".join(f"<li>{_e(x)}</li>" for x in c["fit_treg"])}</ul></div>
  </div>
</div></section>

<section class="sec" id="side-by-side"><div class="wrap">
  <h2 class="rv">treg vs {_e(short)}, side by side</h2>
  <p class="lede rv">Each {_e(short)} cell restates {_e(name)}'s own public pages on {cp.checked_label(key)}. "Not published" means those pages say nothing about it, not that the feature does not exist.</p>
  <div class="tablewrap rv"><table class="vs-table">
    <thead><tr><th scope="col"><span class="sr">Feature</span></th><th scope="col" class="us"><span>{_treg_mono()}treg</span></th><th scope="col"><span>{_mono(key)}{_e(short)}</span></th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
  <p class="tnote">treg's catalog size is read live from the catalog. Prices change; check each product's own pricing page before you decide.</p>
</div></section>

<section class="zeroband rv" id="zeroband">
  <h2>0% <i>additional fee.</i></h2>
  <div class="zeq"><span class="ps">$</span><code id="zeqtxt">provider rate <b>+ $0.000 markup</b> = your price</code><span class="zcaret"></span></div>
  <div class="zsub">we earn on volume pricing with vendors - not on you</div>
</section>

<section class="sec" id="tradeoff"><div class="wrap">
  <p class="eyebrow rv">The trade-off</p>
  <h2 class="rv">What each one does better</h2>
  <div class="ledger rv">
    <div class="col them" id="their-strengths">
      <h3>{_mono(key)}Where {_e(short)} does best</h3>
      <ol>{li_pairs(c["better"])}</ol>
    </div>
    <div class="col us" id="treg-differs">
      <h3>{_treg_mono()}Where treg is the better choice</h3>
      <ol>{li_pairs(c["different"])}</ol>
    </div>
  </div>
</div></section>

<section class="sec" id="both"><div class="wrap">
  <div class="both rv">
    <div><p class="eyebrow" style="color:#8B8985">Not either-or</p><h2>Using {_e(short)} and treg together</h2><p>{_e(c["both"])}</p>{in_catalog}</div>
    <div class="flow" aria-hidden="true">
      <div class="agent">your agent</div>
      <div class="lanes">
        <div class="lane"><b>{_mono(key)}{_e(short)}</b><span>{_e(c["better"][0][0].rstrip("."))}</span></div>
        <div class="lane us"><b>{_treg_mono()}treg</b><span>{_e(c["different"][0][0].rstrip("."))}</span></div>
      </div>
    </div>
  </div>
</div></section>

<section class="setup rv" id="setup">
  <h2>Give your agent <br>every tool in the catalog.</h2>
  <div class="cmd-tabs" role="tablist">
    <button type="button" role="tab" aria-selected="true" data-tab="agent"><span class="ct-ico"><img src="/media/people-search/claude-code.png" alt="" width="18" height="18"><img src="/media/people-search/codex.png" alt="" width="18" height="18"><img src="/media/people-search/openclaw.png" alt="" width="18" height="18"></span>Connect to your agent</button>
    <button type="button" role="tab" aria-selected="false" data-tab="dev">Developers</button>
  </div>
  <div class="cmd">
    <span class="ps">$</span>
    <span class="cline" id="cmdLine">set up treg — <a href="{_e(base)}/llms.txt">{_e(base)}/llms.txt</a></span>
    <span class="cline" id="cmdDev" hidden>Read <a href="{_e(base)}/integrate.md">{_e(base)}/integrate.md</a> and integrate treg into our product, including per-customer usage tracking and billing.</span>
    <button id="cmdCopy" type="button">copy</button>
  </div>
  <div class="ctas"><a class="pill dark" href="/app?ref=compare-{cp.page_slug(key)}-setup">Get started</a></div>
</section>

<section class="sec" id="faq"><div class="wrap narrow">
  <h2 class="rv">Frequently Asked Questions</h2>
  <div class="faq rv">{faq}</div>
</div></section>

<section class="sec" id="sources"><div class="wrap narrow">
  <div class="sources rv">
    <h2 class="eyebrow">Sources and method</h2>
    <ol>{sources}</ol>
    <p>We read these pages on {cp.checked_label(key)}. treg's own facts come from its <a href="/pricing">pricing</a>, <a href="/terms">terms</a> and <a href="{_GH}" target="_blank" rel="noopener">code</a>. We left out what we could not check, and we do not guess why a product works the way it does.</p>
    <p>Something wrong or out of date? Write to <a href="mailto:{cp.CORRECTIONS}?subject=Correction%3A%20treg%20vs%20{_e(short)}">{cp.CORRECTIONS}</a> and we will fix it. The same page as text: <a href="/compare/{cp.page_slug(key)}.md">markdown</a>.</p>
  </div>
</div></section>

<section class="sec"><div class="wrap">
  <h2 class="rv">Other comparisons</h2>
  <div class="cards rv">{others}</div>
  <p class="tnote"><a href="/compare">All comparisons</a> · <a href="/pricing">Pricing</a> · <a href="/catalog">Tool catalog</a></p>
</div></section>
</main>

<section class="final rv">
  <h2>One token for {"{ENDPOINTS}"} tools</h2>
  <p>Your agent searches by the job, reads the price, and calls the tool. Your own keys and accounts stay free.</p>
  <div class="ctas"><a class="pill dark" href="/app?ref=compare-{cp.page_slug(key)}">Start free</a><a class="pill" href="/catalog">Browse the catalog</a></div>
</section>"""


def _treg_app_ld(base: str) -> dict:
    return {"@type": "SoftwareApplication", "@id": base + "/#software", "name": "treg",
            "url": base + "/", "applicationCategory": "DeveloperApplication"}


def _page_ld(key: str, base: str) -> list[dict]:
    c = cp.COMPETITORS[key]
    url = f"{base}/compare/{cp.page_slug(key)}"
    return [
        {"@context": "https://schema.org", "@type": "WebPage", "name": c["title"], "url": url,
         "description": c["description"], "inLanguage": "en", "dateModified": cp.checked(key),
         "isPartOf": {"@type": "WebSite", "name": "treg", "url": base + "/"},
         "about": [_treg_app_ld(base),
                   {"@type": "SoftwareApplication", "name": c["name"], "url": c["site"],
                    "applicationCategory": "DeveloperApplication"}]},
        _crumbs_ld(base, [("treg", "/"), ("Compare", "/compare"),
                          (f"treg vs {cp.short(key)}", f"/compare/{cp.page_slug(key)}")]),
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in c["faq"]]},
    ]


def _page_md(key: str, base: str) -> str:
    c = cp.COMPETITORS[key]
    short = cp.short(key)
    out = [f"# treg vs {short}", "", c["sub"], "",
           f"Read from {c['name']}'s public pages on {cp.checked_label(key)}. HTML page: "
           f"{base}/compare/{cp.page_slug(key)}", "", f"## treg vs {short}: the short answer", "", c["tldr"], ""]
    if c.get("note"):
        out += [f"Recent change: {c['note']}", ""]
    out += ["## Which one fits", "", f"Choose {short} if:", ""]
    out += [f"- {x}" for x in c["fit_them"]] + ["", "Choose treg if:", ""]
    out += [f"- {x}" for x in c["fit_treg"]] + ["", f"## treg vs {short}, side by side", "",
            '"Not published" means the product\'s pages say nothing about it.', ""]
    for group, items in cp.rows_for(key):
        out += [f"### {group}", "", f"| | treg | {short} |", "|---|---|---|"]
        out += [f"| {label} | {_cell(key, row, True)} | {_cell(key, row, False)} |" for row, label in items]
        out.append("")
    out += [f"## Where {short} does best", ""]
    out += [f"- **{a}** {b}" for a, b in c["better"]]
    out += ["", "## Where treg is the better choice", ""] + [f"- **{a}** {b}" for a, b in c["different"]]
    out += ["", f"## Using {short} and treg together", "", c["both"], "", "## Frequently Asked Questions", ""]
    for q, a in c["faq"]:
        out += [f"### {q}", "", a, ""]
    out += ["## Sources and method", ""]
    out += [f"{i}. [{lab}]({u})" for i, (lab, u) in enumerate(c["sources"], 1)]
    out += ["", f"Read on {cp.checked_label(key)}. Corrections: {cp.CORRECTIONS}.", "",
            f"All comparisons: {base}/compare", ""]
    return "\n".join(out)


def _hub_body() -> str:
    groups = []
    for gid, title, lede in cp.GROUPS:
        keys = [k for k, c in cp.COMPETITORS.items() if c["group"] == gid]
        groups.append(f'<section class="group rv" id="{gid}"><h2>{_e(title)}</h2>'
                      f'<p class="lede">{_e(lede)}</p><div class="cards">'
                      + "".join(_card(k) for k in keys) + "</div></section>")
    return f"""<header class="hero">
  <div class="crumbs"><a href="/">treg</a> / Compare</div>
  <h1>How treg compares</h1>
  <p class="sub">treg is like OpenRouter for tools: one API key and one balance for many paid APIs. These are honest, side-by-side comparisons with the other ways an AI agent gets tools and data, and each one says where the other product is the better choice.</p>
  <div class="checked"><i></i>Each page says the day its facts were read, most recently {cp.checked_label()}</div>
  <div class="ctas"><a class="pill dark" href="/app?ref=compare">Start free — $1.00 credit</a><a class="pill" href="/catalog">Browse the catalog</a></div>
</header>
<main class="wrap">
  <div class="method rv">
    <div><b>Their words, their pages.</b>Every fact about another product comes from its own site, docs or pricing page, and is linked.</div>
    <div><b>"Not published" is not "no".</b>When a page says nothing, we say so, rather than guess that a feature is missing.</div>
    <div><b>Dated, and open to fixes.</b>Prices change. Each page says when we read it; write to {cp.CORRECTIONS} with a correction.</div>
  </div>
  {"".join(groups)}
</main>
<section class="final rv">
  <h2>One token for {"{ENDPOINTS}"} tools</h2>
  <p>Your agent searches by the job, reads the price, and calls the tool. Your own keys and accounts stay free.</p>
  <div class="ctas"><a class="pill dark" href="/app?ref=compare">Start free</a><a class="pill" href="/pricing">See pricing</a></div>
</section>"""


HUB_TITLE = "Compare treg: MCP and API gateways for AI agents"
HUB_DESC = ("treg is like OpenRouter for tools. Honest comparisons with Composio, Zapier MCP, Apify, Clay, "
            "Apollo, RapidAPI, AnyAPI and more.")


@router.get("/compare", include_in_schema=False)
async def compare_hub(request: Request):
    """The hub every comparison hangs from, grouped by the job each product does."""
    if not _hosted():
        raise HTTPException(status_code=404, detail="not found")
    base = get_settings().public_url.rstrip("/")
    ld = [
        {"@context": "https://schema.org", "@type": "CollectionPage", "name": HUB_TITLE,
         "url": base + "/compare", "description": HUB_DESC, "dateModified": cp.checked(),
         "about": _treg_app_ld(base),
         "mainEntity": {"@type": "ItemList", "itemListElement": [
             {"@type": "ListItem", "position": i, "name": f"treg vs {cp.short(k)}",
              "url": f"{base}/compare/{cp.page_slug(k)}"}
             for i, k in enumerate(cp.COMPETITORS, 1)]}},
        _crumbs_ld(base, [("treg", "/"), ("Compare", "/compare")]),
    ]
    html = _shell(title=HUB_TITLE, description=HUB_DESC, path="/compare", here="", body=_hub_body(),
                  ld=ld, ref="compare", modified=cp.checked(), them_color="#118453",
                  nav='<a href="#per-call">Paid tools</a><a href="#apps">App actions</a>')
    return _respond(html, request)


@router.get("/compare/{slug}", include_in_schema=False)
async def compare_page(slug: str, request: Request):
    """One comparison, `/compare/treg-vs-<key>`; `.md` on the end serves the same page as text."""
    if not _hosted():
        raise HTTPException(status_code=404, detail="not found")
    as_md = slug.endswith(".md")
    key = slug.removesuffix(".md").removeprefix("treg-vs-")
    if not slug.startswith("treg-vs-") or key not in cp.COMPETITORS:
        raise HTTPException(status_code=404, detail="not found")
    base = get_settings().public_url.rstrip("/")
    if as_md:
        return PlainTextResponse(_fill_headline(_page_md(key, base)), media_type="text/markdown; charset=utf-8",
                                 headers={"X-Robots-Tag": "noindex", "Cache-Control": "no-cache"})
    c = cp.COMPETITORS[key]
    html = _shell(title=c["title"], description=c["description"], path=f"/compare/{cp.page_slug(key)}",
                  here=f' <span class="sl">/</span> <span class="here">vs {_e(cp.short(key))}</span>',
                  body=_page_body(key, base), ld=_page_ld(key, base),
                  ref=f"compare-{cp.page_slug(key)}", modified=cp.checked(key), them_color=c["mono"],
                  nav='<a href="#side-by-side">Side by side</a><a href="#faq">FAQ</a>')
    return _respond(html, request)


def sitemap_paths() -> list[tuple[str, str]]:
    """Each page with the day its facts were read, for the sitemap's lastmod."""
    return [("/compare", cp.checked())] + [(f"/compare/{cp.page_slug(k)}", cp.checked(k))
                                           for k in cp.COMPETITORS]
