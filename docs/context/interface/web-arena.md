---
title: Web Arena — private comparisons and live provider observations
status: gated
sources:
  - src/treg/domain/web_arena.py
  - src/treg/domain/web_arena_scores.py
  - src/treg/application/web_arena.py
  - src/treg/application/web_arena_quality.py
  - src/treg/application/web_arena_publications.py
  - src/treg/application/web_arena_calls.py
  - src/treg/routers/web_arena.py
  - src/treg/models.py
  - src/treg/alembic/versions/0057_web_arena.py
  - src/treg/alembic/versions/0058_web_arena_call_stats.py
  - src/treg/alembic/versions/0059_web_arena_seed_start.py
  - src/treg/alembic/versions/0060_web_arena_seed_progress.py
  - src/treg/config.py
  - src/treg/bootstrap.py
  - src/treg/worker.py
  - src/treg/web/web-arena.html
  - src/treg/web/web-arena/arena.js
  - src/treg/web/web-arena/arena.css
  - tests/test_web_arena.py
  - tests/test_web_arena_calls.py
related:
  - interface/enrich-arena.md
  - architecture/catalog.md
  - architecture/money.md
---

# Web Arena

`/web-arena` is one standalone Vue page; the old `/web-arena/leaderboard` path redirects there.
The page shares Enrich Arena's tokens, type, light canvas, fighter sprites, and dialogs, with its
own layout. There is no page navigation: the brand links home and the page is the arena. The
header carries Enrich Arena's GitHub, Discord, and X links, a "Setup treg in" pill that opens the
shared setup dialog, and, when signed in, a balance button that saves the draft and opens team
billing. On narrow screens the account menu holds setup, top-up, and sign out. The intro uses its
Treg credit. The provider leaderboard stays below private results when a run is open. The page links to the public
`/web-arena/api/leaderboard` JSON aggregate download;
it contains available task summaries, update time, window, filters, and sample counts, not
individual queries or provider responses.
The `branddev` provider keeps its catalog identifier and logo but appears as Context.dev in the UI.
`web_arena_enabled` defaults to false. The page and run API need the flag. Brand is visible but disabled.

`web_arena.quote` takes a task and one input. Search, News Search, and Paper Search ask for
10 results. YouTube Search and Maps Search compare the first ten returned rows. Maps queries
should name the area, such as "coffee shops in Austin TX". Fetch asks for one URL,
and Sitemap asks for a site URL with an optional search phrase and up to 10 URLs. A bare address
such as apple.com is read as its https site; other schemes are rejected. The phrase goes
to adapters that accept it; URL-only adapters continue with the URL, and Olostep joins only when
the phrase is present. Sitemap hides the Jev quality switch and records Jev as off; URL validity
checks always run. Sitemap result cards show the unique valid URL count without a coverage claim.
`route.build_plan` supplies verified, scoped catalog adapters and the team's
current credential tier. A task that needs a limit drops an adapter that cannot send that limit,
except Search1API Sitemap, whose response is compared using only its first ten URLs. Its upstream
request remains unbounded. Context.dev Map sends the selected limit upstream. The ten-URL bound
also permits Tavily Map on the shared key.
Brand.dev's fixed ten-result search can join a Search quote because it enforces that limit in
its adapter. The Web Arena quote checks this after planning the scoped provider candidates.
TinyFish and Crawl4AI have no count request field. A Search quote can include their
first page; Web Arena compares at most the first ten returned links. Other Search providers
must send the ten-result limit upstream or enforce a fixed ten-result page. Spider Search uses
listing-only mode so its search does not fetch the result pages.
News Search combines verified news endpoints from TinyFish, Search1API, Exa, AnyAPI, Serper,
Cloro, SerpAPI, DataForSEO, LiteScrape, and Tavily. TinyFish, Search1API, Exa, and Tavily use
the `web.search.news` catalog contract; the others use `google.serp.news`. LiteScrape fixes
Google's `nws` vertical, and Tavily fixes its `news` topic with basic depth and usage evidence.
Paper Search joins Exa and TinyFish publications with Serper Scholar. YouTube Search compares
JustOneAPI, SerpAPI, and TikHub video results. Maps Search compares Apify, DataForSEO, and
SerpAPI Google Maps place results. These lineups use platform-provided, verified catalog
adapters; the quote still chooses one endpoint per provider and calls it directly. Maps hides
the quality switch and records Jev as off, while Paper and YouTube use the optional intent check.
The Maps first-page comparison does not claim that providers searched an identical viewport;
the query itself should include a city or area. TinyFish, Cloro, and
SerpAPI use their first news page; the other providers receive the ten-result limit upstream.
Every news response is compared using at most its
first ten articles. News uses the same optional intent check and result cards as Search, while
retaining its own task and leaderboard totals.
The public task response shows verified adapter previews and a catalog-based price estimate, so
the provider lineup and logos appear before sign-in. The lineup is a catalog preview; a signed-in team quote removes providers it
cannot call. A changed input, mode, Jev choice, or provider selection refreshes the quote after
a short pause. The lineup is one line in the query card: up to eight selected provider logos,
then "+N of M" for the rest (or "of M" when some are off; nothing more when all fit). Edit opens a roster of provider chips ordered by quoted, then catalog, price; toggling a
chip never reorders it. Selected chips carry a teal border and check; chips that are off keep
their logo in color. While the roster is open the logo summary gives way to a live "N of M
selected" count, and, while some providers are off, an "All providers" switch sits beside Done;
turning it on selects every provider, then the switch fades away. Starting a run closes the roster. Battle still calls providers in parallel.
The signup dialog uses Enrich Arena's layout, OAuth availability from `/meta`, email-code
step, local development code notice, and legal links. After sign-in, Web Arena opens the same
shared setup dialog as Enrich Arena: a new user names a team, then chooses an agent and gets
setup instructions. An OAuth return resumes that setup, and the query draft survives the redirect.
The team switcher shares Enrich Arena's saved team selection and restores it on reload when the
user still belongs to that team. Creating a team saves that selection too.
Opening a saved run restores its selected fighters from the saved attempts and scrolls to its
results. Failed, empty, and downvoted attempts use the fallen fighter pose; other available
providers stay excluded. The current quote appears on the Run button, inside the query field,
without a separate price step; its tooltip notes that the final charge may vary.
The public leaderboard stays visible before and after a run and joins content-free live totals
by provider: hit rate appears after 20 decided direct
calls and median provider time after 20 successful uncached direct calls. The task-specific
quality estimate appears after 20 checked Web Arena inputs. Search, News Search, Paper Search,
and YouTube Search use Jev intent match,
Fetch uses relative fact coverage, and Sitemap coverage stays unknown without a known URL
list. A provider call made during a Battle or Waterfall enters the direct-call aggregate
once through `CallRecord`; it is not counted again from `WebArenaRun`. Repeat checked Arena
inputs count once per provider for quality, using the latest checked result.
The run form uses one quality switch with Jev and treg details in an info tooltip. A focused query has one outer
border. Starting a run scrolls to the fight stage, once; a reader who scrolls away is not pulled
back. The stage shows one lane per attempt in quote order, and lanes never reorder. Each lane is
first the loading state and then the result. A Battle lays its cards out in two columns (one
when the results area is narrower than about 720px); a Waterfall shows full-width lanes as a
relay: only providers that tried, or the one about to start, get a card, and the rest wait in one
"Next up" line that disappears once a provider returns results. Each card shows a provider
bot: a canvas-drawn robot head in treg ink with smooth light, the provider logo laid over it as
a vector image so it stays sharp. The bot hops while running (every third hop spins, and the logo
hides while the back faces the viewer), hops more gently while Jev checks a hit, stands while
waiting for a slot, dozes when it was never called, gives one happy hop on a result with a teal
glow beneath it, hops now and then as a badge winner, and slumps with a grey logo on a failure or
thumbs down. One shared animation loop draws every bot, pauses bots off screen, and draws a still
pose under reduced motion. A running card has a faint teal border and one soft light that glides
along its bottom edge; the strip's progress bar uses the same light. A queued card visible during a
run (the next Waterfall provider) stands awake rather than dozing. Status text shimmers ("Starting…", "Waiting for a slot" while four Battle legs run,
a task verb such as "Searching the news…", or "Checking quality…"), and a timer counts from the
first poll that saw the provider running; the final time is the saved provider time. Every hit
always shows its result count with intent match and help, badges, time and cost, its first
links (three in a Battle card, four in a Waterfall lane, two on narrow screens) marked by quiet
dots with the rank kept for screen readers, a bounded Fetch text preview, View all, View
provider response, and thumbs at the bottom right. Sitemap states its count once: "N valid site
URLs", or "N site URLs · M valid" when they differ. Failed cards stay one line with a plain reason
and their thumbs. Show fewer returns the viewport to that provider card in card view.
After a run, providers that were never called fold into one short line that
says why in plain words: an earlier provider returned results, the $10 run limit, an unknown
fee, or a stopped run. Above the cards, one strip shows a small working orb, progress as a thin bar, the amount charged
so far, elapsed time, and Stop and
stays pinned while the run is live; during a Fetch fact check it alone says so, and the cards
show no per-card check state; afterwards it shows the results count, the total charged, and
the view switch, and names the run's query only when the search box no longer holds it. Battle
cards in a row share a height with their actions pinned to the bottom; cards without results stay
compact. Starting a run with the roster open waits for it to close before scrolling.
The quality check's internal link list is not displayed separately.
Diffbot search rows use `pageUrl` for the link and `content` for the excerpt; both the result card
and Jev's bounded search input read those fields. Earlier runs keep their saved quality state.
The strip has an icon toggle for card and compact table views with tooltips. The table
keeps Fastest, Cheapest, Most Relevant, and Token Efficient badges below the provider name. Its
plus action expands the full links
or provider output; thumbs, cost, time, and quality stay in the row. The final visible row has
no bottom divider inside the table's rounded border.
Stop sits in the strip. A transient status-poll failure
keeps the run active in the browser and retries the poll; the saved run state controls Stop's visibility.
Completed Battle runs mark the fastest and cheapest successful results when all compared values
are known. Search Battles mark the highest estimated intent match among at least two scored,
successful results; an unscored provider does not suppress that badge. Ties receive the same badge.
These badges appear on cards and table rows. Fetch Battles mark the best token efficiency when all successful, non-downvoted
results have usable efficiency scores. Search results describe freshness from
known source dates in words. A result with no usable dates shows no freshness label.
An unavailable search match check leaves the score line empty.
One endpoint per provider joins the quote. Battle selects all by default, with a fresh quote
after a provider switch. Waterfall sorts by quoted cost and stops before its quoted spend exceeds
$10. Providers without direct capacity do not join a comparison. A quote freezes endpoint and adapter
hashes; `start` checks them again, locks the user for admission, and checks team credits.
Valyu is excluded from the Web Search lineup and quote until its web search price cap is verified.

Every leg in `_run` creates a direct `CallInput` for `service.execute_call`. Each child uses the
ordinary credential, hold, settle, and cancellation path. The run reads and closes the full
provider stream before it saves a bounded display result. Most provider results have a 256 KB
read limit; Apify Maps has an 800 KB limit and retains only the first ten places' display fields.
TinyFish News 429s show the provider's retry time when it sends a numeric `Retry-After` header.
No database session stays open
during the provider request. Own credentials still take priority in the call runtime. Web
Arena requests disable overflow for a direct provider comparison. A Battle runs at most four
legs at once. Waterfall runs one leg at a time, cheapest first, and stops at the first provider
that returns a result: a valid result list for the search tasks, useful text for Fetch, and a
valid same-host URL list for Sitemap, which does not claim full site coverage. With Jev on, the
check still scores that result for its card and the live totals; it never decides when a
Waterfall stops.

`WebArenaRun` holds an encrypted input, quote, attempt state, quality data, and results. Only the
creator in the same team can read it. It expires after 30 days. A process interruption does not
retry an ambiguous provider call. Ratings live in the private run payload and do not change the
automatic check.

`web_arena_quality.search` sends the first five links, titles, and snippets to Jev and shows the
answer as an Intent match estimate, with the scoring scope in help text. It reads dates only when present. Freshness uses a disclosed 30-day
window over dated links when the query needs recent information; missing dates remain unknown.
Fetch counts words and symbols with the fixed `word-or-symbol-v1` tokenizer. For at least two
provider texts, a bounded LLM request lists up to 12 facts from their union. Jev tests retention
of each fact in each text. This is relative coverage and cannot detect facts every provider
missed. Web Arena reads plain page text, Olostep's `markdown_content`, Parallel's `full_content`,
and Brand.dev's nested `markdown.data` before deciding whether a Fetch returned usable text. Fetch cards show text count,
Fact coverage, and token efficiency as compact metrics with explanations. The text count is a
word-and-symbol count, not a model token count. Cards omit the underlying tokens-per-fact ratio
and the shared fact-list generation time. Card and table views both call the fetch metric Fact
coverage and explain its relative scope in a tooltip. Sitemap checks URL syntax, exact host, and duplicates
without Jev. It shows coverage only
when a separate known URL list exists. Results save before checks; a check failure leaves the
provider data visible. `WebArenaJudgeBudget` admits external quality calls under a daily user
and operations cap before network I/O. Treg pays those calls separately from provider charges.
Local development with a SQLite database and loopback public URL skips the quality-call caps
for testing. It still needs an AI gateway key.

`web_arena_calls.collect` walks `CallRecord` by a locked cursor with a commit lag and folds
eligible Web task endpoint calls into content-free daily buckets. Only actual, uncached,
unrefused provider attempts enter the buckets. Adapter hit/miss verdicts and provider faults
decide hit rate; unknown outcomes, caller 4xx, and gateway failures generated by treg do not.
When the decided count is less than half the timed successful count, the saved aggregate proves
most successful calls lack a verdict, so the hit rate remains unknown despite the sample floor.
Older gateway failures already folded before this rule remain in daily buckets until they age out.
Only successful direct calls provide
response-time samples. The collector processes backlog incrementally and never reads a provider
answer body. `WebArenaCallDayStat` and `WebArenaCallCursor` are owned by this module.
For an initial release with a large audit backlog, `treg-worker web-arena seed` scans only
listed Web task endpoints from the most recent ten days. It builds bounded, content-free
daily buckets in separate staging tables. Each invocation saves a bounded batch and reports
whether another invocation is needed. The endpoint list, time range, and scan position are
frozen on the first pass, so interrupted runs resume without double counting. Only after
all listed endpoints are scanned does one transaction replace partial live totals, advance
the call cursor to the high-water mark, and record the actual observation start. The command
requires Web Arena to be off and refuses to run after a successful seed. An incomplete or
failed pass leaves the existing live totals in place. The ordinary collector then handles later calls
and ages observations into the rolling 30-day window; it does not backfill the preceding
20 days. The page displays the shorter observation coverage while that window fills.
`web_arena_publications.refresh_live` joins those call observations with quality from completed
Battle and Waterfall runs and saves content-free totals in `WebArenaPublication`. Quality wins
remain Battle-only because they require simultaneous checked comparisons. The Arena preview and
public leaderboard read that one publication. The scheduled `treg-worker arena insights` command
collects new call observations on each tick and refreshes the publication
after the one-time seed or when Web Arena is enabled, at most once every 30
minutes. The cron can run more often; `refresh_live_if_due` skips the full
rolling-window read while the saved totals are fresh and retries on the next run after a
failed refresh. The standalone
`treg-worker web-arena totals` command remains available for manual refresh. Local development
reads recent direct calls and Arena quality on the leaderboard request so new test runs appear
without a cron worker. If older local runs cannot be decrypted after a key change, the local
quality summary uses readable recent runs for each task and labels that data as partial. A task with
no readable runs keeps its last saved quality totals with a stale-data label. If no run is readable
and no saved publication exists, the read fails visibly. The hosted worker still fails on an
unreadable payload.
`summarize_live` uses completed Web Arena Battle and Waterfall runs from the most recent 30
days, capped at the 10,000 newest runs, for checked quality. The direct-call buckets include
eligible dashboard, CLI, agent, Battle, and attempted Waterfall calls to the Web Arena's
listed endpoints. A skipped Waterfall provider has no call to count. The source, window,
filters, and sample floors travel with each saved publication.
The composer shows Web, News, Papers, YouTube, Maps, Fetch, and Sitemap as one row of task tabs
with a sliding active pill; a saved run opens its own task. When the row overflows, left and
right arrows appear only on the side that can scroll, and the selected tab scrolls into view.
Sitemap's optional topic sits behind a "+ Filter links by topic" control beside the mode switch.
Fetch and Sitemap show a fixed, muted https:// before the address field. A bare address such as
apple.com means its https site, and the server applies the same rule; a pasted full URL moves its
http or https scheme into the prefix on paste, with no doubled scheme. Spaces, other schemes, and
addresses without a domain are reported after a short pause in typing, on leaving the field, or
on Run, and block quoting and running.
A request that cannot reach the server shows a plain connection message. Motion follows the transitions.dev token scale and turns off under
`prefers-reduced-motion`. Signed-in query history stays beside the main content near the viewport edge on wide
screens; a toggle folds it into a thin rail with tooltips for History and New query, and the
choice is remembered. On narrow screens only the New query button shows.
The live leaderboard follows the task selected in the query composer and uses Enrich Arena's
comparison rail, provider logos, and hover or selection details. It opens on catalog price, the
first view for every task, and a zero price reads Free. Search offers price, hit rate, Jev
relevance, and price vs hit rate;
Fetch adds fact coverage, token efficiency, and fact coverage vs token efficiency; Sitemap uses
price and hit rate. Single metrics can appear as vertical or horizontal bars. Comparison plots
show both axes and scroll horizontally inside the chart when needed. Price values retain their
catalog unit, and the UI warns when units differ. Hit rate is visible with its decided-call
count; quality metrics appear after 20 checked inputs per provider. Hovering or focusing a
quality option shows an Arena-style tooltip explaining the score and its checked Web Arena
source. Fetch live publications
aggregate token efficiency separately from fact coverage, using the same checked-input threshold.
Quality win rate stays unknown until 20 comparable checked runs. Sitemap needs a known reference
URL list before any live quality win can exist. There is no Benchmark tab or fixed-case runner.

The worker entry point `treg-worker web-arena totals` manually refreshes live totals. Production settings belong in the paired
private repository after public code merges.
