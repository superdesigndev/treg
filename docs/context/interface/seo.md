---
title: Search surfaces — robots, sitemap, the crawlable catalog, and the social card
status: shipped
sources:
  - src/treg/api.py
  - src/treg/routers/web.py
  - src/treg/agent_pages.py
  - src/treg/web/robots.txt
  - src/treg/web/catalog.css
  - src/treg/web/usecase.css
  - src/treg/web/index.html
  - src/treg/web/landing.html
  - src/treg/web/people-search.html
  - src/treg/web/grokbot.html
  - src/treg/web/fable-gtm.html
  - src/treg/web/llms.txt
  - scripts/indexnow_submit.py
  - src/treg/web/support.html
  - assets/brand/og-card.html
related:
  - interface/api.md
  - interface/dashboard.md
  - architecture/catalog.md
---

# Search surfaces

Everything a crawler, a link unfurler or an AI answer engine sees. It is one subsystem because the
pieces only work together: a sitemap is worthless without pages to list, and pages are worthless if
`HEAD` 405s before the crawl starts.

## The problem this fixed

The catalog — ~2,630 endpoints across 80 platform shelves, the entire substance of the product — had
**no URLs**. The dashboard browses platforms through hash routes (`/app#platform/<slug>`) behind a
login, and individual endpoints were expandable rows with no address at all. A crawler could reach
six thin marketing pages and nothing else. On top of that: no `robots.txt`, no `sitemap.xml`, `HEAD`
answering 405 everywhere, no `og:`/`twitter:` tags or image, no structured data, and `/docs` serving
FastAPI's stock Swagger shell — a kilobyte of JavaScript to anything that does not run scripts.

## The pieces

| Path | What it is |
|---|---|
| `/robots.txt` | Bundled file, `{BASE}`-templated. Disallows `/app`, `/login`, auth and OAuth flows, `/call/`, `/mcp`, `/admin`, `/docs/api`. Names the sitemap. |
| `/sitemap.xml` | **Generated**, not bundled — 80 of its URLs come from the catalog. Static pages take `lastmod` from their file's mtime, shelves from the newest mtime under `src/treg/catalog/`. |
| `/resources` + `/use-cases/<slug>` | The outcome pages and their hub. Their sitemap rows are spread from `_USE_CASES` rather than listed by hand, so routing a new page lists it — see below. |
| `/people-search` + `/grokbot` + `/fable` | The launch-campaign landings, bundled files served by their own routes (`FileResponse`, no-cache). All are first-class pages: canonical, OG tags, listed in `_SITEMAP_PAGES` at 0.8 — `/people-search` is "Claude for people search" (the enrichment launch film's destination), `/grokbot` the "Grok Bot for Outreach" animatic plus the six-bot treg team gallery (ICP Map Coach, Lookalike Scout, Rival Watch Desk, SERP Watch Team, Creator Shortlist Crew, and GTM Expert, each linked directly to its `x.ai/bot/…` page). Its nav, hero and closing CTAs remain the pair **"Setup treg"**, primary — signed in it goes to `/app`, signed out it opens the page's own sign-in modal in place (the `/people-search` pattern; the `href` `/app?ref=grokbot` is only the no-JS fallback) and stashes `treg-ref=grokbot` so the first-run welcome preselects Grok Bot, and **"Install plugin"** → the x.ai plugin page, secondary. `/fable` (file `fable-gtm.html`) is the Claude Fable 5.1 launch: one terminal session that reads the market, plans, spawns four agents and shows one result window at a time. Their asset paths are **relative** (`media/…`, `logos/…`) so the same file previews from `file://`; that only holds while the routes stay slashless. All are registered in `bootstrap.py`'s route-ownership manifest like every other route. |
| `/catalog` | The dashboard SPA, in public mode — the marketplace's Catalog view on an indexable URL. |
| `/catalog/<slug>` | The same SPA, on the platform view for one shelf. |
| `/tools/<service>` | The catalog sliced by **vendor**, fully server-rendered (`_page`, no SPA): one public page per provider. **Title and H1 match** to describe the real listing: metered providers get `{Provider}: {n} tools from {price}` (title adds `API pricing:` prefix for SEO), own-account providers get `{Provider}: connect your own account`. The page shows logo, category, blurb from the oauth-provider registry, setup/MCP instructions, up to 8 tools per platform (with "See all N on the catalog" link for larger sets), why-treg cards, alternatives, and a metered-vs-own-account FAQ. JSON-LD: BreadcrumbList (with `treg.to` not bare `treg`), ItemList, FAQPage, HowTo. Tool counts and prices are live from `catalog_store`, never hardcoded. No em-dashes in page copy. There is no provider index page: /providers earned no searches and the provider links live in /catalog's prerender instead. `/tools/<service>` is safe from shadowing the API (the API's GETs are `/tools` and `/tools/by-name/…`). A signed-out `GET /app/marketplace/<service>` 302s to `/tools/<service>`. `tests/test_provider_pages.py` pins the route shape. |
| `/docs` | Server-rendered API reference built from `app.openapi()`. |
| `/docs/api` | FastAPI's Swagger UI, moved here and `Disallow`ed. ReDoc is off. |
| `/media/og.png` | The 1200×630 social card, served by the pre-existing `/media` mount. |
| `/media/brand/*` | Stable hot-linkable brand files — `logo.png` (512² square mark, the Organization JSON-LD `logo` and the apple-touch-icon), `logotype.png` (white wordmark), `wordmark-black.png`, `mark-{black,white}.svg`, `avatar-*`. Copies of `assets/brand/twitter/`; the favicon is the same mono mark. |
| `/catalog.css` | Skin for `/docs`. The catalog URLs need none — they ship the dashboard's own stylesheet, because they ship the dashboard. |

The `/grokbot` gallery mirrors the public Grok share-card silhouette without importing that dark
page wholesale: `.bot-deck` is a uniform three-column grid whose six cards carry the workflow order
instead of a separate legend, each `.bot-card` splits a layered Grok-style dark-teal vignette stage from treg's light copy surface, and the six
`.bot-face` variants preserve the templates' upright capsule with mismatched eye strokes, rounded
flat-top shield, softly faceted hex, cloud, rounded map-diamond, and horizontal tablet characters. Their paired eyes track the pointer within a deliberately small range and blink;
the existing reduced-motion branch centers them and disables their blink. Each card keeps its summary
to one sentence and follows it with three `.bot-tag` capability badges for quick scanning. Every card
carries inline treg and Grok Bot marks in its "by treg" byline and action.

`_page()` in `routers.web` is the shell for the standalone server-rendered pages (`/docs`) — it owns
`<title>`, the meta description, the canonical, the og/twitter card and the JSON-LD, so a new page
cannot ship missing them. That omission is exactly what left the landing bare.

It owns `/adtrack.js` for the same reason. Until 2026-08-30 that script sat only on the hand-written
marketing HTML (`landing.html`, `usecase-*.html`, `resources.html`, `index.html`), so every page off
this shell — agent pages, programmatic use-case pages, the hubs, `/docs` — was invisible to paid
attribution. The failure is silent and total: no `adtrack.js` means no `treg_ad` cookie, so
`signup._ad_attribution_from()` returns empty, `org.ad_gclid` stays NULL, and `adsconv.queue()`
no-ops by design, so a paid click could sign up and make its first call with Google never hearing
about it. Nothing errors and nothing logs. It surfaced from the ads side: the Agent × job campaign
spent A$125 over three days landing every click on `/agents/*` for zero recorded conversions, while
campaigns pointing at the static use-case pages recorded normally.

The scope is **`_page()` callers**, not "every server-rendered page". `_legal_page()` (`/terms`,
`/privacy`, `/support`, `/contact`), `/dashboard-tour/` and the FastAPI Swagger shell at `/docs/api`
render their own HTML and remain uninstrumented — none is an ad destination. `/tutorial` is likewise
out of scope; it is slated for removal. The `.md` variants are `text/plain` and cannot run scripts.

`/sitetrack.js` is deliberately NOT in the shell. It already shipped more widely than `adtrack.js`
(it is on `tutorial.html` too), but it can load PostHog with pageview/session-recording config while
`web/privacy.html` promises no analytics or session-replay scripts and lists no such processor.
Broadening it across the pSEO surface is a product/legal decision, not a side effect of fixing ad
attribution — `treg_ad` and `/adtrack.js` are already documented in that policy, so shipping those
alone changes nothing about it. `tests/test_agent_pages.py` asserts exactly one `adtrack.js` per
path so a new route off `_page()` cannot drop it.

## The public catalog is the marketplace, not a copy of it

The first cut of this hand-built `/catalog` pages in Python string templates. They shared the API
functions but not the UI, and it showed immediately: the app renders **one row per job with
competing providers merged onto it** (Majestic $0.0008 · Serpstat $0.0025 · SE Ranking $0.018) —
the comparison *is* the product — while the hand-built page listed each endpoint separately. Same
data, different axis, two things to maintain.

So `/catalog` and `/catalog/<slug>` now serve **`index.html`**, and the Vue app renders the same
platform views a member sees. This works because the catalog API is unauthenticated; `publicCatalog`
in `index.html` is the flag, set from `catalogFromPath()` before the `/auth/me` check so the first
paint is already in public mode.

What public mode changes, and why each one:

| Hidden / swapped | Because |
|---|---|
| The app top bar (global tool search) and the whole sidebar → a marketing-style `.pubnav` | a catalog visitor is reading a **website**, not operating an app; the workspace chrome is furniture for a job they have not started. `.layout.solo` gives the main column the full width |
| Org switcher, Getting started, Your vault, Activity, Team | every one needs a session |
| The "not connected" badge on all 80 tiles | connection state is a member fact; publicly it is 80 red herrings |
| Try-it, Connect, BYOK, the provider chips, "Start free" | all open the **sign-in modal in place** |
| Modal subtitle | the default is the sandbox's ("bring it into a real account") — nonsense to someone who arrived from a search result |

**Never point a public CTA at `/app`.** A logged-out visit to `/app` hits
`location.replace('/')` in the boot and lands on the marketing landing with no modal open — so the
CTA loses the page the visitor was reading and offers them nothing. That was the first cut and it
was a dead end at every one of six call sites. They call `openSignin()` instead.

For that to work the sign-in modal had to move: it lived **inside** the logged-out landing branch,
which public mode does not render. It is now a sibling of both branches, near the end of `#app`.
`.lc-scrim` is `position:fixed` and unscoped, so the move needed no CSS change.

`platProviders`, `provName` and `mkKnown` fall back to the open catalog response's own `providers`
map, since their normal source (`/connections`) needs a session. Without the fallback every provider
on a public shelf renders as its bare slug and the whole action chain collapses to nothing.
`mkOauth` has no public fallback — the open response carries no `auth_kind` — so the public branch
offers BYOK, which is true for every provider, rather than guessing Connect.

**Each action is ONE button whose handler forks on `publicCatalog`**, not a duplicated public
template. `tests/test_dashboard_markup.py` asserts the member chain's exact shape
(`v-else-if="mkOauth(e.provider)" class="btn sm primary"`, `openProvider(e.provider)`, …), and a
fork keeps those substrings intact where a parallel branch drifts. That test reads a fixed-size
window of the markup and has already been outgrown once by these forks.

### The no-JS fallback

Vue compiles `#app`'s own innerHTML as its template, so prerendered markup **cannot go inside it**.
`#prerender` is a sibling, removed by the app on boot.

It is deliberately plainer than the Vue view. The ledger's row-merging is a chain of client-side
computeds (`platRowsAll` → `platRowsPreDomain` → `platLedger`), and reproducing that server-side
would recreate exactly the duplicate implementation this whole design removes. The fallback carries
the **text** — names, summaries, providers, prices — which is what a crawler that runs no scripts is
here for. Google executes JS and sees the real view; the ones that don't still get the content.

`/catalog/<slug>` asks the API for `include_hidden=1`, matching what the SPA asks for in
`loadPlatform`. Requesting a different population than the view about to replace it would put two
different endpoint counts on one URL.

## Adding a platform, changing the UI

**New catalog data appears on both sides with no code change.** Everything — the app's tile grid,
the shelf pages, the `#prerender` fallback and the sitemap — reads one `catalog_store.load()`. Drop
the YAML in, restart (the catalog is parsed once per process and changes only on deploy), and the
new shelf is live and indexable.

One gate: give the platform a `platforms:` entry in `capabilities.yaml`. Without one,
`catalog_store` auto-registers it as `category: "Other"`, and the dashboard's `platCategories`
skips `Other` outright — so the sitemap would publish `/catalog/<slug>` while the app's own grid
links to nothing. `test_no_shelf_is_published_that_the_app_grid_hides` fails the build if that
happens.

**UI changes to the shared views reach the public pages automatically** — it is the same
`index.html`. Three things do NOT follow along:

1. **Anything reading member-only state.** `providers`, `connCount`, `billing` and `sessionMode` are
   all empty without a session, so a new element built on them renders blank publicly. Three helpers
   already needed public fallbacks for exactly this (`platProviders`, `provName`, `mkKnown`).
2. **New member-only actions leak.** Public mode hides things by naming them; a new button is
   visible to signed-out visitors until it is gated on `authed` or forked on `publicCatalog`.
3. **The `#prerender` fallback and the SEO head are server-built** (`_spa_catalog_page`), so a
   change to what the page *says* needs the fallback and the meta description updated too.

## Things that will bite you

**`{BASE}`, never a hardcoded `treg.to`.** Every page is also served by self-hosted registries. A
hardcoded canonical tells their crawler the real page lives on someone else's domain. `landing()`,
`_legal_page()` and `tutorial_page()` all read-and-substitute for this reason — they were plain
`FileResponse`s before. `tests/test_seo.py` asserts no response body leaks a literal `{BASE}`, and
none leaks a hardcoded host when `public_url` is overridden.

**HEAD is widened after registration, and must not leak into the schema.** FastAPI's `APIRoute` pins
`methods` to `{"GET"}` and never adds HEAD (unlike Starlette's plain `Route`), so every page 405'd on
the probe crawlers send first. The composition root widens GET-only routes, while its OpenAPI wrapper
temporarily hides those implied HEAD operations; only `/call/{rest}`, which declares HEAD itself, is
documented with one. See [application composition](../architecture/composition.md).

**`/catalog/<slug>` sits in front of the JSON routes.** `/catalog/platforms`, `/catalog/search`,
`/catalog/endpoints/…` and `/catalog/examples/…` keep matching only because they are registered
first. `_CATALOG_RESERVED` refuses those names explicitly as a second guard, and the tests assert
the JSON routes still answer `application/json` — if the page route ever swallows one, the dashboard
and every installed CLI break at once.

**Structured data must match the visible page.** Google treats schema claiming something the page
does not say as a violation, not a shortcut. The landing's `Offer` figures ($1.00 free, 0% markup)
are asserted against the rendered HTML, and every FAQ question in `support.html`'s schema is
asserted to appear in its body. Edit one, edit the other, same commit.

**The catalog page and the app must ask for the same population.** See `include_hidden` above.

**A promo banner on `index.html` is a catalog-page edit.** `/catalog` and `/catalog/<slug>` render
from `index.html`, so anything added to that file lands on all ~80 crawlable shelves unless it is
gated. The one banner this app has carried — the Product Hunt launch strip, since removed — sat
inside the Vue app behind `v-if="…&& !publicCatalog"` for exactly this reason; anything similar needs
the same gate, plus a test that the catalog's `#prerender` block never carries it. Note also that
`landing.html` **is** `{BASE}`-substituted
and `index.html` **is not** (`dashboard()` returns a plain `FileResponse`), so a placeholder that is
safe in one half ships literally in the other — hardcode absolute URLs on the app side.

**Prices need `_usd_short`, not `%g`.** `%g` flips to scientific notation below `1e-4`, and a shelf
advertising "from $1.2e-07 per call" reads as a bug. Anything under a hundredth of a cent renders as
`<$0.0001` — which then has to be HTML-escaped at every use site, because that `<` is real markup.

## The outcome pages are listed from the route map, not by hand

`_SITEMAP_PAGES` spreads `_USE_CASES` rather than repeating its five slugs, which is why `_USE_CASES`
is defined **above** the sitemap block instead of next to its route — a page cannot be routed and
then forgotten by the sitemap. Two details are load-bearing:

- **No trailing slash.** `/use-cases/<slug>/` 307s to the bare form, and the comment above
  `_SITEMAP_PAGES` is explicit that listing a redirect is worse than listing nothing. The pages'
  `<link rel="canonical">` matches the bare form for the same reason.
- **The hub is what makes them crawlable at all.** Before `/resources` existed, nothing on the site
  linked to them; the sitemap alone would have been the only path in.

**The sitemap is walked, not spot-checked.** `test_every_sitemap_url_answers_200` fetches what it
publishes. Rename a route and the sitemap silently starts serving 404s to Google with nothing else
failing.

**`catalog.css` is stamped with its mtime.** It is served with a real `max-age`, so without
`?v=<mtime>` an edited skin keeps rendering from the browser's cache — the same trap `/tutorial.js`
already guards.

## The social card

`assets/brand/og-card.html` is the **source**; `src/treg/web/media/og.png` is the render. Open the
HTML at exactly 1200×630 in a headless browser and screenshot it. The provider favicons are fetched
at render time and baked into the PNG, so the shipped card has no runtime network dependency.

Every brand on the card is a real provider — checked against `catalog_store.load()`, after an early
draft showed Ahrefs, which treg does not carry. LinkedIn's mark is inlined because its Google s2
favicon only resolves at 16px and falls back to a generic globe at 64.

Per-platform cards (`/media/og/<slug>.png`) are a deliberate follow-up. Until then every catalog page
points at the shared one.

## The agent pages — `/agents/<agent>`, and the hub at `/agents`

"I use ChatGPT — what can it do now?" answered on one server-rendered URL per client. The first is
`/agents/chatgpt`; the set is the keys of `agent_pages.AGENTS`, and nothing else (an unknown agent
404s). They came out of the programmatic-SEO plan in `marketing/pseo-build-spec.md`: the measured
demand is for the *agent* ("chatgpt connectors") and the *platform* ("linkedin api pricing"), never
for "how to <job> in chatgpt", so the job list lives on the agent page as rows, not as URLs.

**The hub at `/agents`** (2026-08-31) is one card per client off `AGENTS` — name, the `definition`
sentence with the counts formatted in, a link. It exists because the discovery pass gave every
surface an "Agents" nav link with nowhere to point it: the link went to `/agents/claude-code` (one
client's page standing in for all of them) while the bare `/agents` URL 404ed. The nav now points
at the hub, the agent pages' breadcrumb carries it as the parent (treg.to / Agents / <name>), and
the sitemap lists it at 0.8 beside the other two hubs. Hosted-only and adtrack'd like everything
else off `_page()`; no `.md` mirror, matching `/use-cases` and `/workflows`.

**One skin for both page types.** The agent and use-case pages render with `usecase.css`, the
landing-page skin the five outcome pages already use, passed to `_page(css=...)`: centered hero with
a kicker pill and CTAs, `seclab` labels above each `h2`, real `<table>` comparisons in a `tablewrap`,
the dark `promptbox` with a copy button, `steplabel` numbered steps, `cards` grids, `pricewall` for
the money, and the dark `final` band. The stack of `.ep` rows the first cut used is gone: a job menu
and a provider comparison are tables, and the endpoint inventory belongs on the catalog shelf, which
the pages link to rather than reprint.

**The economics block** (`pricewall`) anchors a per-call price against a subscription: "instead of
$34/mo (Hunter, at list) → you pay $0.89 for 100". Plan prices come only from
`agent_pages.PLAN_PRICES`, which mirrors `marketing/landing/_facts.md` F-20..F-23 and records where
each figure was sourced; a page names a provider's plan price only if it is listed there. With none
listed the anchor falls back to the catalog's own spread (dearest vs cheapest for the same 100
calls), because an invented subscription number is worse than no anchor.

**Two halves, one rule each.** The hand-written half lives in `src/treg/agent_pages.py` — a module
with no heavy imports so it costs the light CLI nothing and can be reviewed without reading routing
code. It holds `ROLES` (the rotating "for *SEO experts / social media managers / SDRs*…" line; the
H1 itself is the keyword and the promise — "The ChatGPT Connector: call {n} APIs without keys" —
because a persona in the H1 read as the page's audience, and the first role is server-rendered on
the roleline so a crawler still gets a full phrase), the install
steps and screenshot, one example prompt per category, the FAQ, and `USE_CASES`: the buyer's menu —
plain-words jobs ("Find professional emails", "Find creators by keyword") under fourteen buyer
categories, each mapped to the capability ids that do it. That taxonomy is the map of the whole
site, and it is re-cut whenever the catalog grows, so **it is metadata and never a URL** (see the
use-case pages below). A row links to its page once one exists. The route projects
the rest from `catalog_store.load()` per request — the union of providers, the lowest USD price
via `cost_view`, verified counts, one chip per platform — and the counts in the title are computed.
`tests/test_agent_pages.py` asserts every capability id in `USE_CASES` exists in the catalog, so a
job the catalog cannot do cannot be advertised and a renamed capability fails the suite instead of
silently dropping a row. Never a row per endpoint — that is the banned page-per-endpoint in list form.

**One axis per category, and sub-headings where the buyer needs depth.** The first cut mixed two
axes: "Connect your own accounts" named an *access mode*, so Google Analytics sat beside Business
Profile reviews for the only reason that both authenticate with the team's own key, while the buyer
looking for either was reading a different page. A category now names one thing only: what the job
is about. `test_one_axis_only_no_access_mode_categories` holds that, and two more tests hold that no
job appears in two categories and no two jobs share a capability set (which is how a genuine
duplicate was found).

Data enrichment is the killer use case, so it carries twenty-five of the ninety jobs, and its four
stages — find companies, find people, contact details, enrich a record — render as sub-headings
inside the one section, driven by `CATEGORY_GROUPS`. They are **not** categories: they are stages of
one motion, and promoting them would have committed four URL segments to a distinction that only
exists in a practitioner's head. The section builder orders rows by group and prints a divider row
before each; the `.md` mirror sorts by the same order.

**Hosted only.** The copy describes treg.to's own listings — the ChatGPT Connectors entry, the $1.00
grant — none of which is true of a self-hosted registry. `_hosted()` checks `public_url` against
`PUBLIC_HOST_ALIASES`; elsewhere the route 404s and the sitemap omits the rows, rather than lie.

**One spelling per page.** Lookups are case-insensitive, but both routes resolve the slug to the
table's own key and 301 any other casing to it (`/agents/ChatGPT` → `/agents/chatgpt`). Two
reasons: a differently-cased URL would otherwise serve a 200 whose canonical points at itself, a
duplicate page; and the slug reaches the canonical, the `rel=alternate` href and the JSON-LD
breadcrumb unescaped, so it must come from the table, never from the request (CodeQL
`py/reflective-xss` flagged exactly this).

**`Disallow: /app` is a prefix rule** and would have blocked `/agents/…` too. `robots.txt` carries an
explicit `Allow: /apps/`; the longer match wins. The test asserts the Allow line exists.

**The shell's CTA now carries `?ref=<page>`.** `_page()`'s "Start free" used to link bare `/app`,
which bounces a logged-out visitor to the landing with nothing open — the dead end this fragment
already documents for the catalog pages. The app boot treats `ref` as a use-case CTA (no bounce,
sign-in opens in place), so every server-rendered page now gets that behaviour and the page that
produced the signup is recorded. Schema on the page: `SoftwareApplication`, `BreadcrumbList`, and
a `FAQPage` whose questions are asserted to appear verbatim in the body.

## The use-case pages — `/use-cases/<job>`, and the hub at `/use-cases`

The spokes. **The reader does one thing, the prompt; everything else is what the agent sees before
it calls.** Above the fold: the setup line, one prompt with a copy button, four "why this prompt
works" cards, an optional screenshot. Then "Why go through treg.to" (`WHY_TREG`, six cards).

The page then takes one of **three forms, chosen from the catalog rather than by hand** — this is
what makes it a template rather than one page's prose:

| Form | Condition | Renders |
|---|---|---|
| `short` | one provider | "How it works": the one call. Own-account copy when the cost is `free`; otherwise the rate and the $0.000 markup. No comparison |
| `platforms` | the job spans several platforms | providers grouped per platform, cheapest claimed per platform |
| `compare` | several providers, one platform | the full comparison |

Of the 90 jobs on the menu, roughly a third are single-provider and a third span several platforms,
so two thirds of the eventual pages are not the plain comparison the first page was built for.

**The URL is flat, and the category is metadata.** `/use-cases/<job>`, with the category carried only
in the breadcrumb, the hub heading and the agent-page section. This is the whole point: the taxonomy
gets re-cut as providers arrive, and **a re-cut must never move a page Google has indexed**. The
nested form that shipped first (`/use-cases/<category>/<job>`) is live and indexed, so it 301s to the
flat form rather than 404s, and `test_use_case_urls_are_flat_so_a_recut_never_moves_them` holds the
shape. Composio's pSEO does the same thing for the same reason. The category breadcrumb points at
`/use-cases#<category>`, so the hub is the parent of its own cluster rather than the ChatGPT page.

**Cheapest is claimed per billing unit, never overall.** Most jobs mix per-call, per-result
and per-success endpoints, and ranking those by USD per chargeable event names the wrong winner: one
call returning a thousand rows is not dearer than one row. The page prints "cheapest per found",
"cheapest per call" and so on, and says the units are not interchangeable when more than one appears.

**Providers are keyed by (provider, platform), not provider.** ScrapeCreators serves Instagram and
YouTube for the same job; collapsing on provider alone silently dropped a whole platform from a
multi-platform page.

**The reliability section renders only when there is traffic.** `endpoint_stats.observed` is empty
for most endpoints, and an empty promise is worse than no section. When it does render it names
per-vendor success rate, median latency and sample size (publication approved by Jason 2026-08-21)
with the "live traffic, not a controlled benchmark" caveat.

**Nothing job-specific or agent-specific is in the route.** The example client is
`agent_pages.DEFAULT_AGENT`; the job's own words, result noun, "what is X" heading, notes, FAQ and
`voices` all come from `USE_CASE_PAGES`, keyed by the job slug alone. A spec's `label` must
match a row of `USE_CASES` exactly (tested), which is how the agent page knows to link the row to
its page. `tests/test_agent_pages.py` asserts the route source contains no job-specific string.

**`voices`** is the section that cannot be regenerated: real questions from Reddit and X, quoted
verbatim with a link, each followed by what the page can honestly do about it (including "no
comparison table can answer this"). The `.agents/skills/treg-page` skill runs that research with
`agent-reach` before any page is written, and documents how to spot the vendor astroturf that
dominates these searches. It is not decoration: on the YouTube pass roughly half the corpus was
vendor-written, thirteen distinguishable clusters, one posting the same body to three subreddits
seven seconds apart. `voices` renders in HTML and in the `.md` mirror, and is optional in the spec
(two of the first seven pages ship without it), so `test_no_use_case_page_ships_with_an_empty_section`
requires `voices` and `voices_intro` together rather than requiring either.

**The section order is comparison, then voices, then notes, then FAQ.** Copy inside `voices`,
`notes` and `faq` that says "the comparison below" is pointing backwards; the first written pages
say it anyway. Write position-neutral ("the comparison above", "the prices here") or the sentence
is wrong for every reader who scrolls.

**Written so far: 42 of the 90 jobs, and the measured-demand worklist is complete** (2026-08-29). From 2026-08-24 the remaining set was no longer "all of them": every unwritten job was measured against Google Ads keyword volume that day; the 34 clearing 50 US searches a month became a worklist ordered by volume, written five a day, and the rest are parked as rows on the menu with no page. A page nobody searches for is the scaled-content shape the risk audit says to avoid, so the 48 parked jobs are a decision rather than a backlog.

The YouTube & video cluster (transcript, video stats, channel stats, search, comments) landed
2026-08-21 and is the first `compare`-form cluster where one row is
free: the official Data API on the reader's own connected Google account, at $0.00 with a 10,000
unit daily quota. The free row is deliberately excluded from the "cheapest per unit" claim, because
`_uc_providers` only ranks rows with a truthy USD price, and a free-but-rationed row is not a
cheaper version of a metered one. Those pages carry the quota arithmetic instead, which is what the
research said people actually get stuck on.

**A single provider on a trial pool is a third `short` state** (2026-08-26). The `short` form
branched on the row's `free` flag: own-account copy, else `metered_single`. Finnhub's company news
is one provider, not free, with no USD and a 50-call daily allowance, so it would have rendered
"metered from your team's balance". `trial_single` now states the allowance and the own-key
fallback in the description, the `.md` and the "How it works" section; the hero and `{cheapest}`
already handled it through `free_words`. Found the usual way, by rendering the page after 88 green
tests.

**A platform label is catalog vocabulary, and a page cannot override it.** The ads page groups
SerpApi's Ads Transparency Center engine under the `google` platform, whose catalog label is
"Google Keyword Data"; the heading is wrong for that job and right for the SERP jobs. Fixing it
means renaming the platform in the catalog, not the page, so it is flagged rather than patched.

**Demand keeps outrunning the worklist term** (2026-08-26). `slack mcp` 4,400 and `slack mcp
server` 1,900 against `slack bot api` 260, the fifth time `mcp` has beaten `api`; `google index
checker` 590 against `google indexing api` 260, and the Indexing API is the wrong API for the
job, so the page carries the checker term and says so; `financial news api` 170 sits beside
`stock news api` 210.

**`{cheapest}` expands to a bare price, so the lede must carry the preposition** (2026-08-27).
On a metered page it becomes `$0.002`, on a trial-pool page the `free_words` phrase, so "one key,
{cheapest}, at the provider's rate" reads "one key, $0.002, at". Write "from {cheapest}" and let
the phrase follow. Three of the 2026-08-27 pages shipped the bare form past 116 green tests and
were caught by reading the `.md`; the same pass caught a note calling Majestic unpriced while the
table printed $0.0008 per result for it, because `cost_view` converts index item units to USD.

**Demand keeps outrunning the worklist term, sixth time** (2026-08-27). `coingecko api` 1,300
against `crypto price api` 170, so the crypto page's H1 leads with CoinGecko; `reddit api` 5,400
and `reddit mcp` 590 against `reddit search api` 140; `free backlink checker` 2,900 and `ahrefs
api` 320 against `backlink api` 140, with Ahrefs not in the catalog, which the page says in its
first paragraph; `competitor keyword research` 1,300 and `semrush api` 720 against `competitor
ppc keywords` 140. The Glassdoor term held: `glassdoor api` 210, `glassdoor scraper` 110.

**A `related` card resolves by label, not inside the current category.** Four categories carry
fewer than five jobs (advertising and market research three, e-commerce and local businesses four),
so their pages have to point at least one of their four cards outside the category. The card used to
look the label up in the current page's category and, on a miss, fall back to that category's anchor
under a caption naming that category: a wrong link nothing failed on, because the test only asks
whether the label exists somewhere on the menu. The 66 labels are unique, so `_related_link` finds
the owner from the label alone.

**A row with no dollar price is not a free row.** The price cell had one `else` branch and it read
"free, your own account" in green. Semrush prices both SERP jobs in API units bought up front, so
its `cost_view` carries no USD and the page labelled the dearest option on it as free on the
reader's own account. Free is now read off the cost's own `type`; anything else without a figure
prints "no dollar rate published", which is what the pages say about Semrush in prose too. It was
found by reading the rendered page, which is the argument for that step in the skill.

**A trial-pool row is a fourth state, and a metered single provider is not an own-account one**
(2026-08-25). Finnhub, Tiingo and Twelve Data are served at $0 on treg.to's own free-tier keys with
a per-team daily allowance (`catalog.trial_pools`); `cost_view` gives them `usd == 0`, so they fell
through to "free, your own account", and with no priced row on the page the hero and the `{cheapest}`
placeholder said the same. `_uc_providers` now carries `trial` (calls per team per day) and the
cell, the `.md` table, the hero and `{cheapest}` state the allowance ("free, 50 calls a day on
treg.to's key, then your own key"). Separately, the `short` form assumed its one provider was an
OAuth connection; the AI-mentions job has one provider, DataForSEO, and it is metered, so the form
branches on the row's `free` flag: `metered_single` states the rate and the markup, and the meta
description does not claim "never metered". Both were found the same way as the Semrush one: by
reading the stock and AI-visibility pages after the tests went green.

**`{cheapest}` is the cheapest of ONE unit, not of the page** (2026-08-28). `_uc_providers` groups
rows by billing unit and `{cheapest}` expands to `cheapest_by_unit[units[0]]`, whichever unit is
found first. On a mixed-unit page that is not the lowest number the reader can see: the keyword
volume job printed "from $0.09" (DataForSEO's flat per-request row) above a table whose first line
is Serpstat at $0.0005 per keyword, and the LinkedIn profile job printed "from $0.0015" above a
$0.001 row. The comparison block itself is correct, because it prints a cheapest per unit with the
units-are-not-interchangeable caveat; only the interpolated lede lies. Both ledes now state the
rate in prose instead. **Do not write "from {cheapest}" on a page whose providers meter in more
than one unit**, and read the rendered lede against the rendered table before shipping. Found the
same way as the two above.

**A provider whose only row has no cost took the page down** (2026-08-29). `_uc_providers` built the
trial allowance with `max()` over the rows that have a `cost_view`; TikHub's LinkedIn comments v2 row
carries no cost at all, so on the comments job the generator was empty and the page 500ed past every
green test (the tests render other jobs). `default=0` now; the cell prints "no dollar rate published".

**Routed rows are off every public surface, not just comparison tables** (2026-08-31). PR #242's
first-party routed endpoints (`treg.<capability>`, `kind: routed`) were leaking into `_uc_providers`
as a fake "treg" provider row — and, found while fixing that, into every other public projection of
the catalog: the agent-page menu counted "treg" as an extra provider on every routed job, the
use-case hub's card metas did the same, `_provider_rows` published a self-referential `/tools/treg`
page into the sitemap, and `_catalog_census` counted the 76 meta-rows on top of the children they
delegate to. `_pub()` in `routers.web` is now the one filter every public page reads (hidden kinds
plus routed), `/tools/treg` 404s (the all-rows-hidden fallback in `tools_provider` no longer
resurrects routed rows), and the advertised tool count dropped accordingly (2,745 → 2,669). The
public name is treg.to; a bare "treg" vendor row must never appear on any page —
`test_routed_rows_never_surface_a_provider_named_treg` pins it.

**Pages with a free own-account row use a filtered WHY_TREG** (2026-08-31). `WHY_TREG` contains
cards like "One key, not 9 accounts" and "Already pay Hunter? Register it..." that are false
wherever the reader's own connected account does the job. That is the short own-account pages (GA4,
Search Console) — and also the YouTube comparison pages whose official Data API row is a $0.00
own-account row, so the condition is `any(p["free"] for p in provs)`, not "short form only" (the
first cut missed the YouTube pages). Those pages render `WHY_TREG_OWN_KEY` instead, keeping only the
cards that stay true ("No code to write", "Nothing to integrate").

**Possessive slugs 301 to clean slugs** (2026-08-31). Five use-case pages shipped with possessive slugs
containing `-s-` (e.g. `get-a-video-s-transcript`, `a-company-s-email-format`). Before GSC indexed them,
the keys in `USE_CASE_PAGES` were renamed to clean slugs (`youtube-transcript-api`, `company-email-format`,
etc.) and `USE_CASE_REDIRECTS` maps old to new. The flat route checks `USE_CASE_REDIRECTS` first and 301s, and the nested legacy route resolves
through the same map before its own lookup (it used to reject a renamed slug first, turning the
promised 301 into a 404 on the nested shape);
the old slugs are not in the sitemap (only `USE_CASE_PAGES` keys appear) and the canonical is on the
new slug. Redirects: `get-a-video-s-transcript` -> `youtube-transcript-api`,
`a-channel-s-profile-and-lifetime-stats` -> `youtube-channel-stats`, `a-video-s-comments` ->
`youtube-video-comments`, `a-business-s-reviews` -> `business-reviews`, `a-company-s-email-format` ->
`company-email-format`.

**The related-card test hard-coded the agent-page anchor** (2026-08-29). It asserted the email-finder
page links to `/agents/chatgpt#`, which was only ever the fallback for a related label with no page;
the day "A company's email format" got one, the assertion had nothing to match. It accepts a
`/use-cases/` sibling now.

**Demand outran the worklist term again, seventh time** (2026-08-29): `company email format` 140 and
`email format checker` 1,300 (which is validity checking, the verify page) against `email pattern
finder` 90; `social listening tool` 4,400 and `instagram comment export` 260 against `social listening
api` 70; `companies by industry` 2,400, `apollo api` 1,000 and `crunchbase api` 390 against `company
list by industry` 70; `indeed api` 720 and `jobs api` 590 against `job board api` 50, with no Indeed
row in the catalog, which the page says in its first paragraph; `yahoo finance api` 2,900, `polygon
api` and `alpha vantage api` 1,600 each against `historical stock data api` 50, with neither Yahoo
nor Alpha Vantage in the catalog, said in the lede.

The five flat ad pages predate all of this, keep their URLs and their `build_html.py` ownership, and
are served first by the same flat handler; `test_legacy_flat_use_case_pages_still_answer` proves a
rendered job page cannot shadow one. `/use-cases` is the crawlable hub they hang from; before it existed the only link into a spoke
was one row on one agent page. All hosted-only and sitemapped; the RENDERED job pages are `.md`-mirrored like the agent pages, but the five flat ad pages are not — their route serves no `.md`, which is why the build stopped advertising alternates for them.

### The five campaign hub pages (2026-08-31 rewrite)

The five Google Ads landing pages (`/use-cases/seo-data-for-ai-agents`, `/use-cases/lead-enrichment-for-ai-agents`,
`/use-cases/social-trend-research-for-ai-agents`, `/use-cases/competitor-ad-research-for-ai-agents`,
`/use-cases/company-research-for-ai-agents`) are built from markdown sources in `marketing/landing/` via
`build.py` and `build_html.py`, served as static HTML with `{BASE}` templating applied at request time.

**Title matches H1, and both carry the measured buyer term.** The shipped H1s: "SEO Data: Google
Results, Keywords and Backlinks", "Waterfall Enrichment: Find and Verify Work Emails", "Social Data
MCP: Reddit, Instagram, TikTok, YouTube", "Ad Library API: Meta, Google, TikTok, LinkedIn Ads",
"Company Research: Funding, Headcount and Leadership". Per-vendor MCP keywords still belong on
`/tools/<provider>`; a category-level term ("social data mcp", "ad library api") may lead a hub when
it is the measured phrase.

**CTAs are "Start Free" and "Paste llms.txt".** The primary CTA links `/app?ref=<page_id>`, the
secondary links `/llms.txt` directly. The copy-paste prompt in each page's workflow section is the
action the page exists to produce.

**JSON-LD: BreadcrumbList and FAQPage.** Every page carries both schemas. The breadcrumb names
"treg.to" (never bare "treg") as the root. FAQ pairs are extracted from the "Before you sign up"
section during build.

**What the 2026-08-31 review fixed in the build** (each shipped past green tests and was caught by
reading the rendered pages): the hero's fenced prompt rendered as `<code>text\n…` — `render_hero`
now strips the fence with `code_text` and gives it the copy affordance, and the bold price line
under it (previously dropped) renders as the subline. The FAQ parser only knew full-line bold
questions, so the rewritten `**Q?**: answer` one-liners folded several visible FAQs into one
JSON-LD acceptedAnswer; it now splits the one-line form first. The kicker was hand-typed
("2,600+ tools · 40+ providers") — it is now read from the catalog at build time and floored to a
bound (F-01's convention: a static page states a rounded-down claim that stays true as the catalog
grows). `sitetrack.js` had been dropped in the rewrite (the attribution test caught it) and is
restored before `adtrack.js` on all six files. The advertised `.md` alternates are gone — the
legacy-hub route serves no `.md`, so every one 404ed. The brand anchor reads `treg.to`. And the
lead-enrichment "Proof from one real run" block no longer shows the 1-email $0.0245 demo: it
carries the receipt of the 50-company workflow run (2026-08-26, $3.62, $0.13 per deliverable
lead) and points at `/workflows/find-and-verify-a-lead-list`, so the hub sells the workflow
instead of competing with it.

**Links to job pages, not competing with them.** The "Next steps" section links real job pages and
workflows: lead-enrichment links `/workflows/find-and-verify-a-lead-list` ($3.62 from a real run) and
`/use-cases/find-professional-emails`; SEO links keyword and SERP job pages; social links creator and
trending job pages. The hubs sell the job pages, not cannibalize them.

**No hardcoded numbers.** Every figure on the page comes from `catalog_store` or a real run receipt.
The workflow receipts (e.g. $3.62 for lead generation) are hand-recorded from actual runs, not
rate-card estimates.

**`{BASE}` templating.** The canonical URL and `og:url` use `{BASE}` in the HTML, substituted at
request time by `routers/web.py` reading and replacing before returning `HTMLResponse`. This is the
same pattern as `landing.html` and the legal pages.

## The workflow pages — `/workflows/<slug>`, and the hub at `/workflows`

A use-case page answers one job; a workflow page is **the sequence a real person runs**, as ONE
paste-in prompt. It renders, in order: the setup line and the prompt (copy buttons), four "why this
prompt works" cards, a **step table** with a per-step price pulled live from the catalog (the
endpoint the worked run used, its `cost_view` price per billing unit, how many providers do the
step, observed success rate and p50 when there are samples, and a link to the step's use-case page
resolved through `USE_CASES` by capability — or the category anchor on the default agent page when
no page is written), a worst-case total (`price × rows_in` if every call hits), then **the receipt**
of a real run (`id="run"`), `WHY_TREG`, the failure modes, the FAQ, and four related cards.
JSON-LD: BreadcrumbList, a `HowTo` whose steps are the table rows, and a FAQPage. `.md` mirrors it
all, ending with `HTML version: …`. Hosted-only, sitemapped (hub 0.8, page 0.7) and case-folded to
the canonical slug with a 301, exactly like the use-case pages.

The data lives in `agent_pages.WORKFLOWS`, one dict per slug: `steps` are
`(name, capability, what the agent asks, endpoint the run used, why)` tuples; `run` holds the
`date`, `rows_in`, the `receipt` label/value pairs, `cost_usd`, the narrative paragraphs and the
CSV path. **The receipt and the CSV are hand-recorded from a real run and dated** — the page
prints them verbatim and computes nothing from them; only the per-step prices are live. The CSV is
shipped in the package at `src/treg/workflow_runs/<slug>.csv` and served at
`/workflows/<slug>.csv` (404 when the file is missing). The published CSV carries **row-level
outcomes only** (`person_found`, the finder, the verdict, catch-all, news) — never a name, title
or address: these are real people and a title at a named company identifies one. A spec's `once`
tuple names the endpoints called once per run rather than once per row (Apollo's list page); the
"at the rates above" total and the hub's per-row price read it, so the list step is 1 × its price.
The rule: **a workflow page is not written
without a real run behind it.** A page whose receipt is a rate card is the thing this page type
exists to not be.

Tests: `test_workflow_page_is_served_with_the_crawler_essentials` (crawler plumbing, HowTo with the
step count, `.md`, `.csv`, hub, 301, sitemap), `test_every_workflow_step_capability_and_endpoint_exist`,
`test_no_workflow_ships_with_an_empty_section`, `test_workflow_copy_has_no_em_dashes`.

## Counts

`2,630 endpoints / 47 providers / 80 platforms`, from `catalog_store.load()`. The landing, `llms.txt`
and the schema all state them and had drifted apart (2,617/42 and ~2,600/~48). Note the catalog index
shows the **whole** catalog, not the sum of its tiles: a tile counts only its browse surface, so the
account/utility endpoints — real inventory, listed on each shelf page — are excluded from tile counts
by `catalog_store.HIDDEN_KINDS`.

## Discovery — the hubs are linked, not just listed

Search Console on 2026-08-27 showed the 38 job pages, `/workflows` and `/agents` at zero
impressions, and URL inspection answered "URL is unknown to Google" for
`/use-cases/find-professional-emails` and `/workflows/find-and-verify-a-lead-list`. They were in
the sitemap and linked from nothing: the landing linked `/catalog` and `/resources` only, `/catalog`
linked `/tools/*` only, `/resources` linked the five outcome pages only — and those five were the
only non-homepage URLs with impressions. A sitemap is not a crawl path.

What links what now, and where it is generated:

| From | To | Where |
|---|---|---|
| footer of every server-rendered page (Explore / Build / Company columns; the nav is unchanged by request) | `/use-cases`, `/workflows`, `/agents/claude-code` — **hosted only**: those pages 404 on a self-hosted registry, so the links are gated by `_hosted()` (the landing wraps them in `<!--hosted-->` markers the route strips off-host) | `_page()` in `routers/web.py` |
| the landing footer (`landing.html`; the public catalog SPA has no footer and links the hubs from its prerender) | same three | hand-kept markup, so `test_every_surface_links_the_three_hubs` walks `/` and `/catalog` |
| `/catalog` prerender | both hubs, in a sentence | `catalog_index` |
| `/tools/<provider>` "Used in" | every job page whose capabilities the provider answers | `_jobs_by_provider()`, cached per process from `USE_CASE_PAGES` × the catalog |
| `/use-cases/<job>` "Run the full sequence" | every workflow with a step on one of the job's capabilities | `_workflows_by_capability()`, cached from `WORKFLOWS[*].steps` |
| `llms.txt` | the three indexes, with the `.md` twins | hand-kept |

Both reverse indexes are derived from the same tables the pages render from, so a new job or
workflow is cross-linked the moment it is routed, and nothing is listed by hand.

### Titles: the pricing intent

The non-brand queries that reach the site are "{provider} api pricing" phrasings ("linkedin api
pricing", "1688 api pricing"), not "api for agents". So:

- `/tools/<provider>` titles lead with it: `{Provider} API pricing: from $0.00245/result, no signup | treg.to`
  (the price label carries its own billing unit, so the copy never says "per call" beside it;
  falls back to `{Provider} API pricing: from $X | treg.to` past 65 characters; own-account
  providers get `{Provider}: connect your own account | treg.to`). **Title and H1 now match**:
  metered H1 is `{Provider}: {n} tools from {price}`, own-account H1 is `{Provider}: connect your own account`.
  The kicker carries the measured line (calls observed, ok rate weighted by DECIDED calls, median p50)
  read through `_observed_or_empty`. Descriptions go through `_serp_desc` (sentence-fit under
  Google's cut), and the HowTo's steps mirror the visible setup section in order — the one-line
  install first, direct MCP second — because schema describing a different flow than the page
  shows is the mismatch Google treats as a violation. The setup line on these pages is the
  canonical `set up treg — {base}/llms.txt` (the em-dash is the documented exception, and a
  colon variant that shipped briefly forked the product's one paste-line).
- compare-form job titles get `, from $X` appended when the hand-written title carries no price and
  the result stays within `_TITLE_MAX` (65); " compared" is dropped to make room.

### IndexNow

`/{INDEXNOW_KEY}.txt` serves the IndexNow key (not a secret: the protocol only checks the key is
served from the host named in the submission), on every host, since IndexNow is generic.
`scripts/indexnow_submit.py` reads the live sitemap and pushes every URL to `api.indexnow.org`
(Bing, Yandex, Seznam, Naver share the feed). Google retired its sitemap ping; its resubmission goes through Search Console
(`google-search-console.x.webmasters-sitemaps-submit` in the catalog, owner OAuth). The route is
registered in `bootstrap.py`'s ownership table like every other public route.

Tests: `test_every_surface_links_the_three_hubs`, `test_provider_page_names_the_jobs_it_serves`,
`test_job_page_names_the_workflows_that_chain_it`, `test_compare_titles_carry_the_cheapest_price`,
`test_provider_title_leads_with_pricing`, `test_indexnow_key_is_served_from_the_root`.

### Agent pages name the workflows

`/agents/<agent>` (and its `.md` twin) carries a **Workflows** section listing every entry in
`agent_pages.WORKFLOWS` with its step count, between "The menu" and the category sections. Before
this the workflow pages were reachable from `/workflows` alone. `AGENTS["grok-bot"]` also carries
three Grok-Bot-specific FAQ entries (lead generation, research, what it cannot do yet) because
"grok bot lead generation" is the one emerging term in the outbound research that passed all seven
gates; the use-case map lives in `marketing/rebuild/06-grok-bot-use-cases.md`. Test:
`test_agent_pages_name_the_workflows`.
