---
title: Feedback - private intake for problems and suggestions
status: shipped
sources:
  - src/treg/feedback_contract.py
  - src/treg/domain/feedback/__init__.py
  - src/treg/domain/feedback/reports.py
  - src/treg/domain/feedback/reviews.py
  - src/treg/domain/feedback/verdicts.py
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
  - tests/test_feedback_handling_schema.py
  - src/treg/web/feedback.md
  - tests/test_feedback.py
  - tests/test_reviews.py
  - tests/test_endpoint_verdicts.py
  - tests/test_hints.py
  - tests/test_kv.py
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
team's `CallRecord.call_ref` or `LedgerEntry.call_id`; no cross-team lookup runs. A pinned caller
verifies only against rows carrying its own pin (on the ledger only the `reserve` entry carries
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
its successful child's endpoint/provider, keeping the parent as `routed_via`. `invited` is recomputed
at submission from a 2xx, non-cached, `platform`-tier record and the current sampling rate, so it
means "sampled, hence eligible"; the true count of invitations sent is the `hint_attached` event.
Volunteered reviews are accepted with `invited=false` and count through the team vote below. Retries
return the original id with `already_reviewed`; a unique index arbitrates races, and the savepoint
stays open until the application commit (SQLite deferred BEGIN). The sole writer is
`domain.feedback.reviews`. `GET /admin/reviews` is superadmin-only and excluded from OpenAPI; there
is no team read route.

## Review and feedback invitations

`application.call.invite.invitation(context, status, replayed)` runs in `routers.call.call_tool`
after the response is built and before it streams, and returns `review`, `feedback` or nothing. The
router sends `X-Treg-Hint: review|feedback` (plus `X-Treg-Review: requested` on a review, read by CLI
releases up to 0.18) and emits one `hint_attached` event per invitation sent. Surfaces only translate
the header: the CLI prints a stderr line, the team `/mcp/` renders it into its single hint slot with
priority replay > 402 > review > feedback. `/mcp/v2/` and OpenAI's clients on `/mcp/` render neither,
so for them `hint_attached` counts an invitation the agent never saw.

- **Review**: a direct catalog call on treg's platform key, 2xx, not replayed, not archive-served,
  sampled by `hints.sampled("review", call_ref)`, and within the per-team budget
  `kv.store().take("review-budget:<org_id>", review_budget_per_hour, 3600)`. Per-call sampling scales
  with volume, so the budget caps how often a busy team is asked. There is deliberately no
  per-endpoint cooldown. An own tool never qualifies, even if its name matches a catalog endpoint.
- **Feedback**: any other successful, non-replayed, non-cached call that samples in at
  `feedback_hint_rate`, own tools included. It asks for nothing, so it has no budget.

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

New tenants add narrow methods, never a generic get/set surface. `/admin/kv` reports `configured` and
`reachable`.

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
  tone, with verdict, month (never day) and `client`; no team, user or call is identified. Reasons are
  quoted unfiltered because the review tool tells agents they may be quoted publicly.

`application.feedback.endpoint_verdicts` caches the fold per process; only the first fold is awaited,
later refreshes run in the background and a failed refresh keeps the previous fold. With no fold the
router attaches nothing: an enrichment must never take the catalog down.
