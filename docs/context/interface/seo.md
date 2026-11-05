---
title: Search surfaces - robots, sitemap, the crawlable catalog, and the social card
status: shipped
sources:
  - src/treg/api.py
  - src/treg/routers/web.py
  - src/treg/agent_pages.py
  - src/treg/web/robots.txt
  - src/treg/web/catalog.css
  - src/treg/web/usecase.css
  - frontend/index.html
  - frontend/src/App.vue
  - frontend/src/components/PublicNavigation.vue
  - frontend/src/state/boot.js
  - src/treg/web/landing.html
  - src/treg/web/media/landing/product-theme.css
  - src/treg/web/media/landing/refinement.css
  - src/treg/web/media/landing/gateway.css
  - src/treg/web/media/landing/hero-opening.js
  - src/treg/web/media/landing/refinement.js
  - src/treg/web/media/landing/catalog-drum.js
  - src/treg/web/media/landing/gateway.js
  - src/treg/web/media/landing/gateway-loader.js
  - src/treg/web/media/landing/gateway-3d.js
  - src/treg/web/media/landing/gateway-model.js
  - src/treg/web/media/landing/gateway-intro.js
  - src/treg/web/media/landing/hero-particles.js
  - src/treg/web/media/landing/command-beam.js
  - src/treg/web/media/landing/SOURCES.md
  - frontend/e2e/landing.spec.ts
  - src/treg/web/terms.html
  - src/treg/web/usecase-seo.html
  - src/treg/web/usecase-company.html
  - src/treg/web/usecase-enrichment.html
  - src/treg/web/usecase-social.html
  - src/treg/web/usecase-ads.html
  - src/treg/web/people-search.html
  - src/treg/web/leads-signals.html
  - src/treg/web/recruit.html
  - src/treg/web/web-access.html
  - src/treg/web/gtm-engineering.html
  - src/treg/web/grokbot.html
  - src/treg/web/fable-gtm.html
  - src/treg/web/ugc.html
  - src/treg/web/aeo.html
  - src/treg/web/rea-growth.html
  - src/treg/web/rea-growth.md
  - src/treg/web/start.html
  - src/treg/web/jev.html
  - src/treg/web/showcase.html
  - src/treg/web/rankings.html
  - src/treg/routers/rankings.py
  - src/treg/web/media/showcase/wall.json
  - src/treg/application/jev_xboost.py
  - src/treg/application/house_calls.py
  - src/treg/web/media/jev/xboost-seed.json
  - src/treg/web/media/jev/triage.json
  - src/treg/web/media/jev/signals.json
  - src/treg/web/astra.html
  - src/treg/web/media/astra/page.css
  - src/treg/web/media/astra/page.js
  - src/treg/web/media/signin-sso.js
  - frontend/src/components/SignInDialog.vue
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

Everything a crawler, a link unfurler or an AI answer engine sees. Signup-credit copy on every page
describes one grant per new verified account, not per team
([money](../architecture/money.md#signup-credit-eligibility)).

## Routes

| Path | Rule worth knowing |
|---|---|
| `/robots.txt` | Bundled, `{BASE}`-templated. Disallows `/app`, `/login`, `/auth/`, `/oauth/`, `/connect-demo`, `/call/`, `/mcp`, `/admin`, `/docs/api`; names the sitemap. `Disallow: /app` is a prefix rule: it also blocks any future public path starting with `/app` (e.g. `/apps`) unless a longer `Allow:` line is added. |
| `/sitemap.xml` | Generated. Static pages take `lastmod` from file mtime, shelves from the newest mtime under `src/treg/catalog/`. |
| `/` | Server-rendered `web/landing.html`; visuals under `web/media/landing/` (`SOURCES.md` pins the design reference and library versions; Three.js and Lenis load from version-pinned jsDelivr URLs). Reduced motion is static; WebGL or module-download failure (`gateway-loader.js`, outside the module graph) reveals the static mark; BFCache `pageshow` resumes animation. Signed-in visitors see **Open dashboard**. Session-dependent HTML is `private, no-store` with `Vary: Cookie`. `frontend/e2e/landing.spec.ts`. |
| `/catalog`, `/catalog/<slug>`, `/catalog/<slug>/<key>` | The dashboard SPA in public mode (below). `<key>` is a capability two or more providers serve on that shelf (`Catalog.compared`, `_comparison_paths`); a single-provider capability is a 404. All are sitemapped and each is fetched for a 200 in `tests/test_catalog_api.py`. |
| `/tools/<service>` | One server-rendered page per provider (`_page`, no SPA), counts and prices live from `catalog_store`. No provider index page. Safe from shadowing the API (`/tools`, `/tools/by-name/...`). A signed-out `GET /app/marketplace/<service>` 302s here. |
| `/agents`, `/agents/<agent>` | Agent pages (below). |
| `/use-cases`, `/use-cases/<job>`, `/resources` | Use-case pages and hubs (below). |
| `/workflows`, `/workflows/<slug>`, `/workflows/<slug>.csv` | Workflow pages (below). |
| `/blog`, `/blog/<post>` | Hand-kept index (`_BLOG_LAUNCHES`, `_BLOG_POSTS`). Posts carry `BlogPosting` (`_blog_posting_ld`) beside breadcrumbs. `/agents/grok-bot` 301s to `/grokbot`. |
| Launch landings (`/people-search`, `/leads-signals`, `/web-access`, `/recruit`, `/grokbot`, `/fable`, `/ugc`, `/aeo`, `/rea-growth`, `/gpt6`, `/jev`) | Bundled files through `_static_page` (placeholders filled, no-cache, ETag revalidation to 304), canonical, OG, sitemapped. `/astra` 301s to `/gpt6` keeping the query (campaign attribution). `ugc.html` uses relative asset paths, which only resolve while the route stays slashless. `/web-access` reads current prices from `/web-arena/api/leaderboard` with a bundled snapshot fallback and deliberately shows no hit-rate board (each vendor sees different traffic; that stays on `/web-arena`). Copy on `/leads-signals` and the Hermes guide must not imply treg schedules or diffs: the agent does. `/rea-growth.md` is `noindex` via `_serve_md`. |
| `/jev` | `/jev/xboost.json` serves the document `treg-worker jev xboost` stores (Ephemeral `jev`/`xboost`), else `media/jev/xboost-seed.json` flagged `snapshot: true`. `POST /jev/xboost/judge` is rate-limited per IP and fleet-wide through ratestore, answers 503 until `jev_treg_token` and `ai_gateway_api_key` are set, and its entries survive the daily run. `application/jev_xboost.py` calls `/call/` with the demo team's token, so the receipt is a real bill. Bundled recipe runs have every email address replaced. `tests/test_jev.py`. |
| `/start` | Paid-traffic landing. Hosted-only, `noindex` (meta and `X-Robots-Tag`), no canonical, not sitemapped. `?job=` picks copy from a fixed table; unknown values (including `constructor`, `__proto__`) get the general page. `sitetrack.js` loads before the page script so `start_viewed` is not dropped; copy events fire only after a fulfilled clipboard write. `tests/test_start_page.py`. |
| `/gtm-engineering` (+ `.md`) | Hand-written (not `_page()`) because only hand-written pages load `sitetrack.js`. Its original studies publish results, never rows: the test fails if a catalog provider is named in a study block, a headline figure leaves its section, or a study FAQ answer drifts from its JSON-LD; raw data stays out of the repo. The `.md` twin is hand-kept and `tests/test_gtm_engineering_page.py` fails on a missing chapter heading. Hosted-only. |
| `/rankings` | Served on every registry (it counts that registry's traffic), sitemapped. Boards come from the row `treg-worker rankings refresh` saves ([catalog](../architecture/catalog.md)). The first board is inlined as JSON plus an `ItemList`; the rest load from `/rankings/data/{board}`. The page hand-copies the dashboard's `SelectMenu` and `SpendBars` because a static page cannot import Vue components: change them together. `tests/test_rankings.py`. |
| `/showcase` | Tiles written in the HTML with `CollectionPage`/`ItemList`. The X wall is a committed snapshot (`media/showcase/wall.json`); the page never fetches from X. Hosted-only. `tests/test_showcase_page.py`. |
| `/docs`, `/docs/api` | Server-rendered reference from `app.openapi()`; Swagger UI moved to `/docs/api` and disallowed. ReDoc is off. |
| `/media/og.png`, `/media/brand/*` | The 1200x630 social card and stable hot-linkable brand files (copies of `assets/brand/twitter/`). |
| `/{INDEXNOW_KEY}.txt` | The IndexNow key, on every host (not a secret: the protocol only checks it is served from the submitted host). `scripts/indexnow_submit.py` pushes the live sitemap to `api.indexnow.org`. Google has no sitemap ping; resubmit through Search Console. |

Hosted-only pages check `_hosted()` (`public_url` against `PUBLIC_HOST_ALIASES`); elsewhere they
404 and the sitemap omits them, because their copy describes treg.to's own listings and grant.
Links to them in bundled files sit inside `<!--hosted-->...<!--/hosted-->`, which `_strip_hosted`
unwraps on the hosted deployment and removes elsewhere, so no served page links a 404.

Every route is registered in `bootstrap.py`'s route-ownership manifest.

## The `_page()` shell and tracking scripts

`_page()` in `routers.web` owns `<title>`, meta description, canonical, og/twitter card, JSON-LD,
`/adtrack.js` and a "Start free" CTA carrying `?ref=<page>` (the boot treats `ref` as a use-case CTA:
no bounce, sign-in opens in place, and the page that produced the signup is recorded). A page
without `adtrack.js` silently loses paid attribution: no `treg_ad` cookie, so `org.ad_gclid` stays
NULL and `adsconv.queue()` no-ops.
`test_pages_off_the_shared_shell_carry_adtrack` asserts one `adtrack.js` on a `_page()` caller. Pages outside the shell (`_static_page` files, `/docs/api`) carry their tags by
hand; nothing sweeps every route, so a new standalone ad destination must add the tag itself.

`/sitetrack.js` is deliberately **not** in the shell. It loads PostHog, and `web/privacy.html`
promises no analytics or session-replay scripts on the signed-in dashboard; broadening it across
the programmatic pages is a product/legal decision, not a side effect of an attribution fix.
Hand-written marketing pages take the opposite default and do load it, or they emit no pageview at
all. `tests/test_sitetrack.py` enforces that any `web/*.html` carrying an ad script also carries
`sitetrack.js`. Script paths are root-absolute
(`/sitetrack.js`); the test forbids the relative form.

Public sign-in dialogs share `media/signin-sso.js`: email code first, company SSO as an alternate
mode. Company discovery picks the verified team route and never grants access itself.

## The public catalog is the dashboard

`/catalog` and its sub-routes serve the compiled dashboard entry, so the public page and the member
view render the same per-job comparison and cannot drift into two implementations. The catalog API
is unauthenticated; `publicCatalog` (`frontend/src/state/data.js`) is set from `catalogFromPath()`
before the `/auth/me` check, so the first paint is already public. Public mode swaps the app chrome
for a marketing nav, hides session-only views and connection badges, and routes every action
(Try-it, Connect, BYOK, Start free) to the sign-in modal in place.

- **Never point a public CTA at `/app`.** A signed-out `/app` visit `location.replace('/')`s to the
  landing with no modal, losing the page. Call `openSignin()`. The modal is a sibling of both the
  signed-out and app branches so public mode can open it.
- `platProviders`, `provName` and `mkKnown` fall back to the open response's `providers` map (their
  normal source, `/connections`, needs a session). `mkOauth` has none (no `auth_kind` in the open
  response), so public mode offers BYOK, true for every provider.
- `/catalog/<slug>` asks for `include_hidden=1`, matching `loadPlatform`; a different population
  would put two endpoint counts on one URL.
- `frontend/e2e/dashboard.spec.ts` covers public navigation and reachable sign-in.

**No-JS fallback.** Vue replaces `#app` on mount, so prerendered markup is a sibling `#prerender`,
visually hidden (`_PRERENDER_HIDDEN`) and removed on boot; it stays for readers that run no script
(most AI crawlers and agent fetchers). It carries text (names, summaries, providers, prices), not
the ledger's client-side row merging, which would recreate the duplicate implementation. The
response also embeds `/catalog/platforms` as `<script id="catalog-platforms">`, read by
`loadPlatforms` instead of fetching. `/search` instead paints a full-viewport ground so it opens
with no loading step.

**Adding a platform** needs no code: everything reads one `catalog_store.load()`. It does need a
`platforms:` entry in `capabilities.yaml`; without one the shelf is `category: "Other"`, which the
app grid skips while the sitemap still publishes it
(`test_no_shelf_is_published_that_the_app_grid_hides`).

**Changing shared UI** reaches the public pages automatically, except: elements built on
member-only state (`providers`, `connCount`, `billing`, `sessionMode`) render blank publicly; a new
member-only action is visible to signed-out visitors until gated on `authed` or `publicCatalog`;
and the `#prerender` text and SEO head are server-built (`_spa_catalog_page`), so changing what the
page says means updating them too. Anything added to the `App.vue` shell (a promo banner) lands on
every crawlable shelf unless gated on `!publicCatalog`, with a test that `#prerender` never carries
it.

## Things that will bite you

- **`{BASE}`, never a hardcoded `treg.to`.** Self-hosted registries serve these pages; a hardcoded
  canonical tells their crawler the page lives elsewhere (`test_no_page_ships_an_unsubstituted_base`). `landing.html` and
  `_static_page` files are substituted; the dashboard's `index.html` is **not**, so hardcode
  absolute URLs on the app side.
- **HEAD.** FastAPI's `APIRoute` never adds HEAD, and crawlers probe with it first. The composition
  root widens GET-only routes after registration while the OpenAPI wrapper hides the implied HEAD
  operations ([composition](../architecture/composition.md)).
- **`/catalog/<slug>` sits in front of the JSON routes.** `/catalog/platforms`, `/catalog/search`,
  `/catalog/endpoints/...` and `/catalog/examples/...` match only because they register first;
  `_CATALOG_RESERVED` is the second guard (`test_the_json_catalog_routes_still_answer_json`). Swallowing one
  breaks the dashboard and every installed CLI.
- **Structured data must match the visible page.** Google treats schema claiming something the page
  does not say as a violation. The landing's `Offer` figures are asserted against the rendered
  HTML, every FAQ in `support.html`'s schema must appear in its body, FAQPage questions on agent and
  use-case pages appear verbatim, and a HowTo mirrors the visible setup steps in order. Edit both in
  one commit.
- **Prices need `_usd_short`, not `%g`** (scientific notation below `1e-4`). Sub-hundredth-cent
  renders `<$0.0001`, which must be HTML-escaped at every use site.
- **Title matches H1**, and a price label carries its billing unit ("/result", "/call"), so copy
  never says "per call" beside it. Titles fit `_TITLE_MAX`; descriptions go through `_serp_desc`.
- **Sitemap.** `_SITEMAP_PAGES` spreads `_USE_CASES` (defined above it) so a routed page cannot be
  forgotten. List bare paths only: `/use-cases/<slug>/` 307s and listing a redirect is worse than
  nothing. `test_every_sitemap_url_answers_200` walks it.
- **A sitemap is not a crawl path.** Pages in the sitemap but linked from nothing stay unknown to
  Google. The `_page()` footer, the landing footer and the `/catalog` prerender link the hubs;
  `/tools/<provider>` links every job page it answers (`_jobs_by_provider`) and each job page links
  workflows that chain it (`_workflows_by_capability`), both derived from the rendered tables.
- **`catalog.css` is stamped `?v=<mtime>`**; it has a real `max-age`.
- **Counts are never typed.** Headline numbers come from `catalog_store.headline_counts` through
  `_fill_headline` (`{ENDPOINTS}`/`{PROVIDERS}`); `marketing/landing/build_html.py` emits the
  placeholders. Untemplated copy (`tutorial.js` and mirrors, `README.md`, the CLI) states no count.
  `test_no_page_types_the_catalog_size` holds this.
  Catalog tiles count only their browse surface (`catalog_store.HIDDEN_KINDS`).
- **Routed rows and a vendor named "treg" never appear publicly.** Every public projection filters
  through `_pub` (`catalog_store.browsable`: hidden kinds and `kind: routed` out); `/tools/treg`
  404s (`test_routed_rows_never_surface_a_provider_named_treg`).

## The social card

`assets/brand/og-card.html` is the source and `src/treg/web/media/og.png` the render: open the HTML
at exactly 1200x630 headless and screenshot. Provider favicons are baked in at render time. Every
brand on it must be a provider in `catalog_store.load()`. Every page uses the shared card.

## Agent pages - `/agents/<agent>` and `/agents`

One page per client, the keys of `agent_pages.AGENTS`; anything else 404s. Demand is for the agent
("chatgpt connectors") and the platform ("linkedin api pricing"), not "how to <job> in chatgpt", so
jobs are rows on the agent page, never URLs. `agent_pages.py` has no heavy imports (light CLI).

- `USE_CASES` is the buyer's menu: plain-words jobs under buyer categories, each mapped to
  capability ids. It is re-cut as the catalog grows, so it is **metadata, never a URL**.
  `test_every_use_case_capability_exists_in_the_catalog` holds the ids. A category names one axis
  (what the job is about, never an access mode); no job appears in two categories and no two jobs
  share a capability set. `CATEGORY_GROUPS`
  renders stages inside one category as sub-headings, not categories. Never a row per endpoint.
- Plan prices for the economics block come only from `agent_pages.PLAN_PRICES`; with none listed the
  anchor falls back to the catalog's own price spread rather than an invented number.
- Slugs resolve to the table's own key and other casings 301 to it: a cased duplicate would be a
  self-canonical duplicate page, and the slug reaches canonical, `rel=alternate` and JSON-LD
  unescaped, so it must come from the table (CodeQL `py/reflective-xss`).
- Near-identical template pages are left unindexed by Google, so an entry may carry `h1`, `guide`
  (hand-written sections, rendered to the `.md` twin by `_guide_md`) and `focus` (categories printed
  in full; the rest collapse into one table linking `/use-cases#<category>`). Tests hold overlap
  with the template page under a threshold, guide endpoints and links real, and every category
  reachable.
- The Hermes guide shows a manual `~/.hermes/config.yaml` block: `treg mcp install` detects Hermes
  but only prints instructions (`test_the_hermes_page_and_treg_mcp_install_agree_on_the_server`).
- Agent pages list every `WORKFLOWS` entry.

## Use-case pages - `/use-cases/<job>` and `/use-cases`

The reader does one thing, the prompt; the rest is what the agent sees before it calls. The page
takes one of three forms chosen from the catalog: `short` (one provider), `platforms` (the job spans
platforms; cheapest per platform) or `compare` (one platform, several providers).

- **The URL is flat; the category is metadata** (breadcrumb, hub heading). A taxonomy re-cut must
  never move an indexed page. The nested legacy shape 301s to the flat one, and renamed slugs 301
  through `USE_CASE_REDIRECTS` on both shapes
  (`test_possessive_slug_redirects_hold_in_every_shape_they_were_live`); only `USE_CASE_PAGES` keys
  are sitemapped.
- **Cheapest is claimed per billing unit, never overall**; a page mixing units says they are not
  interchangeable. `{cheapest}` expands to a bare price of one unit (`cheapest_by_unit[units[0]]`),
  so write "from {cheapest}", and never use it on a page whose providers meter in more than one unit.
- **Rows key on (provider, platform)**, or a provider serving two platforms drops one.
- **Price states:** free is read from the cost's own `type`; a row with no USD figure prints "no
  dollar rate published", never "free"; a trial-pool row (`catalog.trial_pools`) states its daily
  allowance and own-key fallback (`trial_single` for a lone provider); a lone metered provider is
  `metered_single`, never own-account copy. A free-but-rationed own-account row is excluded from the
  cheapest claim. Pages with any free own-account row render `WHY_TREG_OWN_KEY` instead of
  `WHY_TREG`, whose cards are false there.
- The reliability section renders only when `endpoint_stats.observed` has traffic, captioned as
  live traffic, not a controlled benchmark.
- Nothing job- or agent-specific lives in the route: copy comes from `USE_CASE_PAGES` by slug, and a
  spec's `label` must match a `USE_CASES` row exactly. `voices` (real quoted questions, with vendor
  astroturf filtered out) and `voices_intro` are required together or not at all.
- Section order: comparison, voices, notes, background, optional `failure_modes`, FAQ. Write
  position-neutral copy ("the comparison here").
- A `related` card resolves its owner by label alone (`_related_link`); labels are unique.
- A platform label is catalog vocabulary; a page cannot override it.
- Not every menu job gets a page: a page nobody searches for is scaled content. Pages are written
  for jobs with measured demand.

**Campaign hubs.** The five `/use-cases/*-for-ai-agents` ad landings are built from
`marketing/landing/` by `build_html.py`, served first by the flat handler (a rendered job page
cannot shadow one) with `{BASE}` substituted per request, and have no `.md` twin. Breadcrumb root is "treg.to"; FAQ pairs come from the "Before you sign up" section;
the kicker count is read from the catalog at build time and floored; receipts are from real runs.

## Workflow pages - `/workflows/<slug>` and `/workflows`

A workflow page is the sequence a real person runs, as one paste-in prompt, with a step table priced
live from the catalog, a worst-case total, the receipt of a real run, `WHY_TREG`, failure modes and
FAQ; JSON-LD BreadcrumbList, `HowTo` (the table rows) and FAQPage; `.md` twin; hosted-only;
case-folded 301 like the use-case pages. Data: `agent_pages.WORKFLOWS`.

- **A workflow page is not written without a real run behind it.** The receipt and CSV are
  hand-recorded and dated, printed verbatim, and nothing is computed from them.
- **Prices are never written into copy**; only the receipt holds a dollar figure. The step table
  prints the live catalog rate.
- The CSV (`src/treg/workflow_runs/<slug>.csv`) carries row-level outcomes only, never a name,
  title, handle or address. `once` names steps called once per run rather than per row.
- A `decision` step is a judgement on fetched rows (jev, through the team's own key), priced from
  `agent_pages.DECISION_STEPS`, the one place that rate lives.
- Optional keys `description`, `agent` and `sections` override the generated meta description, the
  example agent and add hand-written sections.
- Tests require every step capability and endpoint to exist, the CSV to carry no PII, and a CSV to
  be served for every run.

## Provider pages - `/tools/<service>`

`tools_provider` lists all active non-routed provider tools including account/helper operations
(the browse census still excludes management tools); small inventories list every tool. Mixed-access
pages show total, platform + BYOK and BYOK-only counts. Prices use `Catalog.cost_view` display
units. A tool counts as platform only when it is platform-eligible and the deploy holds an enabled
key for its provider (`platform_key_for`, the same two halves as MCP's `no_key_needed`); a keyless
provider's page reads as own-key, and only platform prices inform the starting price. OAuth rows use
account-connection language. The kicker reads `_observed_or_empty`. Own-account providers where
search demand is for "<provider> mcp" (`_MCP_INTENT_PROVIDERS`) lead title and H1 with MCP.
`tests/test_provider_pages.py`.
