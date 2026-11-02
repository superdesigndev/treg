---
title: Web Arena - private comparisons and live provider observations
status: gated
sources:
  - src/treg/domain/web_arena.py
  - src/treg/domain/web_arena_scores.py
  - src/treg/application/web_arena.py
  - src/treg/application/web_arena_quality.py
  - src/treg/application/web_arena_publications.py
  - src/treg/application/web_arena_calls.py
  - src/treg/routers/web_arena.py
  - src/treg/routers/enterprise_sso.py
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

`/web-arena` is one standalone Vue page comparing web tasks (search, news, papers, YouTube, maps,
fetch, sitemap) across providers in Battle (parallel) or Waterfall (cheapest first) mode. It
shares Enrich Arena's shell, sign-up, setup dialog and saved team selection
([enrich-arena](enrich-arena.md)); `/web-arena/leaderboard` redirects to it. The page and run API
need `web_arena_enabled` (default false). `/web-arena/api/leaderboard` is the public aggregate
download: task summaries, update time, window, filters and sample counts, never queries or
responses.

## Quotes and runs

`web_arena.quote` takes a task and one input. `route.build_plan` supplies verified, scoped catalog
adapters and the team's credential tier; one endpoint per provider joins, providers without direct
capacity are dropped, and a signed-in quote removes providers the team cannot call. The public task
response shows the catalog preview and estimates before sign-in.

- A task that compares a fixed number of results drops an adapter that cannot send that limit
  upstream, except the named lists in `application/web_arena.py` (`FIXED_PAGE_SEARCH`: a fixed
  first page; `UNBOUNDED_SITEMAP`: an unbounded upstream request). Comparison always uses at most the
  first N returned rows. Per-task lineups (`NEWS_ENDPOINTS`, `PAPER_ENDPOINTS`, ...) are also there.
- Fetch and Sitemap read a bare address as its https site (client and server apply the same rule);
  other schemes, spaces and addresses without a domain are rejected before quoting.
- `ARENA_EXCLUDED` keeps Brave Search out of comparisons: its response-retention terms forbid saving
  result links for later viewing. Brave remains callable through the catalog and routed tools; old
  Brave runs keep only aggregate counts and scores.
- A quote freezes endpoint and adapter hashes; `start` rechecks them, locks the user for admission
  and checks team credit. Battle refuses a quote above the run ceiling; Waterfall stops before its
  quoted spend would exceed it.

Every leg in `_run` makes a direct `CallInput` for `service.execute_call`, so credentials (own keys
first), holds, settlement and cancellation are the ordinary path; overflow is disabled for a direct
comparison. Battle runs a bounded number of concurrent legs. Waterfall stops at the first provider
that returns a result (a valid result list, useful Fetch text, or a valid same-host URL list for
Sitemap, which never claims full coverage); the quality check scores that result but never decides
when a Waterfall stops. The run reads and closes the full provider stream, then saves a bounded
display result. No DB session is open during a provider request. An interrupted process never
retries an ambiguous provider call.

`WebArenaRun` holds encrypted input, quote, attempt state, quality data and results, readable only
by its creator in the same team, expiring after 30 days. Ratings live in the private payload and
never change the automatic check.

## Quality checks

Results save before checks; a check failure leaves provider data visible.
`WebArenaJudgeBudget` admits external quality calls under daily per-user and operations caps
before network I/O; treg pays them separately from provider charges. A local SQLite deployment on
a loopback URL skips the caps but still needs an AI gateway key.

- **Search-like tasks:** `web_arena_quality.search` sends the first few links, titles and snippets
  to Jev and shows an intent-match estimate. Freshness uses known source dates only; missing dates
  stay unknown.
- **Fetch:** text is counted with the fixed `word-or-symbol-v1` tokenizer (not model tokens). For two
  or more texts, a bounded LLM request lists facts from their union and Jev tests each text's
  retention: relative coverage that cannot detect facts every provider missed.
- **Sitemap:** URL syntax, exact host and duplicates, no Jev; coverage only against a separately
  known URL list.

Battle badges (fastest, cheapest, most relevant, token efficient) appear only when the compared
values are known for the eligible results; ties share; an unscored provider does not suppress the
relevance badge.

## Live observations

`web_arena_calls.collect` walks `CallRecord` with a locked cursor and commit lag, folding eligible
Web task endpoint calls into content-free daily buckets (`WebArenaCallDayStat`,
`WebArenaCallCursor`, owned by this module). It never reads an answer body. Only actual, uncached,
unrefused provider attempts count; adapter hit/miss verdicts and provider faults decide hit rate,
while unknown outcomes, caller 4xx and treg gateway failures do not. When decided calls are under
half the timed successful calls, hit rate stays unknown despite the sample floor. Only successful
direct calls give response-time samples. An Arena leg is counted once, through `CallRecord`, never
again from `WebArenaRun`.

`treg-worker web-arena seed` backfills a large audit backlog for an initial release: it scans only
listed Web task endpoints over a recent window into staging tables in bounded, resumable batches
(endpoint list, range and position frozen on the first pass, so no double counting). Only after
every endpoint is scanned does one transaction replace partial live totals, advance the call cursor
and record the observation start. It requires Web Arena to be off and refuses to run after a
successful seed; a failed pass leaves existing totals in place.

`web_arena_publications.refresh_live` joins call observations with checked quality from completed
runs (`summarize_live`) into one content-free `WebArenaPublication`, read by the preview and the
public leaderboard. Quality wins are Battle-only (they need simultaneous checked comparisons);
repeat inputs count once per provider. The `treg-worker arena insights` cron collects on each tick
and calls `refresh_live_if_due`, which skips the full rolling-window read while totals are fresh;
`treg-worker web-arena totals` refreshes manually. Local development reads recent calls and quality
on the request instead; if old local runs cannot be decrypted it labels the summary partial or
stale, and fails visibly with nothing readable. The hosted worker fails on an unreadable payload.

Leaderboard metrics appear only above their sample floors (decided calls for hit rate, successful
uncached calls for time, checked inputs for quality). Price values keep their catalog unit and the
UI warns when units differ. There is no Benchmark tab or fixed-case runner.
