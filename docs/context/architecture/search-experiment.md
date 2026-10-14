---
title: Discovery experiment — a relevance judge behind catalog search, measured on what the caller does next; the job-first answer served to agents
status: building
sources:
  - src/treg/application/catalog_search.py
  - src/treg/application/search_experiment.py
  - src/treg/domain/catalog/interleave.py
  - src/treg/infra/judge.py
  - src/treg/alembic/versions/0041_searchlog.py
  - src/treg/alembic/versions/0056_searchlog_verdict.py
  - scripts/search_experiment_report.sql
  - scripts/search_agent_bench.py
  - tests/test_search_experiment.py
  - tests/test_catalog_search.py
  - tests/test_search_agent_bench.py
related:
  - architecture/find.md
---

# Discovery experiment

`catalog_search` is token matching (see [catalog — search scoring](catalog.md#search-scoring--most-words-must-match-and-the-rare-ones-decide)).
It is reproducible and explainable, and the SearchMiss log shows where that is not enough: a
task-phrased query whose words are parameter values ("apple stock closing prices for last year")
admits nothing, because six rare words must mostly match and three of them name a ticker and a
date range that no catalog row will ever contain; and a query whose words happen to occur in an
unrelated row admits the wrong thing. Both are semantic failures. This experiment puts a relevance
judge behind a widened recall and asks whether the page it produces is better — measured on
behaviour, because there are no labels.

## The mechanism

Three layers, imports pointing inward:

- **`domain/catalog/store.candidates`** — recall for the judge: every concrete endpoint that hits at
  least one required token, lexical score order, cut at `search_experiment_candidates` (30). It is
  safe to be this loose only because nothing here is shown without the judge's answer. Routed
  parents are left out and put back by `with_routed_parents` (shared with `search`), so the judged
  page steers to `treg.<capability>` exactly as the baseline does.
- **`infra/judge.py`** — TypeSafe's System One API (Jev). One request carries the query and every
  candidate as `state`, and one Noul question per candidate; the answer is a probability per row.
  It never raises: timeout, non-200, malformed body all return `probs=None` with a reason, and the
  caller serves the baseline. Answers are cached in-process by (model, query, candidate ids, and
  any criteria or extra questions). A caller may attach Noul `criteria` to every candidate question
  and add `extra` questions about the same state, Noul (a probability) or Choice (the option, its
  confidence and every option's probability); a candidate in `job_view`'s shape is asked about a
  whole job, under `job_criteria` ([find](find.md)). The experiment passes none of these, so its
  question and its cache key are unchanged while it runs.
- **`application/catalog_search.py`** — the search use case behind MCP `catalog_search` on both
  surfaces: the shipped ranker's page (`store.rank_band` over a band wider than the page, the
  evidence rerank, listed hub tools merged by score, routed groups, cut to the page), then the
  experiment's say over it, then the records (`audit.record_search` while the experiment is on,
  `record_search_miss` whenever the LEXICAL page is empty). The hub read holds its own session and
  closes it before any judge request. The MCP layer resolves who is asking and shapes the rows.
- **`application/search_experiment.py`** — the experiment. Judges the candidates, builds the judged
  page with the SAME finishing steps the baseline had (evidence rerank, routed grouping, cut to the
  page — the use case passes that function in), deals the caller an arm, decides what is shown, and
  hands back a row for `audit.record_search`.

The judged page is **bucketed, not sorted by probability**: rows under `search_judge_keep` (0.4)
are dropped, rows at or over `search_judge_high` (0.7) go first, and inside a bucket the lexical
order is kept — so rows that tie lexically still tie exactly and `store.rerank`'s evidence buckets
keep their say. The lift that separates the buckets through the score-first rerank is stripped
before anything reaches the caller.

## Modes and arms

One setting, `search_experiment`, is also the kill switch:

| mode | who sees what | why |
|---|---|---|
| `off` (default) | the shipped ranker, unchanged; nothing here runs | |
| `shadow` | the baseline; both pages are computed and logged | how often the pages differ, judge latency and cost in production, and a counterfactual read against later calls — before any caller sees a changed page |
| `interleave` | most callers: a team-draft merge of both pages; two holdouts (`search_experiment_holdout_percent` each): a pure baseline page and a pure judged page | the merge gives the paired preference cheaply; the pure arms give the absolute conversion and the latency cost |
| `v2` | most callers: the job-first answer (below); the same two holdouts: the pure baseline page and the pure v1 judged page | v2 is read against what it replaces and against the shipped ranker; a holdout caller on the baseline page still has v2 computed and recorded, so a `none` v2 would have given is read against what that caller did with the lexical page |

Arms are dealt per **caller**: by team and sign-in email where the search resolved them
(`identity_key`), else by a salted hash of the bearer token (`caller_key`). An OAuth grant's token
rotates hourly; dealt by token, that caller would change arms with it. `search_experiment_salt`
re-deals. An unconfigured judge (`typesafe_api_key` empty) makes every mode behave as `off`.

**Interleaving** (`domain/catalog/interleave.py`) is team draft: per round a coin decides who picks
first, each side adds its highest-ranked row not yet on the page. Credit at analysis time goes only
where the pages **disagree** — a row only one page carried, or one they ranked differently (the
higher rank wins); a row both carried at the same rank is a tie and evidence for nobody. Queries on
which the two pages are identical therefore contribute nothing, which is right: the judge changed
nothing there. The share of queries where the pages differ is the first number the shadow week
reports, and a low share means little to win as much as it means slow convergence.

## The job-first answer, served to agents (`v2`)

The engine behind `/catalog/find` ([find](find.md): recall by job and by meaning, one judge request
with the platform Choice, nine rules, every vendor of a fitting job) answers an agent's search in
`v2` mode. `application/catalog_search.py` runs `catalog_find.recall_with_meaning` and `answer_v2`
under the agent's budget (`typesafe_timeout_s` for the judge, `find_embed_timeout_s` for the
query's vector), then lays the answer out for a page (`agent_page`):

- **Rows by job, dealt round-robin.** The answer's groups (`expand_groups`: one per kept unit, a
  job's vendors in evidence order; a name page's rows by job) are the page's groups. A judged
  answer keeps its best `page_groups` jobs, one per two rows (four on a page of eight: measured on
  agents' searches, fewer jobs per page lifts the chance the row an agent calls is on it, more
  lifts the chance its job is, and one per two rows holds both near their best); the page takes
  the first row of every group, then the second, until `limit`, and lays them out job by job,
  contiguous. Inside a job, members the judge rated on their own at or over `search_judge_high`
  lead (the vendor whose own words matched the query's qualifier, which the evidence order would
  bury past the cut), then the evidence order. A routed parent (`store.routed_parent`) leads its
  job's group when routed discovery is on (strong and closest answers only; a name page is what
  the name offers). A listed hub tool whose capability is a kept job joins that group with no
  lexical gate; none joins an empty page.
- **What a row says.** `job` (its capability) and, on a job's first row, `more_providers`: vendors
  of that job the page left out (`expand`'s fold plus the cut; the same `hidden` field a lexical
  routed group counts cut child rows in). `score` is null: no probability reaches an agent. `jobs`
  lists the page's jobs with their vendor count and rows shown.
- **The verdicts an agent sees.** `strong`, `closest` and `name` as find gives them, with a `hint`
  that says only what to do next (the facts are on `jobs` and the rows): for a fitting job,
  `catalog_get` on its routed row where it has one, else on its first row, which lists the others
  doing the job. A `none` for a catalog **gap** is served as an empty page with `reason: gap` and
  a hint to file `catalog_request`, with no near misses (whose advice, drop a word and retry, is
  the opposite): that is the answer that stops an agent re-querying. Where the judge named the
  platform the task needs, the hint names it: the catalog has Threads, not posting to it. "Not a
  task" is not served as a `none`: an agent's input always means something and that rule was
  settled on people's queries, so `decide` is asked for `keyword` there (`not_task`), and the
  lexical page is served under it (recorded `keyword:not_task`). An abstaining judge (`keyword:<its error>`), a failed
  answer, or a caller past the per-caller cap also serve the lexical page as `keyword`.
- **The per-caller cap.** A search is not metered, so `search_judge_max_per_caller_hour` (a
  `ratestore` window under `catalog_search`) is the only bound on what an agent in a loop can
  spend on the judge. It guards every dealt caller in `v2` mode before any judge, keyed by who
  the caller is (team and email); a caller the search could not resolve shares one bucket, since
  the tool reads the catalog without validating a per-team token and a made-up token would
  otherwise be a fresh cap each search. Past it the hour's searches are the lexical page,
  recorded `keyword:rate_limited`. The hub read and the cap each open and close their own
  session before the judge is called.

The HTTP route and the CLI still answer from the shipped ranker; they follow once the route's hub
read holds no session through a judge call and the CLI sends its token.

## What is recorded

`SearchLog` (migration 0041) — one row per MCP search while the mode is not `off`: query, source,
caller's team and email, mode, arm, the baseline page, the judged page with probabilities, the page
served with each row's owner, the lexical match count (`baseline_total`, 0 = the gate admitted
nothing — the recall stratum), whether the pages differ, and the judge's latency, tokens and error.
Every served row in `shown` carries its job as a third element (`[id, owner, job]`, in every
mode), so the report can credit a call to any vendor of a job the page showed. In `v2` mode the
arm is `v2`, `baseline` or `judged`; a v2 row (arm `v2`, and the `baseline` arm, for the
counterfactual) also carries find's v2 readings and `verdict` (0055, 0056: the reason after a
colon, `none:gap`, `keyword:not_task`), and `judged` is the kept units. Written fire-and-forget
through `audit.record_search`; a dropped row costs one sample.

Unlike `SearchMiss`, this row carries identity: the outcome is the caller's later `call`
(`CallRecord.org_id` + `user_email`), and an anonymous row has no outcome to join. The HTTP search
route is therefore **not** in the experiment — it is open and anonymous. In the v1 modes
`SearchMiss` is still written whenever the **lexical** page is empty, whatever the judged page
found: that log measures the shipped ranker's coverage and feeds `aliases.yaml`, and a judged hit
is the experiment's result, not a reason to stop recording the gap. A served v2 answer files a miss
when it is a gap (`reason: gap`, `engine: v2`), or when its keyword fallback is an empty lexical
page (`reason: not_task` where that is why); the lexical coverage stays readable in
`SearchLog.baseline_total`.

The same facts go to PostHog as `catalog_search_judged` (distinct id = caller key) for dashboards.
That pipe is lossy by design (`analytics.py`); the numbers that decide the experiment are read from
the database.

## Reading it

`scripts/search_experiment_report.sql` (Postgres, read-only; `-v mode=` picks the mode whose arms
it reads) joins `searchlog` to `callrecord` by team + email within ten minutes of the search, on
endpoints that were on the served page:

1. volume and health per arm — differs share, empty-baseline share, judge error rate, p50/p95 judge
   latency, tokens;
2. conversion per arm, **stratified** by empty vs non-empty baseline (recall gain and ranking gain
   are different claims); 2b, for `v2` mode, conversion by **job** per arm and verdict, where a call
   to any vendor of a job the page showed counts (a v2 hint sends the agent there, on the page or
   not), and the `baseline` arm's verdict is v2's counterfactual reading, so a `none` that still
   converted is a false none read directly; 2c, after a `none`, whether the caller called anything
   at all;
3. interleaving credit with the disagreement rule and a binomial z;
4. re-query rate per arm and verdict — a second search within two minutes and no call in between is
   a page that did not do its job; an answer that tells the agent to stop lowers it whether or not
   it was right, so it is read with 2c.

Volume is a few heavy callers' to a large degree, and no block clusters its error by caller; a
small difference between arms is read with that in mind.

Before a rule change reaches agents, `scripts/search_agent_bench.py` scores the v2 answer offline
against searches agents made and what they called next (a JSONL extracted from this log, kept
outside the repository): per labeled case whether the called endpoint does a job the v2 page
shows (**job-hit**, the main number: a v2 page lists a job's vendors by measured success and sends
the agent to the rest), hit@limit, and **false-none**, a gap answered where the caller did call
something, by arm; plus the verdict distribution, tokens and latency over every case. The label
can only name what the caller was shown, so the lexical and v1 judged pages it compares against
are favoured and v2's numbers are a lower bound.

## Served to people: find tools for a job

`GET /catalog/find` is this mechanism with a person on the other end: the dashboard's Catalog box
and the public `/search` page, anonymous, rate limited, streamed, and switchable to a recall by job
(`find_engine`). See [find](find.md). Its v2 engine is what `v2` mode serves to agents (above);
`/catalog/search` answers from the shipped ranker.

## Guardrails and what is deliberately not here

- The judge can add at most `typesafe_timeout_s` (2.5 s) to a search and can never fail one. Live
  answers at 30 candidates measured 1.2-1.5 s and about 4.5k input tokens; the shadow week reads
  the real distribution from `judge_ms`. Shadow mode still awaits the judge before answering, so
  its latency cost is the same as the live arms' — a non-blocking shadow is a possible follow-up.
- The agent-facing response does not carry the judge's probability, in any mode. Exposing it would
  change how agents pick and turn the experiment into a different one. A v2 answer's verdict is a
  word, and its hint says what to do with it.
- Page length is the same in every arm, so "more options" cannot masquerade as "better options".
- A recently-added search (`added_within_days` or `sort=newest`, catalog.md "`added`") is not part
  of the experiment: `added_page` serves it with no judge and no record. Without those inputs
  `catalog_search` runs the experiment exactly as before.
- Nothing here touches `/call/`, money, or the HTTP search route (`/catalog/find` is its own
  route). The routed-discovery switch
  (`routed_discovery`) applies to the judged page through the shared finishing function.
- Not yet built: a read path for `SearchMiss` other than the report scripts, any use of the judge
  outside catalog discovery, and crediting a `/catalog/find` answer with what the person did next.
