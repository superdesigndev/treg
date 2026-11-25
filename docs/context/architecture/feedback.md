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
  - tests/test_review_body_audit.py
  - src/treg/domain/feedback/comparisons.py
  - src/treg/application/compare.py
  - src/treg/hints.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - src/treg/application/call/invite.py
  - src/treg/infra/kv.py
  - src/treg/application/feedback.py
  - src/treg/routers/feedback.py
  - src/treg/alembic/versions/0025_feedback.py
  - src/treg/alembic/versions/0026_callreview.py
  - src/treg/alembic/versions/0030_feedback_handling.py
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
`hint_attached` event per invitation sent. Surfaces only translate
the header: the CLI prints a stderr line, the team `/mcp/` renders it into its single hint slot with
priority replay > 402 > compare > review > mark > feedback. `/mcp/v2/` and OpenAI's clients on `/mcp/` render none,
so for them `hint_attached` counts an invitation the agent never saw.

- **Review**: a direct catalog call on treg's platform key, or a routed parent (credited to the
  child that answered, as an uninvited routed review always was), 2xx, not replayed, not archive-served,
  sampled by `hints.sampled("review", call_ref)`, and within the per-team budget
  `kv.store().take("review-budget:<org_id>", review_budget_per_hour, 3600)`. Per-call sampling scales
  with volume, so the budget caps how often a busy team is asked. There is deliberately no
  per-endpoint cooldown. An own tool never qualifies, even if its name matches a catalog endpoint.
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
  the next refresh. `invited`
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

- Counters (`take`, one atomic `INCR` + `EXPIRE NX`) **fail closed**: a store that cannot answer is a
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
which serves it better. It separates vendors a usefulness score cannot, and the pairs are chosen.
Results are internal; nothing here is published or changes routing.

**The marker, on the call path.** After a successful routed answer, `application.call.service`
calls `compare.mark_eligible(call_ref, capability, raw_headers, route_summary, archive_opted_out=...)`:
when the feature is configured (`compare_pairs` set, `compare_max_per_day` above 0), the capability
has a valid pair, the request carried no `X-Treg-Route-*` header (a caller that chose vendors or
routing is left alone), the team keeps an archive and `hints.sampled_at("compare", call_ref,
compare_rate)` samples it in, it writes `compare-ok:<call_ref>` in the shared store for two hours
(`MARKER_TTL_S`), inside a short timeout. The value is the contract input the caller sent
(`route_summary["identity"]`, the body minus a page cursor) as JSON, at most
`MARKER_INPUT_MAX_BYTES`; a larger input writes no marker. A page continuation (a `cursor` in
the summary) is never marked: B would be asked page one against A's later page. A team opted out of the archive gets no
marker, so its input never sits in the store. No database, no change to the answer, nothing raised: a lost marker only means the call is
never compared. It is the only work on the call path.
Every decision is counted by reason and `X-Treg-Client` (`marked`, `not_sampled`, `route_header`,
`page_continuation`, `not_paired`, ...) and logged per process every `MARK_LOG_EVERY_S` as one
`compare markers {...}` line of counts; a run reports its `no_marker` calls by client too.

**The run.** `treg-worker compare collect` (`application.compare.collect`), meant for an hourly
cron. `compare.pairs()` keeps a configured pair only when both providers have a verified adapter for
the capability, and logs the rest. Each run:

1. Removes the rows of teams that no longer exist, on every run, the feature on or off. Team
   deletion removes a team's comparisons itself (`ORG_SCOPED_MODELS`); this catches a row written
   while the team was being deleted, within one run.
2. Stops when off (no pairs, `compare_max_per_day` 0 or `compare_rate` 0) or when the UTC day's
   attempts (`compareattemptday`) have reached `compare_max_per_day`.
3. Chooses eligible teams first: not opted out of the archive, not a demo, whose agent answered
   at least one review invitation in `compare_answer_days` on a surface that shows a compare
   invitation (`CallReview.invited`): the team MCP (client `mcp` or `mcp-prior`), or a client that
   declared `compare` in `X-Treg-Hints` on the review itself (`compare-answering:<org_id>`, the
   time of the latest such review, read against the current window in one `MGET` for all teams; a CLI reports only the agent it runs under, so an old and a new release
   look alike in `CallReview.client`). A team whose agents never answer one there, such as a
   pipeline calling without reading hints, or one on a CLI that does not show compare, would leave
   its pairs unjudged and take the day's budget. And holding no
   access policy at all (a member or project tool ACL, a member pinned to tags, a deny rule, or a
   capability pin on a paired capability). A team with any is skipped, so the run never has to
   emulate a team's authorization.
4. Reads those teams' recent routed parents per paired capability: `endpoint_id =
   treg.<capability>`, `org_id` among the eligible teams and `created_at` within
   `compare_lookback_hours` (at most two, the marker's life), newest first, at most
   `SCAN_PER_CAPABILITY` (one ordered range of the `(endpoint_id, created_at)` index each), 2xx and
   `credential_tier` routed; drops calls already compared. Teams come first because other teams'
   calls can fill a newest-N window read over every team, leaving eligible teams' earlier marked
   calls unread until their markers expire.
5. Per call, until `compare_max_per_run` or `--max-seconds`, one at a time, any error ending only
   that call: the marker must exist (none, or a store that cannot say, skips it; the marker holds
   the sample, so the run does not sample again); the credited child (the latest successful
   `<call>:r<n>`, as reviews attribute it), on treg's key, with its archive identities and a
   retention-allowed endpoint; its provider A on a pair, B the other side; the team under
   `compare_team_cap` comparisons of this capability and pair (either order) in
   `compare_window_days`, counting decided ones and pending or ready ones younger than three days.
6. The contract input is the marker's value, canonicalized (`canonical_identity`): exactly the
   question the caller asked, so nothing is read back from A's provider request. A marker without
   an input (an older one) is skipped. A's answer comes from the archive record of that child
   (`arena_insights._evidence`), read through A's adapter (`route.child_outcome`); only a hit goes
   on.
7. B's target: an endpoint the router itself would ask for this input (`plan.candidates_for`, so
   a routing adapter: a judge-only `route: false` adapter builds no request; then `build_plan`'s
   filter cap, declaration and scope checks), on a provider with a pasted-key credential shape, not
   paused, platform-eligible with every platform credential its bindings name, not exhausted on
   treg's primary account, retention allowed, priced, with no second host or path parameter. B's
   adapter builds the request (`to_upstream`) with the router's variant.
8. Right before sending, the team is read again: it still exists, is not suspended, has no active
   flow-control pause (`org_flow_control.confirmed_unpaused`: a fresh read; a read that fails, an
   invalid publication or a pause in force all skip, unlike admission, which allows on failure),
   and still has no access policy and no archive opt-out.
9. The claim, in one short transaction: one attempt on the current UTC day's `compareattemptday`
   row, read at the claim, so a run crossing midnight counts on the new day (one conditional
   `UPDATE ... SET attempts = attempts + 1 WHERE attempts < limit`, the day's first row inserted
   on conflict do nothing; at the limit nothing is written and the run stops), then the team and
   pair cap read again (on Postgres the day row's lock, taken by that update, orders concurrent
   claims), then the comparison row as `pending` (its unique `call_ref` stops a second run,
   concurrent or later). A crash after the claim leaves `pending`: it still counts and is never
   resent. The day's count is no team's row, so deleting a team gives no quota back.
10. One request, through the relay alone (`infra.upstream.relay.relay`) on treg's primary key
    (`platform_bindings`): the request the router's first child call on treg's key sends for the
    team's question. The adapter's values go to the relay unformatted (a `false` reaches the vendor
    as `false`), the body as `route._child_body`, path parameters dropped as the call path drops
    them, with the call path's rewrites: treg's User-Agent, the provider's client header for the
    team (`tag_shared_client`) and its ceiling parameter at the router's default ceiling
    (`service.ceiling_query`). `body_read` is offered as the call path offers it (a key that goes in
    the JSON body is injected from it). No router, no fallback, no overflow, no hold, no ledger
    entry, no archive record, no capacity mark. Calls are `CALL_GAP_S` apart. The catalog price of
    the request is logged and stored as `b_cost_micro`.
11. B's answer is read through B's adapter as the router reads a child on treg's key
    (`route.child_outcome` with the canonical identity: a `question_miss`, such as no row on a
    `site:` the question names, is a miss), so both are contract `output`; the log line names the
    outcome and its reasons, never the body. The row is settled to
    `ready` only on a hit; anything else (an error, no answer, a miss) settles it `failed`, with no
    bodies, and nothing else is called. If the team was deleted meanwhile, the row is taken back.

**Spend and capacity.** A day's spend is at most `compare_max_per_day` times B's catalog price per
call, and that holds only where the catalog price is the request's maximum cost (a per-result or
usage-settled price can charge more than the estimate). The direct relay does not take the shared
provider limiter, fair-share or rate window the router's children take, so check each pilot
vendor's price and capacity before turning a pair on.

**Storage** (`callcomparison`, written only by `domain/feedback/comparisons.py`, as is
`compareattemptday`; no foreign keys): one row per call (`call_ref` unique), `pending` until its
call is settled. A `ready` row holds both outputs and the input, blinded: identifier keys removed at
any depth (`_treg`, `next_cursor`, `cursor`, `provider`, `served_by`, `endpoint_id`, and `source`
when it names a vendor), and either vendor's id, endpoint id or API host inside text replaced by
`[vendor]`, the text kept. Each answer is cut to `compare_max_answer_bytes` and the input to 2 KB,
`truncated` saying which; `first` (which answer is shown as 1) is random. The team's
`compare-ready:<org_id>` flag is then set for three days in the shared store, so `TREG_KV_URL` must
be set for markers and invitations to reach across processes.

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

**Settings** (all off by default): `TREG_COMPARE_PAIRS` (JSON, capability to unordered provider
pairs, e.g. `{"web.search": [["provider-a", "provider-b"]]}`), `TREG_COMPARE_RATE`,
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
(`application.review_judge`), once per review and in two independent passes over the window:

- Privacy: each quotable reason gets `person_p` and `privacy_judged_at`.
- Attribution: each `partly` or `not_useful` review gets `primary_code`, an optional `secondary_code`
  and `codebook_version` (`domain.feedback.review_codes`, a fixed codebook the vendor report cards
  aggregate per endpoint). A review of a direct call whose answer is still on file is asked with the
  request and the shortened answer beside its reason (`application.review_answers`,
  `review_codes.body_code_question`), `review_judge_answer_batch` per request, each answer cut to
  `review_judge_answer_chars`, as the reviewed call delivered it (`review_answers.as_delivered`: that
  call's status, and no provider-reported charge under `strip_reported_charge`); a routed call's
  review (its rows cannot prove which answer the caller got), or one whose answer is gone for good,
  from its reason alone. An answer still on file that cannot be read now (object storage
  unavailable, a failed read) leaves its review uncoded for a later run (`answer_unreadable`); a read
  that raises codes that review from its reason (`answer_errors`) and the run carries on. The
  worker opens the archive's object store, and without its credentials logs that and runs on, so
  such reviews wait for them. Reading stops with a request's worth of time left before the
  deadline, so what was read is sent, and no request starts after it. A review with no
  reason is `NOREASON` without asking. A new codebook version re-judges only reviews coded under an
  older one; `2026-11-25.1` brought the answer in, so it re-judges every review in the window.

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
counted as `answer_unconfirmed` and never compared: the audit rows cannot prove which answer the
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
