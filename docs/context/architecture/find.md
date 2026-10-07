---
title: Find tools for a job - /catalog/find, recall by job and one judge request
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
  - frontend/src/state/find.js
  - frontend/src/components/FindAnswer.vue
  - frontend/src/pages/SearchPage.vue
  - frontend/src/components/CatalogSearch.vue
  - frontend/e2e/catalog-find.spec.ts
related:
  - architecture/search-experiment.md
  - architecture/catalog.md
  - interface/dashboard.md
---

# Find tools for a job

`GET /catalog/find?q=` (`application/catalog_find.py`) answers a person who describes what they want
done: the dashboard's Catalog search box (Enter, or a pause in typing) and the public `/search` page.
It is the discovery experiment's mechanism ([search-experiment](search-experiment.md): a loose
recall read by one `infra.judge` request, bucketed at `search_judge_keep` / `search_judge_high`)
served to people. The v2 engine below also answers MCP `catalog_search` in the experiment's `v2`
mode, laid out for an agent by `application/catalog_search.py`
([search-experiment](search-experiment.md)); `/catalog/search` answers from the shipped ranker, and
nothing here touches `store.search`, its scoring or the evidence rerank. [Vibe-it](vibe-it.md)'s
agent calls `stream` in-process for its `catalog_search` (the person's id stands in for the client
address in `admit`, under the same hourly windows) and falls back to `/catalog/search` when find is
not configured, over its limits, or the judge abstains.

## Two engines, one switch

`find_engine` picks the answer, and is the rollout and the rollback:

| value | served | logged |
|---|---|---|
| `v1` (default) | endpoint recall (below) | one `SearchLog` row, `engine=v1` |
| `v2` | job recall (below) | one row, `engine=v2` |
| `shadow` | v1 | both: v2 runs beside v1, its judge request in parallel, and never reaches the page |

Both stream the same two NDJSON events - `candidates` as soon as recall is computed, `judged` when
the judge answers - so the pages animate the wait on the first. The judge abstains rather than
fails; an unambiguous provider name still opens that provider's tools, while other abstentions
fall back to the keyword page (`store.rank_band`, 25 rows, unjudged, verdict `keyword`).
`admit` rate limits per IP and per deployment through
`ratestore` in a session committed and closed before the judge is called, and the evidence read
(below) happens after the judge has answered: no request holds a connection while Jev thinks.

## v1: endpoint recall

`store.candidates` (every concrete endpoint hitting one required token, lexical order, cut at
`find_candidates`) read by one judge request; a candidate question carries `FIT_CRITERIA`, whose
`false` side includes "the task only names a product, company or platform", and the same request
asks one extra Noul, whether the text is only a name. Verdicts: `strong` (a row at or over high),
`closest` (kept rows, none strong), `none`, `keyword`, or `name` (no strong fit and the judge reads
a name, or it is exactly a platform's name: the platforms whose label contains it, else a provider's
endpoints, unjudged).

Its weakness is the unit. A job sold by thirty vendors either spends thirty of the judge's seats or
none, and the page lists only the vendors the lexical order happened to reach.

## v2: recall by job

`domain/catalog/find_recall.py`, pure, built once per `Catalog` and cached on it
(`Catalog._find_index`, the `_search_fields` pattern). Three kinds of unit:

- **job**: a capability. Its card is the id's words, the description, the platform's label and the
  first names its members go by. One unit carries every vendor.
- **representative**: a member endpoint whose own card scored higher than its job's in some
  channel. Judged on its own: the vendor's wording is closer to the query than the job's, so the
  job may not be what it does (a personal-email finder filed under work email). A one-vendor job
  has none. The long-term fix for a member that does another job is its own capability.
- **uncatalogued endpoint**: no capability; its own unit.

Only the browse surface (`store.browsable`, no routed parents). A first-party endpoint may share its
capability's id, so the index keeps jobs (`job_pos`) and endpoints (`pos`) apart.

**Channels.** A lexical channel scores each unit with the idf of every query word its card holds as
a whole word: folded (NFKD, diacritics off, CJK kept), stopwords and single letters dropped, lightly
stemmed (the forms of one verb agree, "scraping" and "scrape"), `aliases.yaml` phrases matching when
all their words do, the words that name a platform counting double. A platform is named by its
whole name as a token sequence, from its slug's words and its label's short form, longest first
("tiktok ads library" names TikTok Ads, not TikTok; "search console clicks" names Search Console). A word of five letters or more may also be
the prefix of a word in a unit's id, platform or provider names, at half weight ("scrap" starts
every Scrapecreators row). A semantic channel scores each unit by the cosine of its card's vector
and the query's (below), and is off for a query whose vector is not there. Each channel max-pools a
job over its members and remembers which member
won; ties go to the unit whose own card scored higher, so a common word does not seat the jobs that
merely have one member mentioning it.

**Fusion and seats.** Each channel's top 300 fuse by reciprocal rank (k=60): the lexical channel
admits only units with a hit, the semantic one its 300 most similar whatever the sign, so a query
with no word on any card still fills its seats by meaning. Seats: the jobs on a
platform the query names first (`find_platform_seats`), then the best jobs to `find_jobs`, then the
representatives of those jobs in fused order (`find_delta`), then uncatalogued endpoints
(`find_raw`). `?platform=` keeps one shelf's units.

**The judge.** One request (`judge_v2`): a job reads `infra.judge.job_view` (id, description,
platform, vendor count, a few names) and is asked whether tools that do the job accomplish the task,
under `JOB_CRITERIA`; an endpoint keeps the v1 question and view. Two extra questions ride along:
the v1 name Noul, and off a shelf a Choice over the platforms plus `none` (`platform_question`). No
second model request, ever; the Choice only classifies and records. A recall with no units still
sends the two extra questions (a request of extras only), so an empty recall is told apart as a
catalog gap or not a task instead of defaulting to `not_task`.

**The name table** (`find_recall.name_of`) is string lookup, because a name is a lookup, not a
judgement: a platform (exactly its name or slug, listed with the others the name matches: exact
first, then one whose name starts with the query, then the shelves' featured order, then most jobs;
or, from four letters, a prefix of exactly one platform's name - the only match, or the only
platform whose own name the query starts without being a whole word of it: "instagra" is Instagram
though Meta Ads' label mentions Instagram), else a provider (its name exactly
at any length, "exa"; or, from four letters, a prefix of exactly one provider's), else a product or
model name. A word several platforms or providers share ("video", "search", "ads", "goog") names
none of them, and the judged answer reads it. Product names come from endpoint names on the `AI generation` platforms: words two or
more of those names share and names elsewhere rarely use ("gemini", "seedance", "flux"; not
"image"), or, on a platform with a single endpoint, words of its name no other name uses ("jev"),
minus the keys of `aliases.yaml` ("tts" is a way of saying a job, not a product), and
adjacent pairs of them ("nano banana"), matched with spaces and hyphens folded away.
On a shelf only a provider there counts.

## v2: the semantic channel

`application/find_index.py` holds one float32 matrix per catalog, a row per unit, and
`infra/embed.py` is the OpenAI-compatible `/embeddings` client (`find_embed_url`, OpenRouter by
default; `find_embed_model`, `voyageai/voyage-4-lite`). Per find: the query's vector (cached
in-process by model and folded text for an hour, `find_embed_timeout_s`), one matrix product, and
`recall` fuses it with the lexical channel. The client never raises: a timeout, a non-200, a
malformed body or a vector of the wrong size is an `embed_error`, and the find goes on lexical.

The build starts with the process (`warm`, a background task of the lifespan on every role,
cancelled with it; the catalog's parse and the index's build run off the event loop), or, where that
did not finish, with the first v2 find on a catalog; a find answers lexically until it is done. Each card's vector is read
from the archive's object store under
`find-vectors/<model slug>/<sha256 of the card>`, only the missing cards are embedded (batches of
96), and those are written back. The store is content-addressed everywhere else; these named
objects are the one exception, their names built by treg from a validated slug and a digest, and
each body carries its own card hash and size (`MAGIC`, `dim`, hash, floats) instead of a content
check. A batch that fails transiently (timeout, 429, 5xx) is tried twice more with a short backoff
before the build gives up; a refused key or a wrong-size vector is not retried. Without a store the
vectors live in the process only; without the API the channel stays off and a failed build is
retried after five minutes. A new model is a new prefix, so vectors of two
models never mix, and a cached vector of another size is not used. No lock: two instances building
the same new cards both embed them. The build holds no database connection.

The key is `find_embed_api_key`; left empty with the OpenRouter URL, treg's own OpenRouter key
(`platform_key_openrouter`) is used. numpy (the `[server]` extra) does the matrix product.

## v2: the verdict

`decide`, the first rule that holds:

| # | condition | verdict |
|---|---|---|
| 1 | the judge abstained | `keyword`, reason the judge's error |
| 2 | the query is exactly a name, or a name's prefix and name p >= `find_name_min` | `name` (a name wins over strong) |
| 3 | no strong fit, at most three words, the name table matches | `name` (a typed prefix) |
| 4 | no strong fit, name p >= `find_name_min`, no name matched, nothing kept | `none`, reason `gap` |
| 5 | the platform Choice is `none` with confidence >= `find_gap_min` | top under 0.6: `none`, `gap`; else `closest`, never strong |
| 6 | a fit at or over high | `strong` |
| 7 | a fit at or over keep | `closest` |
| 8 | nothing kept, the platform Choice names a platform with confidence >= `find_gap_min` | `none`, reason `gap`: the catalog has the platform, not this job on it (posting to Threads where only reading it is listed) |
| 9 | otherwise | `none`, reason `not_task` (`decide`'s `not_task` names the verdict: an agent's search asks for `keyword`, [search-experiment](search-experiment.md)) |

Rule 5 sits before the strong rule: a confident "no platform provides this" caps the answer. On a
shelf (`?platform=`) the platform Choice is not asked and any `none` is reason `scope`: that find read
one shelf, so it cannot say the catalog lacks anything, and its SearchMiss row says `scope`.

**Rows** (`expand_groups`, one group per kept unit; `expand` flattens them). Units best first. A job at or over high lists every vendor, in the evidence
rerank's order (`store.rerank`: measured success, core, price), each row carrying the job's fit and
`fit_from: job`; the pages show a job as one line with its vendor count, so no vendor is cut. A job
between keep and high is folded by provider: one row for each of its first five providers, and
`children_hidden` on its first row counts the job's other providers not on the page (so "4 of 30
providers" adds up to the vendor count the judge was shown). An endpoint unit at or over keep is its own row with its own fit, `fit_from: endpoint`; a
member judged on its own keeps its own fit inside its job, and is left out when that fit is under
keep. A name's page (`name_page`) is the named platform's endpoints and those the name also
matches (twelve platforms, forty rows each), a provider's, or every endpoint carrying the product
name by platform, jobs first, unjudged. The evidence (measured success per endpoint) is read by the
route's `EndpointObservationReader` only for a strong or closest answer, after the judge.

**Events.** The first `candidates` event is the lexical recall, sent before the query is embedded,
so it stays immediate; when the query's vector changes what is read, a second `candidates` event
carries the fused units (the pages just replace the list), and `judged.read` counts those. It gains
`units: [{kind, id}]` (its `candidates` list is every endpoint the units reach, so the pages light
the right platforms and vendors); `judged` gains `reason` (on
`none`), `platform: {choice, confidence}`, `engine: "v2"`, and rows gain `fit_from` and
`children_hidden`. `named` is `platform`, `provider` or `product`.

## What is recorded

`SearchLog` (mode `find`, source `web-find`, no identity) with `engine`; v2 also writes
`verdict` (the reason after a colon: `none:gap`), `platform_choice`, `platform_conf`, `name_p`,
`recall_ms`, and `units` as `[kind, id, p]`;
`baseline_ids` is every endpoint the units reach, `judged` the kept units. `embed_ms` and
`embed_error` record the query's vector (`off` without a key, `not_ready` while the card vectors
build, else the client's reason). The `judged` event carries the same as `embed: {ms, error}`. `SearchMiss` gains `reason` (`gap`,
`not_task`, `judge_off` for an empty keyword fallback, `scope` for a shelf's `none`) and `engine`.
Only the served engine files a miss: in `shadow` v1 does, and v2's empty answers show in its
SearchLog row only, so a find never counts twice in the misses. Migrations 0055 and 0056. Fire-and-forget through
`audit`, like every row there.

## The pages

`state/find.js` keeps `reason` from the answer and sends `engine`, `reason` and
`platform_choice` with `search_answered`. A job group's fit says where it came from
(`findFitTitle`), its vendor count says "4 of 30" when the server folded the rest
(`findProvidersText`), and `/search`, which lays vendors out as cards, adds a line for each folded
job with a way to the whole list on its shelf. An empty answer says which kind it is: a gap ("treg
does not have this kind of data or action yet"), with a request link, or not a job ("try describing
the data you want"), without one. On a shelf an empty answer is one line, "Nothing in <platform> for …", with
"Search all tools" (`findEverywhere`): the Catalog page, its box holding the same words, asked
unscoped. A shelf never offers a request, since it cannot know the tool is missing.

The Catalog box asks on its own when typing pauses on two characters or more; that answer sits
above the still-filtered shelves, so a name being typed both filters and is answered, and it shrinks
to one line when it reads or finds nothing. Enter still asks for the full answer (see
[dashboard](../interface/dashboard.md)).

## Measuring it

`scripts/find_bench.py` scores an engine against labeled queries: a gold regex over unit and
capability ids, the acceptable verdicts, and for a name what it must name. The `recall` tier calls
nothing (is a gold unit among the candidates; how many of the gold job's vendors the candidates
reach); the `judge` tier runs the whole answer and reports verdict accuracy by stratum,
false-strong, false-none, top-1 and MRR, tokens, latency and **job coverage** - of a gold job with
two or more vendors, how many the page shows (micro, macro, fully covered). Coverage is the first
number: it measures what a person gets. Judge answers are cached on disk by (model, query, unit
ids, questions) with the latency and tokens they cost live, and query vectors beside them, `--baseline` diffs two runs case by
case, `--engine v2` builds the card vectors first when an embedding key is set (cached under
`<cache>/find-vectors/`), and `--engine logged` scores what a JSONL case's find log recorded. CI runs the recall tier on
the synthetic `tests/fixtures/find_bench.yaml`; a label that no longer matches the catalog stops the
run. The labeled real queries live outside this repository, and a score on the set the rules were
settled on is optimistic.

## Not here

- No generated card expansions, no vectors in the repository, no model loaded in the process.
- No second model request per find, and no probability shown to agents.
