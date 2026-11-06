---
name: compare-pages
description: >
  Add, refresh or audit a treg.to/compare page ("treg vs X"). Use when asked to compare treg with
  another product, add a competitor page, update a comparison's prices or facts, write or fix its
  FAQ, check how the comparison pages rank or show up in AI answers, or make their share images.
  Walks research with the treg CLI (the competitor's own pages, keyword demand, Google results, AI
  answers), the honesty rules, the record in compare_pages.py, SEO checks, the share image, and
  the tests. Keyword numbers and monitoring results never go in this public repo.
metadata:
  internal: true  # repo tooling: hidden from `npx skills add superdesigndev/treg`
---

# Comparison pages — treg.to/compare

A comparison page is **one record** in `src/treg/compare_pages.py` (`COMPETITORS[key]`). The
renderer `src/treg/routers/compare.py` holds no copy. Read `docs/context/interface/seo.md` (the
`/compare` row and "The social card") before you change the mechanism.

| What | Where |
|---|---|
| Copy, facts, sources, FAQ | `src/treg/compare_pages.py` (one record per product, plus `TREG_CELLS`, `TREG_SPEC`) |
| Page, structured data, links | `src/treg/routers/compare.py` |
| Styles, logos | `src/treg/web/media/compare/compare.css`, `media/compare/logos/` (or `web/logos/` if the catalog already has it) |
| Share images | `scripts/og_images.py` → `src/treg/web/media/og/compare/<slug>.jpg` |
| Tests | `tests/test_compare_pages.py` |

Methods follow the Semrush skills (https://github.com/semrush/skills: keyword-research,
serp-analysis, technical-seo-audit, schema-markup, internal-link-audit), with the treg CLI as the
data source instead of the Semrush MCP. `references/research-with-treg.md` has every command.

## The rules that never bend

`references/honesty-rules.md` is the full list. The short form:

1. **Every fact about the other product comes from its own pages,** with the URL in `sources` and
   the read day in `checked`. If its pages say nothing, the cell says "Not published", never "No".
2. **Never talk treg down.** State the other product's real strengths plainly, state treg's
   strengths plainly, and let the reader weigh them. No "only", "just", "a few", "lacks" about treg.
3. **treg facts only from `TREG_CELLS` / `TREG_SPEC`.** Never invent or promise a treg feature.
4. **Every link to another site is `rel="nofollow"`** (a test enforces it).
5. **No catalog counts in the FAQ**; elsewhere use `{ENDPOINTS}` / `{PROVIDERS}`.
6. Check AGENTS.md and the team's rules before naming a competitor outside the /compare copy
   (commit messages, PR bodies, other docs).

## Add a page

1. **Pick the product and check the catalog.** `treg catalog search "<product>"`: if its endpoints
   are already in the catalog, the page must say so warmly (see the `anyapi` and `apollo` records)
   and set `provider` if the catalog id differs from the key.
2. **Research its own pages** (home, pricing, docs, MCP, terms, GitHub, changelog). Write
   `.context/compare-research/<key>.md` (gitignored): each fact with its URL, plus 6-8 real buyer
   questions. Use WebFetch, or the treg CLI web readers when a site blocks plain fetches.
3. **Research demand** (`references/research-with-treg.md`): keyword volume and difficulty for
   "<X> alternative(s)", "<X> pricing", "<X> mcp", "<X> api", "<X> vs ..."; the Google results and
   "People also ask" for the top two; what ChatGPT answers for 2-3 buyer prompts. Keep the numbers
   in `.context/`; they never enter the repo.
4. **Write the record.** Copy the key set of an existing record. Limits:
   - `title` ≤ 60 characters, "treg vs X: ..." with the page's top search in it.
   - `description` 110-135 characters, keyword early, a treg strength stated.
   - `sub` ≤ 120 characters; open with what sets the pair apart, not "Both ..."; no two subs on
     the hub open the same way.
   - `tldr` 3-5 sentences: what both share, treg's concrete strengths, the other product's
     distinct strength, and one plain category sentence ("treg is like OpenRouter for tools...").
   - `better` 3-5, `different` 4-5, `fit_them` / `fit_treg` 4-5 each, `both` one paragraph.
   - `faq` 6-7, in this mix: 3-4 about the other product that people search (alternative,
     pricing, MCP / API, a product-specific worry), 2 naming both products, 1-2 about treg on the
     page's topic. Each answer starts with a direct answer, names both products, ≤ about 60 words,
     and no two answers give the same fact.
   - `sources` 6-8 links: the pages that back the facts, no duplicates.
5. **Logo.** The product's own mark, SVG if possible, else a square PNG of 128-256 px (it shows
   at 28 px at most). Set `logo_fills` and `mono` (a hex colour) like the other records.
6. **Share image.** Ask the person driving the session whether to make the page's share image now
   (`uv run --frozen python scripts/og_images.py /compare/treg-vs-<slug>`), and tell them a
   different look is one more template in `LAYOUTS`. A comparison without one fails
   `test_every_comparison_shares_its_own_image`, so for comparisons the answer is usually yes.
7. **Check it** (`references/audit-checklist.md`): run `uv run --with pytest-xdist pytest -n auto
   -q tests/test_compare_pages.py`, load the page as treg.to (the pages render only when
   `TREG_PUBLIC_URL` is treg.to), check desktop and a 390 px phone, and scan the copy for words
   that talk treg down.
8. **Commit** the record, logo and share image together; follow AGENTS.md (drift check, docs in
   the same commit when the mechanism changes).

## Refresh a page

Re-read every source. Change a fact only where the source changed, then move that record's
`checked` to the read day. When copy changes without new facts, move `UPDATED`. Re-run the share
image for any page whose headline or `sub` changed. The page's `dateModified` and sitemap
`lastmod` follow `modified()` on their own.

## Monthly AI-answer check

Once a month, run the prompt list through ChatGPT, Google AI Mode / AI Overviews and Perplexity
with the treg CLI (`references/research-with-treg.md`, "AI answers") and record, per prompt,
whether treg is named, where, which treg URL is cited and which competitors are named. The prompt
list, the baseline and every result live in the private `treg-internal` repository
(its marketing SEO folder, `compare-pages.md`), never here. Use what you find to choose the next page or FAQ fix.
