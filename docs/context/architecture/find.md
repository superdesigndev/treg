---
title: Find tools for a job - /catalog/find, the agent search experiment, recall by job and one judge request
status: building
sources:
  - src/treg/application/catalog_find.py
  - src/treg/domain/catalog/find_recall.py
  - src/treg/application/find_index.py
  - src/treg/infra/embed.py
  - src/treg/bootstrap.py
  - tests/test_find_index.py
  - tests/test_embed.py
  - src/treg/alembic/versions/0055_find_v2_log.py
  - src/treg/alembic/versions/0056_searchlog_verdict.py
  - scripts/find_bench.py
  - tests/fixtures/find_bench.yaml
  - tests/test_find_bench.py
  - tests/test_find_recall.py
  - tests/test_catalog_find.py
  - tests/test_search_queries.py
  - tests/fixtures/search_queries.yaml
  - frontend/src/state/find.js
  - frontend/src/components/FindAnswer.vue
  - frontend/src/pages/SearchPage.vue
  - frontend/src/components/CatalogSearch.vue
  - frontend/e2e/catalog-find.spec.ts
  - src/treg/application/catalog_search.py
  - src/treg/application/search_experiment.py
  - src/treg/domain/catalog/interleave.py
  - src/treg/infra/judge.py
  - src/treg/alembic/versions/0041_searchlog.py
  - scripts/search_experiment_report.sql
  - scripts/search_agent_bench.py
  - tests/test_search_experiment.py
  - tests/test_catalog_search.py
  - tests/test_search_agent_bench.py
related:
  - architecture/catalog.md
  - interface/dashboard.md
---

# Find tools for a job

Two surfaces read a loose recall through one relevance-judge request:

- **People:** `GET /catalog/find?q=` (`application/catalog_find.py`), behind the dashboard's Catalog
  box and the public `/search` page. Anonymous, rate limited, streamed.
- **Agents:** `catalog_search` on both MCP surfaces and `/catalog/search?engine=agent` (the CLI's
  search) run `application/catalog_search.py`: the shipped ranker's page
  ([catalog search scoring](catalog.md#search-scoring--most-words-must-match-and-the-rare-ones-decide)),
  then the discovery experiment's say over it (`application/search_experiment.py`). Plain
  `/catalog/search` stays the keyword search.

Nothing here touches `store.search`'s scoring, the evidence rerank, `/call/` or money. Lexical
search fails semantically in two ways: a task-phrased query whose rare words are parameter values
admits nothing, and an unrelated row sharing a word admits the wrong thing. The judge exists for
those.

## The judge (`infra/judge.py`)

TypeSafe's System One API (Jev). One request carries the query and every candidate as `state`, one
Noul question per candidate, and returns a probability per row. A caller may attach Noul `criteria`
and `extra` questions (Noul, or Choice with each option's probability); a candidate in `job_view`'s
shape is asked about a whole job under `JOB_CRITERIA`. Answers are cached in-process by (model,
query, candidate ids, criteria and extras).

It never raises: a timeout, non-200 or malformed body returns `probs=None` with a reason and the
caller serves the lexical page. A refusal every later request will also meet (401, 402, 403) is an
outage the fallback hides, so it is logged at ERROR, rate-limited per status. An unconfigured judge
(`typesafe_api_key` empty) makes every mode behave as off. **No second model request per search,
ever.**

Judged pages are **bucketed, not sorted by probability**: rows under `search_judge_keep` drop, rows
at or over `search_judge_high` go first, and inside a bucket the lexical order holds, so
`store.rerank`'s evidence buckets keep their say. **No probability reaches an agent**, in any mode:
exposing it would change how agents pick.

Every path closes its DB sessions before the judge or the embedding is awaited (rate admission,
hub read, caller lookup); the evidence read happens after the judge answers.

## People: `/catalog/find`

`find_engine` is the rollout and the rollback:

| value | served | logged |
|---|---|---|
| `v1` (default) | endpoint recall | one `SearchLog` row, `engine=v1` |
| `v2` | job recall | one row, `engine=v2` |
| `shadow` | v1 | both; v2 runs beside v1 and never reaches the page |

Both stream two NDJSON events: `candidates` as soon as recall is computed, `judged` when the judge
answers. An abstaining judge falls back to the keyword page (`store.rank_band`, unjudged, verdict
`keyword`), except that an unambiguous provider name still opens that provider's tools. `admit`
rate-limits per IP and per deployment through `ratestore`.

### v1: endpoint recall

`store.candidates` (every concrete endpoint hitting one required token, lexical order, cut at
`find_candidates`) read by one judge request under `FIT_CRITERIA`, plus one Noul asking whether the
text is only a name. Verdicts: `strong`, `closest`, `none`, `keyword`, `name`. Its weakness is the
unit: a job sold by thirty vendors spends thirty judge seats or none.

### v2: recall by job

`domain/catalog/find_recall.py` is pure, built once per `Catalog` and cached on it. Units:

- **job**: a capability; its card is the id's words, description, platform label and the first
  member names. One unit carries every vendor.
- **representative**: a member whose own card outscored its job's in some channel, judged on its
  own (the job may not be what it does). The long-term fix for such a member is its own capability.
- **uncatalogued endpoint**: no capability; its own unit.

Only the browse surface counts (`store.browsable`, no paused provider: catalog.md § Paused
providers). A first-party endpoint may share its capability's id, so the index keeps jobs
(`job_pos`) and endpoints (`pos`) apart.

**Channels.** The lexical channel scores each unit by the idf of every query word its card holds as
a whole word: folded (NFKD, diacritics off, CJK kept), stopwords and single letters dropped, lightly
stemmed, `aliases.yaml` phrases matching when all their words do, platform-naming words counting
double. A platform is named by its whole name as a token sequence, longest first ("tiktok ads
library" names TikTok Ads, not TikTok). A word of five letters or more may also prefix a word in a
unit's id, platform or provider names, at half weight. The semantic channel scores units by cosine
against the query's vector and is off when there is none. Each channel max-pools a job over its
members; ties go to the unit whose own card scored higher, so one member mentioning a common word
does not seat its job.

**Fusion and seats.** The channels fuse by reciprocal rank; the lexical channel admits only units
with a hit, the semantic one its most similar whatever the sign, so a query with no word on any card
still fills seats by meaning. Seats in order: jobs of a provider the query names, jobs on a platform
it names (`find_platform_seats` each), the best jobs (`find_jobs`), representatives of those jobs
(`find_delta`), uncatalogued endpoints (`find_raw`). `?platform=` keeps one shelf.

**The judge request** (`judge_v2`) asks of each job, via `infra.judge.job_view`, whether tools that
do it accomplish the task; an endpoint keeps the v1 question. Two extras ride along: the name Noul
and, off a shelf, a Choice over the platforms plus `none` (`platform_question`), which only
classifies and records. An empty recall still sends the extras, so it is told apart as a gap or not
a task.

**The name table** (`find_recall.name_of`) is string lookup, not judgement: a platform (exact name
or slug; or from four letters a prefix of exactly one platform's name), else a provider (exact name
at any length; or from four letters a unique prefix), else a product or model name drawn from the
AI-generation platforms' endpoint names (minus `aliases.yaml` keys, plus adjacent pairs). A word
several platforms or providers share names none of them. On a shelf only a provider there counts.

**A provider named with a task** (`store.provider_focus`, shared with the keyword page so the two
cannot disagree): the provider is named by id or whole display name as a token sequence, matched
before function words drop (a domain is part of the name: "you.com"; "you" alone is a word). A
platform's name names the platform; a name of only short or function words names nothing. The rest
of the query is what recall and the judge read; the provider's jobs and rows lead and are never
folded, and no routed row leads. Focus holds only where one of the provider's rows matches a word of
the rest ("does perplexity mention my brand" reads as typed).

### v2: the semantic channel

`application/find_index.py` holds one float32 matrix per catalog (numpy, `[server]` extra);
`infra/embed.py` is an OpenAI-compatible `/embeddings` client (`find_embed_url`, `find_embed_model`,
`find_embed_timeout_s`; `find_embed_api_key`, falling back to `platform_key_openrouter` with the
OpenRouter URL). The client never raises: any failure is an `embed_error` and the find goes on
lexical. Query vectors are cached in-process by model and folded text.

The build starts with the process (`warm`, a lifespan background task on every role, parse and build
off the event loop) or with the first v2 find; finds answer lexically until it is done. Card vectors
live in the archive's object store under `find-vectors/<model slug>/<sha256 of the card>`; only
missing cards are embedded and written back. These are the store's one non-content-addressed
objects: names built by treg from a validated slug and a digest, each body carrying its own card
hash and size (`MAGIC`, `dim`, hash, floats). Transient batch failures retry briefly; a refused key
or wrong-size vector does not. A new model is a new prefix, so models never mix. No lock (two
instances may embed the same cards) and no DB connection during the build.

### v2: the verdict

`decide`, the first rule that holds:

| # | condition | verdict |
|---|---|---|
| 1 | the judge abstained | `keyword`, reason the judge's error |
| 2 | the query is exactly a name, or a name's prefix with name p >= `find_name_min` | `name` (wins over strong) |
| 3 | no strong fit, at most three words, the name table matches | `name` |
| 4 | no strong fit, name p >= `find_name_min`, no name matched, nothing kept | `none`, `gap` |
| 5 | platform Choice is `none` with confidence >= `find_gap_min` | top under 0.6: `none`, `gap`; else `closest`, never strong |
| 6 | a fit at or over high | `strong` |
| 7 | a fit at or over keep | `closest` |
| 8 | nothing kept, platform Choice names a platform with confidence >= `find_gap_min` | `none`, `gap` (the platform is listed, this job on it is not) |
| 9 | otherwise | `none`, `not_task` (an agent's search asks for `keyword` here instead) |

Rule 5 precedes the strong rule: a confident "no platform provides this" caps the answer. On a
shelf the Choice is not asked and any `none` is reason `scope`: one shelf cannot say the catalog
lacks anything.

**Rows** (`expand_groups`, one group per kept unit; `expand` flattens). A job at or over high lists
every vendor in evidence order (`store.rerank`), each with the job's fit and `fit_from: job`. A job
between keep and high is folded to its first few providers, with `children_hidden` counting the rest
so "4 of 30 providers" adds up to the vendor count the judge saw. An endpoint unit at or over keep
is its own row (`fit_from: endpoint`); a member judged on its own keeps its own fit and is left out
under keep. A name's page (`name_page`) is unjudged. Measured success is read by the route's
`EndpointObservationReader` only for strong or closest answers, after the judge.

**Events.** The first `candidates` event is the lexical recall, sent before embedding; a second
carries the fused units when the vector changes them. `judged` adds `reason`, `platform: {choice,
confidence}`, `engine`, `embed: {ms, error}`; rows add `fit_from` and `children_hidden`; `named` is
`platform`, `provider` or `product`.

### The pages

`state/find.js` sends `engine`, `reason` and `platform_choice` with `search_answered`. An empty
answer says whether it is a gap (with a request link) or not a job (without). On a shelf an empty
answer offers "Search all tools" (`findEverywhere`, the same words asked unscoped) and never a
request, since a shelf cannot know the tool is missing. Box behavior: [dashboard](../interface/dashboard.md).

## Agents: the discovery experiment

`search_experiment` is the mode and the kill switch:

| mode | who sees what |
|---|---|
| `off` (default) | the shipped ranker; nothing here runs |
| `shadow` | the baseline; both pages computed and logged |
| `interleave` | most callers a team-draft merge of both pages; two holdouts (`search_experiment_holdout_percent` each) see a pure baseline or pure judged page |
| `v2` | most callers the job-first answer; the same two holdouts (baseline, v1 judged). A baseline holdout still has v2 computed and recorded as the counterfactual |

v1 recall for agents is `store.candidates` cut at `search_experiment_candidates`; routed parents are
put back by `with_routed_parents` (shared with `search`), and the judged page reuses the baseline's
own finishing function (evidence rerank, routed grouping, cut), so only the judge differs. Page
length is the same in every arm, so "more options" cannot pose as "better options".

**Arms are dealt per caller**: by team and sign-in email (`identity_key`), else a salted hash of the
bearer token (`caller_key`). Dealing by token would move an OAuth caller between arms each time its
token rotates. `search_experiment_salt` re-deals.

**Interleaving** (`domain/catalog/interleave.py`) is team draft: per round a coin picks who drafts
first. Credit goes only where the pages disagree (a row only one carried, or ranked differently);
identical pages contribute nothing.

### The job-first answer (`v2`)

`catalog_search` runs `catalog_find.recall_with_meaning` and `answer_v2` under the agent's budget
(`typesafe_timeout_s`, `find_embed_timeout_s`) and lays it out with `agent_page`:

- A judged answer keeps its best `page_groups(limit)` jobs; the page deals rows round-robin across
  groups up to `limit`, then lays them out job by job. Inside a job, members the judge rated at or
  over high on their own lead, then evidence order. A routed parent (`store.routed_parent`) leads
  its job when `routed_discovery` is on (strong and closest only). A listed hub tool whose
  capability is a kept job joins that group; none joins an empty page.
- Rows carry `job` and, on a job's first row, `more_providers` (folded plus cut); `score` is null;
  `jobs` lists the page's jobs with vendor counts and rows shown.
- Verdicts: `strong`, `closest` and `name` with a `hint` saying only what to do next (`catalog_get`
  on the routed row, else the first row). A **gap** is an empty page with `reason: gap` and a hint to
  file `catalog_request`, never near misses (whose advice, retry with fewer words, is the opposite);
  that answer is what stops an agent re-querying. "Not a task" is never served to agents: their input
  always means something, so `decide` is asked for `keyword` and the lexical page is served
  (`keyword:not_task`). An abstaining judge or a failed answer also serves the lexical page.
- **Per-caller cap.** Search is unmetered, so `search_judge_max_per_caller_hour` (a `ratestore`
  window) is the only bound on an agent looping on the judge. It is keyed by team and email; an
  unresolved MCP caller shares one bucket (the tool reads the catalog without validating a token,
  so a made-up token would otherwise be a fresh cap), and on the HTTP route an unresolved caller is
  keyed by address (`ip:<address>`). Past the cap the lexical page is served as
  `keyword:rate_limited`.

A recently-added search (`added_within_days`, `sort=newest`) bypasses the experiment entirely: no
judge, no record.

## What is recorded

All rows are fire-and-forget through `audit`; a dropped row costs one sample.

- **`SearchLog`**, one row per find (mode `find`, source `web-find`, no identity) and per agent
  search while the experiment is not off (with team and email, mode, arm, baseline page, judged page
  with probabilities, the served page with each row's owner, `baseline_total` (0 means the lexical
  gate admitted nothing), whether the pages differ, judge latency, tokens and error). Every served
  row in `shown` is `[id, owner, job]`, so a call to any vendor of a shown job can be credited. v2
  rows add `verdict` (reason after a colon: `none:gap`, `keyword:not_task`), `platform_choice`,
  `platform_conf`, `name_p`, `recall_ms`, `units` as `[kind, id, p]`, `embed_ms`, `embed_error`
  (`off`, `not_ready`, or the client's reason).
- Agent rows carry identity because the outcome is the caller's later `call` (`CallRecord.org_id` +
  `user_email`); anonymous finds have no outcome to join.
- **`SearchMiss`**, with `reason` (`gap`, `not_task`, `judge_off`, `scope`) and `engine`. Only the
  served engine files a miss (in `shadow`, v1), so a search never counts twice. In the agent v1 modes
  a miss is still written whenever the **lexical** page is empty, whatever the judged page found:
  that log measures the shipped ranker's coverage and feeds `aliases.yaml`. A served v2 agent answer
  files a miss on a gap or an empty keyword fallback.
- MCP searches also emit `catalog_search_judged` to PostHog (lossy by design); decisions read the
  database.

## Measuring it

- `scripts/find_bench.py` scores a find engine on labeled queries: the `recall` tier calls nothing;
  the `judge` tier reports verdict accuracy by stratum, false-strong, false-none, top-1, MRR, tokens,
  latency and **job coverage** (of a gold job's vendors, how many the page shows), the first number.
  Judge answers and query vectors are cached on disk. CI runs the recall tier on the synthetic
  `tests/fixtures/find_bench.yaml`; a label that no longer matches the catalog stops the run.
- `scripts/search_agent_bench.py` scores the v2 agent answer offline against agents' searches and
  what they called next: **job-hit** (the called endpoint does a job the page shows), hit@limit and
  **false-none**. The label can only name what the caller was shown, so v2's numbers are a lower
  bound.
- `scripts/search_experiment_report.sql` reads arms against later calls from the database.

Labeled real queries live outside this repository; a score on the set the rules were tuned on is
optimistic.

## Not here

- No generated card expansions, no vectors in the repository, no model loaded in the process.
- No judge use outside catalog discovery, and no crediting of a `/catalog/find` answer with what the
  person did next.
