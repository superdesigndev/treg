---
title: Feedback - private intake for problems and suggestions
status: shipped
sources:
  - src/treg/feedback_contract.py
  - cli/internal/commands/mark.go
  - src/treg/domain/feedback/__init__.py
  - src/treg/domain/feedback/reports.py
  - src/treg/domain/feedback/reviews.py
  - src/treg/domain/feedback/marks.py
  - src/treg/domain/feedback/verdicts.py
  - src/treg/domain/feedback/review_codes.py
  - src/treg/application/review_judge.py
  - tests/test_review_judge.py
  - src/treg/domain/feedback/body_view.py
  - src/treg/application/review_body_pilot.py
  - tests/test_review_body_pilot.py
  - src/treg/application/review_body_audit.py
  - src/treg/application/review_answers.py
  - tests/test_review_answers.py
  - src/treg/alembic/versions/0128_review_answer_basis.py
  - tests/test_review_answer_basis_migration.py
  - tests/test_review_body_audit.py
  - src/treg/domain/feedback/comparisons.py
  - src/treg/application/compare.py
  - src/treg/application/compare_judge.py
  - src/treg/domain/feedback/compare_rubric.py
  - src/treg/hints.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - src/treg/application/call/invite.py
  - src/treg/application/review_targets.py
  - src/treg/infra/kv.py
  - src/treg/application/feedback.py
  - src/treg/routers/feedback.py
  - src/treg/alembic/versions/0025_feedback.py
  - src/treg/alembic/versions/0026_callreview.py
  - src/treg/alembic/versions/0030_feedback_handling.py
  - src/treg/alembic/versions/0137_compareattemptday_spent.py
  - src/treg/alembic/versions/0139_callcomparison_jev.py
  - src/treg/alembic/versions/0105_callmark.py
  - src/treg/alembic/versions/0115_callcomparison.py
  - tests/test_feedback_handling_schema.py
  - src/treg/web/feedback.md
  - tests/test_feedback.py
  - tests/test_reviews.py
  - tests/test_marks.py
  - tests/test_endpoint_verdicts.py
  - tests/test_hints.py
  - tests/test_kv.py
  - tests/test_compare.py
  - tests/test_compare_judge.py
  - tests/test_compare_spend_migration.py
  - tests/test_callcomparison_jev_migration.py
related:
  - architecture/data-model.md
  - architecture/mcp-oauth.md
  - architecture/super-admin.md
  - interface/skill.md
  - interface/cli.md
---

# Feedback

`FeedbackIn` takes `category` (`FeedbackCategory`), `message`, optional `call_ids` and optional
public `endpoint_id`, and rejects extra fields. Free text is not sanitized: the privacy guidance asks
callers to replace sensitive values and omit raw payloads. Guidance invites reports of small
friction even after a workaround, asks for references in `call_ids` (CLI `--call-id`), and tells the
agent to continue its task after reporting once.

`POST /feedback` (`require_member`, public-demo write restriction applies) commits the report and the
per-team rate-limit hit in one transaction before acknowledging 201. It spends no balance, calls no
provider and never goes through the best-effort audit writer, so a storage failure cannot produce a
success acknowledgement.

`call_ids` and `endpoint_id` stay claims. `verified_call_ids` is the subset found in the submitting
team's `CallRecord.call_ref`, `LedgerEntry.call_id` or a still-queued `CallCharge.call_id`; no
cross-team lookup runs. A pinned caller verifies only against rows carrying its own pin (on the
ledger the `reserve` entry, or an allowance call's `settle` entry or queued charge, carries
`meta.tags`), and its report is stamped with that pin (`Feedback.tags`). Missing audit records never
reject a report, and verified provenance does not make the problem true. Intake never ranks providers
or adjusts charges.

`GET /feedback/{id}` is team-scoped (404 otherwise), narrowed by pin like `/calls`.
`GET /admin/feedback` is superadmin-only on the admin pool. There is no external notification, issue
sync or public feed. `Feedback` is in `ORG_SCOPED_MODELS`.

The team `/mcp/` exposes `feedback` (relays to the same HTTP intake). `/mcp/v2/` does not: its tools
match the Claude directory submission. `skill.md` links `{BASE}/feedback.md`, the one document with
syntax and privacy guidance; CLI help and MCP share `FEEDBACK_DESCRIPTION`. Self-hosted submissions
stay on the configured registry.

## Internal handling storage

`FeedbackHandling` (one row per report; absent means open, unassigned, version zero) and
`FeedbackHandlingEvent` (append-only history, unique `(feedback_id, version)`) hold internal
processing apart from the original `Feedback`. Both cascade from `feedback.id`, so deleting a report
or its team removes its handling history.

This repository owns only the models and migration. The private admin's handling store is the only
runtime writer and owns the state/history transaction, optimistic version checks and DB grants;
public intake and the call runtime never read or write these tables. Deploy the migration before the
admin service's grants and release; downgrading revision 0030 deletes all handling history.

## Call reviews

`CallReview` stores one rating per `call_id`; reviews never create feedback reports.
`application.feedback.submit_review` looks up the reference only in the caller's team audit records,
narrowed to its pin (a foreign reference is 404, never an oracle). A missing audit record is a
retryable 404, including ledger-only evidence, which lacks the attribution a review needs. Own-tool
calls are 400, and so is a maker reviewing its own listed hub tool. A routed parent is attributed to
its latest successful attempt's endpoint/provider (`<call>:r<n>`; an async attempt's polls,
`<call>:r<n>:p<k>` on the provider's job endpoint, are left out, since the review is about what was
asked), keeping the parent as `routed_via`. `invited` is recomputed
at submission from a 2xx, non-cached, `platform`-tier record and the current sampling rate, so it
means "sampled, hence eligible"; the true count of invitations sent is the `hint_attached` event.
Volunteered reviews are accepted with `invited=false` and count through the team vote below. Retries
return the original id with `already_reviewed`; a unique index arbitrates races, and the savepoint
stays open until the application commit (SQLite deferred BEGIN). The sole writer is
`domain.feedback.reviews`. `GET /admin/reviews` is superadmin-only and excluded from OpenAPI; there
is no team read route.

## Call marks

A review is one opinion per call, often given before the answer is used. A mark is evidence: what
the caller found wrong, or confirmed right, after checking the answer. `POST /marks` takes
`call_id`, `basis` (`bounced`, `cross_checked`, `user_said`, `impression`), `items` (each a
`verdict` from `MARK_VERDICTS`, with an optional 0-based row `index` and `field`; no index means
the whole answer), optional `checked`, `evidence_call_id`, a private `note` and `replaces`.

- **Append-only.** A team may mark one call many times as it learns more. A correction or
  withdrawal (empty `items`) is a new row naming the earlier mark it `replaces`, which must be the
  same team's, on the same call. An identical retry returns the original id with `already_received`,
  but only after the call is found in the caller's scope, and the hash includes the caller's pin, so
  a receipt never discloses another pin's call or mark; a unique `(org_id, content_hash)` index
  arbitrates races.
- **Attribution.** The call is looked up exactly as for a review (team and pin scope, retryable 404,
  own tools and a maker's own hub tool refused). The mark keeps the called endpoint, provider and
  credential tier. A routed answer can merge several children's rows, so a row index is the
  caller's view of the merged answer and is never credited to a child here.
- **What it can claim.** `checked` is how many rows the caller looked at, not a denominator: rows
  it did not mark carry no conclusion. Only rows marked with a verdict, `correct` included, do.
  `evidence_call_id` is checked against the team's own audit (`evidence_verified`) and never
  rejects a mark. `basis` is the caller's account of how it knows.
- **Surfaces.** The binary CLI's `treg mark` (the frozen Python CLI has none; its users POST
  `/marks`), the team `/mcp/` tool `mark` (relaying to the same intake; hidden from OpenAI's clients
  with `review`, and absent from `/mcp/v2/`), `skill.md` and `{BASE}/feedback.md` ("Mark").
- **Rows on a review.** `POST /reviews` takes optional `rows` (mark items) and `basis` (default
  `impression`). A new review with rows writes one mark in the same transaction with `review_id`
  set, the review's attribution and no `replaces`; the response carries `mark_id`. Rows spend the
  team's mark allowance (`MARK_RATE_MAX`), so a review with rows over it is a 429 that saves
  nothing. A repeated review keeps the first, records no rows and returns the first one's
  `mark_id` (`marks.of_review`, filtered by the caller's pin like any mark read), so a lost
  response can still be corrected through `replaces`. The review's verdict ignores its rows. The team
  `/mcp/` `review` tool takes `rows`; its description is `REVIEW_ROWS_DESCRIPTION`, while the
  frozen Python CLI keeps `REVIEW_DESCRIPTION` and sends none.
- **A review riding on the next call.** The team `/mcp/` `call` tool takes an optional `prior`
  (`PriorReviewIn`: the previous call's `call_id`, `usefulness`, `reason`, `rows`) and posts it to
  `/reviews` with `client = "mcp-prior"` as a background task (`_PRIOR_TASKS`, given up after
  `PRIOR_TIMEOUT_S`), so it never fails or holds the call. `prior` accepts any value and is parsed
  inside the tool, so a malformed one (a string, a list, a bad field) is dropped instead of failing
  the call's argument check. An
  OAuth grant sees and sends it only with the `review` tool's scope (`treg:catalog`). `_PriorReviewGate` strips `prior` from `tools/list`
  for every credential outside `mcp_prior_review_rate` (a deterministic share of bearer tokens,
  never an OpenAI client); a `prior` sent outside the share is ignored. The rate defaults to 0.
- **Private.** Marks are never published, quoted or folded into verdicts. `GET /admin/marks` is
  superadmin-only and excluded from OpenAPI. The rate limit is per team per hour, separate from
  feedback reports. The sole writer is `domain.feedback.marks`; `CallMark` is in `ORG_SCOPED_MODELS`.

## Review and feedback invitations

`application.call.invite.invitation(context, status, replayed, accepts)` runs in
`routers.call.call_tool` after the response is built and before it streams, and returns `compare`,
`review`, `mark`, `feedback` or nothing, in that order of precedence. `accepts` is what the client says it
shows (`X-Treg-Hints: review,mark,feedback`, parsed by `accepted_kinds`); a client that does not
send the header gets `LEGACY_KINDS` (review and feedback), since released CLIs drop any other kind
in silence. A kind outside `accepts` is skipped before its budget is spent, so the call falls
through to the next. The team `/mcp/` and the binary CLI send all four (`review,mark,feedback,compare`);
`/mcp/v2/` sends the first three. The router sends
`X-Treg-Hint: compare|review|mark|feedback` with its words in `X-Treg-Hint-Text` (`hints.header_text`, only
to a client that sent `X-Treg-Hints`, since the words name flags an older release lacks; plus
`X-Treg-Review: requested` on a review, read by CLI releases up to 0.18) and emits one
`hint_attached` event per invitation sent (`kind`, `call_id`, `client`, `endpoint_id`, which is
`treg.<capability>` for a routed parent; a review adds `budget`, `main` or `extra`, and
`credited_endpoint`, the endpoint its review would be credited to). Surfaces only translate
the header: the CLI prints a stderr line, the team `/mcp/` renders it into its single hint slot with
priority replay > 402 > compare > review > mark > feedback. `/mcp/v2/` and OpenAI's clients on `/mcp/` render none,
so for them `hint_attached` counts an invitation the agent never saw.

- **Review**: a direct catalog call on treg's platform key, or a routed parent (credited to the
  child that answered, as an uninvited routed review always was), 2xx, not replayed, not archive-served,
  sampled by `hints.sampled("review", call_ref)`, and within the per-team budget
  `kv.store().take("review-budget:<org_id>", review_budget_per_hour, 3600)`. Per-call sampling scales
  with volume, so the budget caps how often a busy team is asked. There is deliberately no
  per-endpoint cooldown. An own tool never qualifies, even if its name matches a catalog endpoint.
  A caller that names no runtime (`X-Treg-Client`) and sends no `X-Treg-Hints` is a script or
  backend with no agent reading the header: it is never asked (`invite.UNNAMED_KINDS`) and spends
  no budget, which stays for the team's agents' calls; it still gets the feedback hint. `invited`
  on a review does not recompute this, so such a review, rare, can still read as invited.
  With `review_target_reviews` > 0 the draw is targeted (`application/review_targets.py`): the
  endpoint a review would be credited to (a direct call's own, or a routed parent's latest
  successful child, which the router records as `route_summary["credited"]`; not `served_by`,
  since with `X-Treg-Route-Confirm` a later provider confirms the returned answer)
  is looked up in a per-worker snapshot of reviews per endpoint over `review_target_window_days`,
  each team counted at most `review_target_team_cap` times, refreshed every 10 minutes and read
  once at startup, each read bounded to 2 s (a slow read keeps the last snapshot, or the base
  rate before the first, and never holds a worker from serving). Below the
  target the rate climbs linearly from `review_sample_rate` toward `review_target_rate` (an
  endpoint with none); at the target it is the base rate. It is the same deterministic draw
  (`hints.sampled_at`), and the team budget still caps how many invitations a team receives, so
  targeting moves invitations toward short endpoints rather than adding work per team. The same
  snapshot keeps each team's own count per endpoint: a team already holding
  `review_target_team_cap` reviews of an endpoint is not invited there again (rate 0) until older
  ones leave the window, since the ranking counts no more per team, so its budget goes to
  endpoints where its review still counts; a team holding none is invited at least at
  `review_target_new_team_rate` (0 = no floor), because a ranking needs many teams per endpoint.
  Both act on the snapshot, so a team can receive a few more invitations in the minutes before
  the next refresh. A busy team spends its budget within minutes of each hour on whatever it
  calls most, which leaves short endpoints uninvited however high their rate. So once
  `review-budget:<org_id>` is spent, a sampled call whose rate is boosted above the base
  (`review_targets.boosted`: its endpoint is short of the target, or the team is new to it at a
  floor above the base) may draw on a second budget, `review-target-budget:<org_id>` with
  `review_target_budget_per_hour` (0 = off). It only adds invitations, never moves the first
  budget's. Such a call tries both budgets in one store operation (`kv.take_first`, one Lua
  script under the one 200 ms optional deadline), so a slow store delays its response once. `invited`
  on a review recomputes with the snapshot of its moment, so near an endpoint's target it can
  disagree with the invitation sent; `hint_attached` is the exact record.
- **Mark**: a successful, non-replayed, non-cached catalog call on any credential tier, a routed
  parent (`treg.<capability>`, which has no marketplace target) included, never an own tool, whose endpoint id equals or ends in `.<capability>` for one of `mark_hint_capabilities`
  (empty: every catalog call), sampled at `mark_hint_rate` and capped by
  `take("mark-hint-budget:<org_id>", mark_hint_per_hour, 3600)`. It asks for later work, once the
  agent knows. Its text travels in `X-Treg-Hint-Text`: one of `mark_hint_texts` (A/B variants,
  `{call_id}` filled, printable ASCII), chosen deterministically per call, or `hints.MARK_HINT`;
  `hint_attached` records the `variant`. The binary CLI prints the text and the team `/mcp/`
  renders it, so copy changes need no release; the frozen Python CLI ignores the kind.
- **Compare**: any successful, non-replayed, non-cached call by a team with a comparison waiting
  (the `compare-ready:<org_id>` flag the worker run sets; see "Pairwise comparisons"), from a client
  that lists `compare` in `X-Treg-Hints` and is not pinned to tags, at most
  `compare_budget_per_hour` a team per hour (`compare-hint-budget:<org_id>`); past that the call falls
  through to review. It precedes review. The team `/mcp/` and the binary CLI send `compare`;
  `/mcp/v2/` and the Python CLI never receive it. Both show the pair itself, since an agent judges
  answers in front of it and rarely fetches them: the team `/mcp/` puts it in the call result
  (`compare`, compacted by `compare.compact_pair`) with `hints.compare_inline_hint`, and the binary
  reads `GET /comparisons/next?compact=true` (the same compaction plus `how_to_vote`,
  `hints.compare_cli_hint`) within two seconds and prints it on stderr, or under `--json`
  as `_treg.compare`. A failed read prints `X-Treg-Hint-Text` (`hints.COMPARE_CLI_HINT`) alone.
  `treg compare` shows the pair and `treg compare ID 1|2|tie|both_bad --reason` records the verdict.
  The binary does not ask for compare when it could not show the pair or route the vote: under
  `--await` (it prints only the task's result) and when a `--header` sets `X-Treg-Token`,
  `X-Treg-Org` or `Authorization` (the pair and the vote would go to the CLI's own team). It then
  drops only `compare` from what the call declares, a `--header X-Treg-Hints` included. It reads
  the pair with the call's own client, and a one-off `--org` is kept in the vote's words, the
  fallback words included.
- **Feedback**: any other successful, non-replayed, non-cached call that samples in at
  `feedback_hint_rate`, own tools included. It asks for nothing, so it has no budget.

Before deciding, the router removes any `X-Treg-Hint`, `X-Treg-Hint-Text` or `X-Treg-Review` the
upstream sent (an own tool included): clients render that text as treg's, so it must be treg's.
The hook is best-effort: no database or body access, no change to the call service's exits or
writes; a fault can only lose the header. Sampling rates default to 0.

### The key-value store

`infra/kv.py` is Redis at `TREG_KV_URL`, or a bounded in-process dictionary when unset. Its tenants:

- Counters (`take`, one atomic `INCR` + `EXPIRE NX`; `take_first`, the same on each of several
  budgets in order until one has room, in one script) **fail closed**: a store that cannot answer is a
  spent budget, so an outage withholds review invitations rather than flooding a team; `hint_attached`
  dropping to zero is the signal. Counter calls have their own short outer deadline because the router
  awaits them before returning, and must not inherit the money lease budget.
- [Money admission](money.md#optional-admission-before-the-settlement-session) leases
  (`acquire_lease`, `renew_lease`, `release_lease`) report contention, lost ownership, unavailable and
  `deadline_expired` distinctly and fall back to database protection on store failure. Acquisition
  never retries an unavailable result; cleanup uses the original token even when the SET reply was not
  observed. The local store reports leases unavailable rather than claim cross-process exclusion.
  Lease failure diagnostics are bounded in-memory counts drained by the money diagnostic runner; they
  never retain exception messages, keys, owner tokens or store URLs.
- Org admission and quotas: [org-flow-control](org-flow-control.md),
  [org-endpoint-quota](org-endpoint-quota.md).
- Pairwise comparisons: a call's eligibility marker, a short text holding the caller's input
  (`set_text`, `get_text`), and the per-team ready flag (`set_flag`, `has_flag`, `clear_flag`).
  Both fail closed: an unread marker compares nothing, an unread flag invites nobody.
- Routed child health (`note_route_health`, `read_route_health`, and a `take` for the probe turn):
  best-effort time buckets that expire with their window; a fault loses a count or reads as no
  evidence ([catalog](catalog.md), "Child guards").

New tenants add narrow methods, never a generic get/set surface. `/admin/kv` reports `configured` and
`reachable`.

## Pairwise comparisons

A second kind of review: the team's agent sees two vendors' answers to one input it sent and says
which serves it better. It separates vendors a usefulness score cannot. Only the capabilities are
configured (`compare_capabilities`, ones an agent can judge by reading the answers): A is the vendor
that served the call, B is chosen per call among the capability's other vendors. Results are
internal; nothing here is published or changes routing.

**The marker, on the call path.** After a successful routed answer, `application.call.service`
calls `compare.mark_eligible(call_ref, capability, raw_headers, route_summary, archive_opted_out=...)`:
when the feature is configured (`compare_capabilities` listed, `compare_max_per_day` and
`compare_daily_usd` above 0), the capability is comparable (`compare.comparable()`: listed, with a
contract and at least two providers with a verified adapter), the request carried no `X-Treg-Route-*` header (a caller that chose vendors or
routing is left alone), the team keeps an archive and `hints.sampled_at("compare", call_ref,
compare_rate)` samples it in, it writes `compare-ok:v2:<call_ref>` in the shared store for two hours
(`MARKER_TTL_S`), inside a short timeout. The value is the contract input the caller sent
(`route_summary["identity"]`, the body minus a page cursor) as JSON, at most
`MARKER_INPUT_MAX_BYTES`; a larger input writes no marker. A page continuation (a `cursor` in
the summary) is never marked: B would be asked page one against A's later page. Nor is a merged
answer (`merged_from` in the summary, several vendors' rows delivered as one): no single vendor's
answer is A. The key carries the marker's version (`MARKER_VERSION`), so a run never reads a
marker an earlier build wrote under rules it no longer applies. A team opted out of the archive gets no
marker, so its input never sits in the store. No database, no change to the answer, nothing raised: a lost marker only means the call is
never compared. It is the only work on the call path.
Every decision is counted by reason and `X-Treg-Client` (`marked`, `not_sampled`, `route_header`,
`page_continuation`, `not_listed`, ...) and logged per process every `MARK_LOG_EVERY_S` as one
`compare markers {...}` line of counts; a run reports its `no_marker` calls by client too.

**The run.** `treg-worker compare collect` (`application.compare.collect`), meant for an hourly
cron. A listed capability short of two verified vendors is logged and skipped. Each run:

1. Removes the rows of teams that no longer exist, on every run, the feature on or off. Team
   deletion removes a team's comparisons itself (`ORG_SCOPED_MODELS`); this catches a row written
   while the team was being deleted, within one run.
2. Stops when off (no capability, `compare_max_per_day` or `compare_daily_usd` 0, or
   `compare_rate` 0), or when the UTC day's `compareattemptday` row has reached
   `compare_max_per_day` attempts or `compare_daily_usd` of spend (`spent_micro`).
3. Chooses eligible teams first: not opted out of the archive, not a demo, whose agent answered
   at least one review invitation in `compare_answer_days` on a surface that shows a compare
   invitation (`CallReview.invited`): the team MCP (client `mcp` or `mcp-prior`), or a client that
   declared `compare` in `X-Treg-Hints` on the review itself (`compare-answering:<org_id>`, the
   time of the latest such review, read against the current window in one `MGET` for all teams; a CLI reports only the agent it runs under, so an old and a new release
   look alike in `CallReview.client`). A team whose agents never answer one there, such as a
   pipeline calling without reading hints, or one on a CLI that does not show compare, would leave
   its pairs unjudged and take the day's budget. And holding no
   access policy at all (a member or project tool ACL, a member pinned to tags, a deny rule, or a
   capability pin on a compared capability). A team with any is skipped, so the run never has to
   emulate a team's authorization.
4. Reads those teams' recent routed parents per comparable capability: `endpoint_id =
   treg.<capability>`, `org_id` among the eligible teams and `created_at` within
   `compare_lookback_hours` (at most two, the marker's life), newest first, at most
   `SCAN_PER_CAPABILITY`, 2xx, `credential_tier` routed, and naming what the caller got
   (`served_call_ref` and `served_content_hash` set, filtered before the limit so calls that cannot
   be compared never fill it); drops calls already compared. Teams come first because other teams'
   calls can fill a newest-N window read over every team, leaving eligible teams' earlier marked
   calls unread until their markers expire.
5. Reads every candidate's marker in one `MGET`: it must exist (none, or a store that cannot say,
   skips it; the marker holds the sample, so the run does not sample again). For each marked call,
   before any archive body is read: the credited child, on treg's key, with its archive identities
   and a retention-allowed endpoint; its provider A one of the capability's verified vendors. The
   child is the one the parent names as what its caller got (`served_call_ref`), read for every
   marked call in one query, and nothing else: a missing or cached row skips the call
   (`served_child_missing`), never another attempt. A parent with no `served_call_ref` or
   `served_content_hash` is skipped (`a_delivery_unknown`): the call path leaves them empty when
   it could not check the answer (one over `route.SERVED_CHECK_MAX`, for one), and then the
   latest successful attempt may not be the vendor whose answer was delivered.
6. **Spreading the calls.** The run reads every team's comparisons of the comparable capabilities
   in `compare_window_days` that are not failed, counted per (capability, A, B)
   (`comparisons.tallies`), and takes the marked calls one at a time, until `compare_max_per_run`
   or `--max-seconds`, any error ending only that call: next is the call whose served vendor has
   been A least often on its capability (ties at random). A vendor that rarely serves is still
   compared as A, which the familiarity check needs, and no capability takes a run's calls only
   because its calls were read first. B (`_choose_b`): of the capability's other vendors not in
   `compare_exclude` and not failing (its last `FAIL_STREAK` answers as B in `FAIL_WINDOW_H` hours
   all failed, `comparisons.failure_streaks`, updated as the run goes; skipped as `b_failing` when
   no other is left), the least compared with A in either order across every team (ties at
   random) for which step 8 finds a target at most `compare_max_b_micro` and within what is left
   of the day's spend (read here so an unaffordable B gives way to a cheaper one; the claim checks
   again), and whose pair this team
   holds under `compare_team_cap` comparisons (either order) in `compare_window_days`, counting
   decided ones and pending or ready ones younger than three days. Each claim adds to the counts,
   so one run moves on to the next least compared pair.
7. The contract input is the marker's value, canonicalized (`canonical_identity`): exactly the
   question the caller asked, so nothing is read back from A's provider request. A marker without
   an input (an older one) is skipped. A's answer is only the bytes its caller got: the child's
   archived answer when its content hash is the parent's `served_content_hash`
   (`archive.answer_bytes`, which also checks the bytes read against that hash), otherwise, for an
   async provider, the terminal answer filed under the child's ref
   (`archive.load_terminal_snapshots`) when its bytes hash to it; anything else is skipped as
   `a_answer_not_delivered`. The answer is read through A's adapter (`route.child_outcome`); only
   a hit goes on. Skip reasons are a fixed set, never text read from an answer.
8. B's target: an endpoint the router itself would ask for this input (`plan.candidates_for`, so
   a routing adapter: a judge-only `route: false` adapter builds no request; then `build_plan`'s
   filter cap, declaration and scope checks), on a provider with a pasted-key credential shape that
   the router's per-provider sampling (`route_provider_sample_pct`, `plan.sampled_provider_allowed`)
   would ask for this team's question, not paused, platform-eligible with every platform
   credential its bindings name, not exhausted on treg's primary account, retention allowed,
   priced within the limit step 6 sets, with no second host or path parameter. The provider's
   endpoints are tried in the order `build_plan` ranks a treg-key child (`plan.rank` over the
   caller's keys, the filters each endpoint can express, its price and observed stats; the worker
   has no bound stats cache, so it reads the same day buckets through
   `PostgresEndpointObservationReader`, keeping the endpoints whose fold is ready when another's is
   behind, as the web cache does), after the router's short-window health (`route_health`,
   when `on`: a failing endpoint is never asked, as a comparison never takes a probe turn, and a
   slow one ranks after the healthy ones), so B
   answers the question the router would have asked it (one that ignores a sent filter is not
   asked ahead of one that sends it), and one dear endpoint does not hide a cheaper one that
   answers the same input. B's adapter builds the request (`to_upstream`) with the router's
   variant.
9. Right before sending, the team is read again: it still exists, is not suspended, has no active
   flow-control pause (`org_flow_control.confirmed_unpaused`: a fresh read; a read that fails, an
   invalid publication or a pause in force all skip, unlike admission, which allows on failure),
   and still has no access policy and no archive opt-out.
10. The claim, in one short transaction: one attempt and B's price on the current UTC day's
   `compareattemptday` row, read at the claim, so a run crossing midnight counts on the new day
   (one conditional `UPDATE ... SET attempts = attempts + 1, spent_micro = spent_micro + price
   WHERE attempts < limit AND spent_micro + price <= spend limit`, the day's first row inserted on
   conflict do nothing; at the call limit nothing is written and the run stops, over the spend
   nothing is written and the call is skipped as `day_spend_cap`), then the team and pair cap
   read again (on Postgres the day row's lock, taken by that update, orders concurrent
   claims), then the comparison row as `pending` (its unique `call_ref` stops a second run,
   concurrent or later). A crash after the claim leaves `pending`: it still counts and is never
   resent. The day's counts are no team's row, so deleting a team gives no quota and no spend back.
11. One request, through the relay alone (`infra.upstream.relay.relay`) on treg's primary key
    (`platform_bindings`): the request the router's first child call on treg's key sends for the
    team's question. The adapter's values go to the relay unformatted (a `false` reaches the vendor
    as `false`), the body as `route._child_body`, path parameters dropped as the call path drops
    them, with the call path's rewrites: treg's User-Agent, the provider's client header for the
    team (`tag_shared_client`) and its ceiling parameter at the router's default ceiling
    (`service.ceiling_query`). `body_read` is offered as the call path offers it (a key that goes in
    the JSON body is injected from it). No router, no fallback, no overflow, no hold, no ledger
    entry, no archive record, no capacity mark. Calls are `CALL_GAP_S` apart. The catalog price of
    the request is logged and stored as `b_cost_micro`.
12. B's answer is read through B's adapter as the router reads a child on treg's key
    (`route.child_outcome` with the canonical identity: a `question_miss`, such as no row on a
    `site:` the question names, is a miss), so both are contract `output`; the log line names the
    outcome and its reasons, never the body. The row is settled to
    `ready` only on a hit; anything else (an error, no answer, a miss) settles it `failed`, with no
    bodies, and nothing else is called. If the team was deleted meanwhile, the row is taken back.

**Spend and capacity.** A day's spend is at most `compare_daily_usd` of B's catalog prices, no one
request above `compare_max_b_micro`, and that holds only where the catalog price is the request's
maximum cost (a per-result or usage-settled price can charge more than the estimate). The direct
relay does not take the shared provider limiter, fair-share or rate window the router's children
take, and any vendor of a listed capability can be asked: list a capability only after checking
its vendors' prices and capacity, and put a vendor that must never be asked in `compare_exclude`.

**Storage** (`callcomparison`, written only by `domain/feedback/comparisons.py`, as is
`compareattemptday`; no foreign keys): one row per call (`call_ref` unique), `pending` until its
call is settled. A `ready` row holds both outputs and the input, blinded: identifier keys removed at
any depth (`_treg`, `next_cursor`, `cursor`, `provider`, `served_by`, `endpoint_id`, and `source`
when it names a vendor), and either vendor's id, endpoint id or API host inside text replaced by
`[vendor]`, the text kept. Each answer is cut to `compare_max_answer_bytes` and the input to 2 KB,
`truncated` saying which; `first` (which answer is shown as 1) is random. The team's
`compare-ready:<org_id>` flag is then set for three days in the shared store, so `TREG_KV_URL` must
be set for markers and invitations to reach across processes.

**Jev's blind verdict** (`application.compare_judge`, a pass of `treg-worker reviews judge`):
every comparison holding both answers (`ready`, or `decided` by an agent) on a capability in
`compare_judge_capabilities` gets Jev's verdict beside the agent's, in `jev_choice`,
`jev_orders`, `jev_version` and `jev_judged_at`; `choice` and `status` are never touched. Only
capabilities whose answers can be judged by reading them are listed: finding or verifying an
email, a phone or a person cannot be. Jev sees the capability, the input and the two answers,
never a vendor, under `compare_rubric.RUBRIC` (which better serves the input as written, by
content and never by length), each answer shortened to `compare_judge_answer_chars` with the marks
`body_view.shrink` leaves explained and `truncated` naming the parts cut when stored. Each pair is
asked twice, A shown first and then B, in two requests that go out together
(`compare_judge_batch` pairs each); it is written only when both answered. When the orders agree
that is the verdict (a, b, tie, both_bad); when they disagree it is `split`, an order effect kept
apart from a real tie, and `jev_orders` keeps both (A-first verdict, then B-first). Jev returns a
choice, not text, so there is no Jev reason. Decided pairs come first, so the agents' votes
calibrate Jev's from the first run; at most `compare_judge_per_run` pairs a run. A pair Jev could
not judge steps aside: an unreadable answer backs off from an hour to a week (`jev_attempts`,
`jev_retry_at`), a failed request waits an hour without counting a try. Each request gets only
the time left before the run's deadline; one the deadline cut short ends the pass
(`stopped_by_deadline`) and its pairs wait, uncounted. `tokens_in` counts every request that
answered, also when the other order failed. A new
`compare_rubric.VERSION` judges every pair again; a build with an older one never overwrites it.
`compare_judge` off skips the pass (its rollback switch; verdicts written stay).

**API**: `GET /comparisons/next` (team member) returns the team's oldest `ready` row younger than
three days as `{comparison_id, capability, input, answer_1, answer_2, truncated,
truncated_parts?}`, no vendor; 204 when there is none, after clearing the flag and looking once
more (a row stored meanwhile sets it again). `POST /comparisons {comparison_id, choice:
1|2|tie|both_bad, reason?}` maps 1/2 back to a/b through `first`, sets `decided` with one
conditional update (a second verdict returns 200 with the first), and settles the flag the same
way when nothing is left. Another team's or a failed row is a 404; a caller pinned to tags reads
and answers none. The team `/mcp/` `compare` tool (scope `treg:read`, like `call_result`, since it
shows the team's past inputs and answers; hidden from OpenAI's clients like `review`) fetches the
next pair with an instruction line, or submits with `comparison_id` and `choice`.

**The invitation carries the pair.** When `/call/` answers a team `/mcp/` call with
`X-Treg-Hint: compare`, the MCP reads `GET /comparisons/next` and puts the pair in the call result
as `compare: {comparison_id, capability, input, answer_1, answer_2}`, each answer cut to its first
`INLINE_ROWS` list items, `INLINE_FIELD_CHARS` per string and `INLINE_ANSWER_CHARS` in all
(`mcp._compact_answer`), with a hint naming the `comparison_id` to answer through the `compare`
tool. A stored cut travels as `truncated_parts`, rows or text left out here as `shortened`, and
an incomplete input changes the hint. The pair is attached only for a credential that may read it
(an OAuth grant needs `treg:read`, the `compare` tool's scope, beyond `call`'s `treg:call`) and only
when read within `INLINE_READ_TIMEOUT_S`, since the call has already answered and been charged.
With no pair waiting (the flag outlived it) the result carries neither. In a simulated test
(2026-11-21), agents that had to fetch the pair with the `compare` tool first almost never judged
it, while agents that found both answers in the call result nearly always did.

**Settings** (all off by default): `TREG_COMPARE_CAPABILITIES` (a JSON list, e.g.
`["web.search"]`), `TREG_COMPARE_EXCLUDE` (a JSON list of providers never asked as B),
`TREG_COMPARE_DAILY_USD`, `TREG_COMPARE_MAX_B_MICRO` (default 100000, $0.10), `TREG_COMPARE_RATE`,
`TREG_COMPARE_MAX_PER_RUN`, `TREG_COMPARE_MAX_PER_DAY`, `TREG_COMPARE_LOOKBACK_HOURS`,
`TREG_COMPARE_BUDGET_PER_HOUR` (compare invitations per team per hour), `TREG_COMPARE_TEAM_CAP`,
`TREG_COMPARE_WINDOW_DAYS`, `TREG_COMPARE_ANSWER_DAYS`, `TREG_COMPARE_MAX_ANSWER_BYTES`. Rollback: `TREG_COMPARE_MAX_PER_DAY=0` with the cron kept:
no marker is written and no vendor is called, while each run still removes orphaned rows (removing
the cron stops that cleanup too). Stored comparisons stay and waiting ones are still offered until
they age out.

## Published verdicts

Model reviews lean toward `useful`, often precede actual use and use the scale differently, so a score
means something only between sibling endpoints of one capability: it is published per endpoint and
never rolled up per provider (a provider average mostly measures how hard its jobs are). Reviews do
not affect ranking, routing or `catalog_get` beyond being shown.

`domain.feedback.verdicts` folds the review window into `reviews` on catalog platform, provider and
endpoint responses (`GET /catalog/endpoints/{id}` also attaches it to each sibling, without quotes).
Rules a change must keep:

- One team is one vote per endpoint; a team's several reviews split its vote. This is what keeps a
  batch-rating agent from outweighing a team. `not_sure` is not a verdict.
- `teams` is a band (`TEAM_BANDS`), never the exact count: beside the shares an exact count would give
  a small group's votes away.
- Below `MIN_TEAMS` there is no `share`, only early quotes with `teams: 0`; with nothing quotable there
  is no `reviews` at all.
- Quotes: at most one per team (its latest), drawn in the proportions the teams voted rather than for
  tone, with verdict, month (never day) and `client`; no team, user or call is identified. The review
  tool tells agents reasons may be quoted publicly and to leave private data out, but agents still
  copy what they found: a reason carrying an email address, a phone number (nine digits or more) or a
  LinkedIn profile link (public, Sales Navigator or Recruiter) is never quoted (`verdicts.quotable`).
  Patterns cannot catch a name, so a reason is quoted only after Jev has judged it unlikely to name or
  identify a person (`person_p` below `verdicts.PERSON_P_MAX`, `verdicts.cleared`); a reason not yet
  judged is not quoted. Reasons are otherwise quoted as written.

Jev's judgements are written to the review row by `treg-worker reviews judge`, an hourly cron
(`application.review_judge`), once per review and in independent passes over the window:

- Privacy: each quotable reason gets `person_p` and `privacy_judged_at`.
- Attribution: each `partly` or `not_useful` review gets `primary_code`, an optional `secondary_code`
  and `codebook_version` (`domain.feedback.review_codes`, a fixed codebook the vendor report cards
  aggregate per endpoint), first from its reason alone. `code_basis` says what the codes were judged
  from: `reason` for a direct call's review, which may still be upgraded; `reason_final` for a routed
  call's (its rows cannot prove which answer the caller got) or one without a reason; `answer` once
  upgraded. A review with no reason is `NOREASON` without asking. A new codebook version re-judges
  only reviews coded under an older one, from their reasons, and the upgrade follows again.
- Praise (`judge_praise`): each `useful` review gets a code from `review_codes.PRAISE_CODES`, an
  optional second one and `PRAISE_CODEBOOK_VERSION` in the same columns; its usefulness, which never
  changes, says which codebook they are from. Most praise codes pair with a negative one, so a
  report card can set a praise and a complaint about the same thing side by side: ACCURATE with
  ACCURACY and RISKY, FRESH with STALE, RELEVANT with WRONG and PARAM, COMPLETE with PARTIAL and
  COVER, STRUCTURED with FORMAT, ACCESS with BLOCK and JS, CHEAP with PRICE; HONEST (a plain miss
  instead of a guess), FAST and GENERIC (useful, nothing specific praised) have no pair. GENERIC
  stands alone: it is never a second code nor takes one, and GENERIC with a named quality likely
  beside it is coded as that quality (`review_codes.NO_SECONDARY`). Generated
  media has no code of its own: following the prompt is RELEVANT and a faithful output ACCURATE,
  the axes its complaints are coded on (PARAM, ACCURACY), and never HONEST, which is only for a
  miss or an uncertainty flag. Praise is coded from the reason only and
  is always `reason_final`: an upgrade would read an answer per review, and useful reviews are
  most reviews. A blank reason is `NOREASON` without asking. Only
  the last `review_judge_praise_days` (28) are coded, newest first and at most
  `review_judge_praise_per_run` per run, which bounds the backfill when the pass is first turned
  on or the praise codebook changes; it runs last, on the time the other passes leave. A review Jev
  could not code steps aside so the cap never stays stuck on the same newest rows: an unreadable
  answer backs off from an hour to a week, a failed request waits an hour without counting a try,
  in `answer_retry_at` / `answer_attempts` (a useful review is never upgraded, so it has no other
  use for them; coding it clears both). The run reports `pending` (the whole window still uncoded
  when it starts), `selected` (its share) and `retry_later`.
  `review_judge_praise` off skips it (its rollback switch; codes already written stay, and a
  reader that wants none ignores `useful` rows).
- Upgrade with the answer (`judge_answers`): a direct call's review coded from its reason is asked
  again with the request and the shortened answer beside its reason (`application.review_answers`,
  `review_codes.body_code_question`), `review_judge_answer_batch` per request, each answer cut to
  `review_judge_answer_chars` (JSON that fits is shown as sent, and a shortened one keeps the vendor's
  own number text; a leading byte order mark does not stop JSON being read as JSON). The request is
  the one the archive keeps for the question, the first call's: its query is shown sorted
  (`review_answers.request_url`), its body only when the key proves it is this call's
  (`request_body`: no JSON body holding a fractional number or any object, which another call can share the key with), and
  `review_codes.REQUEST_NOTE` tells every judge, the blind audit too, that the order of parameters
  is not the agent's while every value shown is exact. The judge sees what this call's delivery is known to have had: its own
  status and the body (`review_answers.as_delivered`), with treg's own credentials for the
  provider masked first, since the judge is a model outside treg and a copy kept before the
  archive masked answers may still quote them ([proxy-model](proxy-model.md), "Answers that quote
  a credential"), and without the provider-reported charge the
  endpoint declares (whether this delivery kept it is not on file), read from a version of that
  answer that kept its bytes and still matches its content hash
  (`archive.resolve_result(prefer_bytes=True, verify=True)`). The reason codes become
  `reason_final` at once only on facts that will not change: the call row has no archive link, it
  came through `/table` (`CallRecord.surface`: a reshaped answer; an idempotency key keeps its
  surface, `IdempotentCall.surface`, and never replays on the other), it predates `review_answer_surface_since` (for a deployment with `/table/` on: when the last
  instance of a build that did not record the surface stopped serving), or the bytes are not UTF-8
  text.
  Anything else that does not read now (the archive not written yet, a hash-only or gone answer, a
  failed read, object storage not configured here, the database unreachable, a corrupt or
  mismatched copy) is
  tried again from `answer_retry_at`, an hour doubling to a week (`answer_attempts`), and after
  `review_judge_answer_attempts` tries the reason codes stand; a review waiting its turn is not read,
  so it never holds back the others. Answer codes may replace this version's `reason_final`, so a run
  that read the answer wins over one that meanwhile gave up. The worker opens the archive's object
  store, and without its credentials logs that and runs on. `review_judge_answers` off skips the pass
(its rollback switch: 0128 stays). Each read gets only the time left before
  reading stops, which leaves a request's worth before the deadline, so what was read is sent; a read
  cut by that budget waits six hours without spending a try. Back-off writes hold only while the
  row still has the tries the run read, so concurrent runs never move it back. No request starts after the deadline.

Each answer is validated on its own (a Noul must be a number in [0, 1], a Choice one of the codes);
a failed request or an unreadable answer leaves that review unjudged for the next run, and one pass
failing never stops the other. A judgement is written only onto a review still unjudged, or coded
under an older codebook version (`domain.feedback.reviews`), so overlapping runs never replace or
downgrade an answer; no database connection is held across a Jev request.

`treg-worker reviews body-pilot` (`application.review_body_pilot`) is a dry run of coding with the
answer in view: it pages through the negative reviews, newest first, until it has read `--sample`
answers still on file, and asks Jev the codebook twice, from the reason alone (a fresh control: the
row is coded with the answer by the hourly judge) and with the request and the answer shortened by `body_view.shrink` (long strings cut, long lists kept to their first items
and a count, always valid JSON, every mark a cut leaves named in `body_view.MARKERS` and explained
in the question), and prints how its codes compare with the reason-only ones. Routed reviews are
counted as `answer_unconfirmed` and never compared (a batch whose second request would start after
the deadline is not sent and counts as `half_asked`; one cut before its first, `read_not_asked`): the audit rows cannot prove which answer the
caller got (a confirmed find returns the finder's answer while the review is credited to the
confirmer, `feedback.answering_child`; an upgrade returns another vendor's; a miss returns none),
and the route's `served_by` is not stored on them. A blank
reason is skipped; no Jev request starts after the deadline. It writes nothing and prints only
counts, code pairs and review ids, never a reason, request or answer.

`treg-worker reviews body-audit --pairs id:REASON[:CODE]=BODY[:CODE],...` (`application.review_body_audit`)
checks the pilot's changed codes with a stronger judge: for each review, Claude through the AI
Gateway (`ai_gateway_api_key`) sees the reason, request, shortened answer and codebook and picks
between the pilot's reason-only and body codings, shown as A and B in an order
fixed by the review id. It writes nothing, prints winners per code change and review ids only, and
starts no request once its spend reaches `--max-usd`.

Without `typesafe_api_key`, or with the cron suspended, nothing new is judged: new reasons are not
quoted, but reasons already judged clean keep being quoted. To stop quoting those too, revert the
gate in `verdicts.cleared`.

`application.feedback.endpoint_verdicts` caches the fold per process; only the first fold is awaited,
later refreshes run in the background and a failed refresh keeps the previous fold. With no fold the
router attaches nothing: an enrichment must never take the catalog down.
