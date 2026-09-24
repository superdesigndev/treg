---
title: Discovery experiment — a relevance judge behind catalog search, measured on what the caller does next
status: building
sources:
  - src/treg/application/search_experiment.py
  - src/treg/domain/catalog/interleave.py
  - src/treg/infra/judge.py
  - src/treg/alembic/versions/0041_searchlog.py
  - scripts/search_experiment_report.sql
  - tests/test_search_experiment.py
  - src/treg/application/catalog_find.py
  - tests/test_catalog_find.py
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
  caller serves the baseline. Answers are cached in-process by (model, query, candidate ids).
- **`application/search_experiment.py`** — the use case. Judges the candidates, builds the judged
  page with the SAME finishing steps the baseline had (evidence rerank, routed grouping, cut to the
  page — the MCP layer passes that function in), deals the caller an arm, decides what is shown, and
  hands the MCP layer a row for `audit.record_search`.

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

Arms are dealt per **caller** — a salted hash of the bearer token — so one agent's session is
consistent and its re-queries mean something; `search_experiment_salt` re-deals. An unconfigured
judge (`typesafe_api_key` empty) makes every mode behave as `off`.

**Interleaving** (`domain/catalog/interleave.py`) is team draft: per round a coin decides who picks
first, each side adds its highest-ranked row not yet on the page. Credit at analysis time goes only
where the pages **disagree** — a row only one page carried, or one they ranked differently (the
higher rank wins); a row both carried at the same rank is a tie and evidence for nobody. Queries on
which the two pages are identical therefore contribute nothing, which is right: the judge changed
nothing there. The share of queries where the pages differ is the first number the shadow week
reports, and a low share means little to win as much as it means slow convergence.

## What is recorded

`SearchLog` (migration 0041) — one row per MCP search while the mode is not `off`: query, source,
caller's team and email, mode, arm, the baseline page, the judged page with probabilities, the page
served with each row's owner, the lexical match count (`baseline_total`, 0 = the gate admitted
nothing — the recall stratum), whether the pages differ, and the judge's latency, tokens and error.
Written fire-and-forget through `audit.record_search`; a dropped row costs one sample.

Unlike `SearchMiss`, this row carries identity: the outcome is the caller's later `call`
(`CallRecord.org_id` + `user_email`), and an anonymous row has no outcome to join. The HTTP search
route is therefore **not** in the experiment — it is open and anonymous. `SearchMiss` is still
written whenever the **lexical** page is empty, whatever the judged page found: that log measures
the shipped ranker's coverage and feeds `aliases.yaml`, and a judged hit is the experiment's result,
not a reason to stop recording the gap.

The same facts go to PostHog as `catalog_search_judged` (distinct id = caller key) for dashboards.
That pipe is lossy by design (`analytics.py`); the numbers that decide the experiment are read from
the database.

## Reading it

`scripts/search_experiment_report.sql` (Postgres, read-only) joins `searchlog` to `callrecord` by
team + email within ten minutes of the search, on endpoints that were on the served page:

1. volume and health per arm — differs share, empty-baseline share, judge error rate, p50/p95 judge
   latency, tokens;
2. conversion per arm, **stratified** by empty vs non-empty baseline (recall gain and ranking gain
   are different claims);
3. interleaving credit with the disagreement rule and a binomial z;
4. re-query rate — a second search within two minutes and no call in between is a page that did
   not do its job.

## Served to people: find tools for a job

`GET /catalog/find?q=` (`application/catalog_find.py`) is the same mechanism with a person on the
other end: `store.candidates` recall, one `infra.judge` request, the same `search_judge_keep` /
`search_judge_high` cuts. It backs the dashboard's Catalog search box (Enter on a described job) and
the public `/search` page (see `interface/dashboard.md`). It differs from the experiment where the
audience differs:

- **Wider recall, looser timeout.** `find_candidates` (60) and `find_timeout_s` (6 s). The judge
  scores a request's candidates in parallel, so 60 measured the same wall time as 30, and it lets
  rows the lexical order ranks low reach the judge ("why is my blog losing google traffic" found
  the Search Console performance report only at 60).
- **Streamed.** Two NDJSON events: `candidates` as soon as the recall is computed, `judged` when the
  judge answers. The pages animate the gap on the first event.
- **A verdict, not a page.** `strong` (a row at or over `high`), `closest` (kept rows, none strong),
  `none` (nothing kept), or `keyword` when the judge abstained and the rows are the lexical page,
  unjudged. Kept rows are best fit first (no `interleave.bucketed` lexical order inside a bucket),
  each with its fit and the catalog's own price shape; the event carries `high` so the pages draw
  the strong cut from the server's setting. The probability is shown to people; agents still never
  see it.
- **Open and rate limited.** No identity is needed, so `admit` bounds use per IP and per deployment
  (`find_max_per_ip_hour`, `find_max_per_hour`) through `ratestore`, in a session that is committed
  and closed before the judge is called.
- **Logged in the same tables.** One `SearchLog` row with `mode=find`, `source=web-find` and no
  identity (so no outcome join yet), and a `SearchMiss` when nothing fit.

Agents are unaffected: `/catalog/search` and MCP `catalog_search` answer exactly as before.

## Guardrails and what is deliberately not here

- The judge can add at most `typesafe_timeout_s` (2.5 s) to a search and can never fail one. Live
  answers at 30 candidates measured 1.2-1.5 s and about 4.5k input tokens; the shadow week reads
  the real distribution from `judge_ms`. Shadow mode still awaits the judge before answering, so
  its latency cost is the same as the live arms' — a non-blocking shadow is a possible follow-up.
- The agent-facing response does not carry the judge's probability. Exposing it would change how
  agents pick and turn the experiment into a different one.
- Page length is the same in every arm, so "more options" cannot masquerade as "better options".
- Nothing here touches `/call/`, money, or the HTTP search route (`/catalog/find` is its own
  route). The routed-discovery switch
  (`routed_discovery`) applies to the judged page through the shared finishing function.
- Not yet built: a read path for `SearchMiss` other than the report scripts, any use of the judge
  outside catalog discovery, and crediting a `/catalog/find` answer with what the person did next.
