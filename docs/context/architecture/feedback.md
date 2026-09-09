---
title: Feedback - private intake for problems and suggestions
status: shipped
sources:
  - src/treg/feedback_contract.py
  - src/treg/domain/feedback.py
  - src/treg/application/feedback.py
  - src/treg/routers/feedback.py
  - src/treg/alembic/versions/0025_feedback.py
  - src/treg/web/feedback.md
  - tests/test_feedback.py
related:
  - architecture/data-model.md
  - architecture/mcp-oauth.md
  - architecture/super-admin.md
  - interface/skill.md
  - interface/cli.md
---

# Feedback

`FeedbackCategory` is the shared four-value vocabulary: `quality`, `pricing`, `friction`, `other`.
`FeedbackIn` accepts only `category`, `message` (trimmed, 1-2,000 characters), optional `call_ids`
(at most 100 bounded opaque references, deduplicated), and optional public `endpoint_id`.
Guidance encourages proactive reports of small annoyances and observed friction even after successful workarounds, without requiring
a proven bug. It directs agents to pass references in `call_ids` (CLI `--call-id`), not only prose,
and to continue the task after reporting an issue once. Extra fields are rejected. Privacy instructions ask callers to replace sensitive values and omit
raw payloads; free-text content is not guaranteed anonymous or automatically sanitized.

`POST /feedback` uses `require_member`, including agent identities and the existing public-demo
write restriction. `application.feedback.submit` commits the report and a per-team rate-limit
hit in one transaction before acknowledging HTTP 201 with `feedback_id` and `status: received`.
The intake allows 30 reports per team per hour. It spends no balance, calls no provider and uses
no best-effort audit writer. Storage failures cannot produce a success acknowledgement.

`call_ids` and `endpoint_id` remain submitted claims. `verified_call_ids` is the subset found in
the submitting team's `CallRecord.call_ref` or `LedgerEntry.call_id`; no cross-team lookup runs.
Missing or delayed audit records do not reject a report. Verified provenance does not establish
that the reported problem is true. Intake does not rank providers or adjust charges.

`GET /feedback/{feedback_id}` returns the report only to its team; other teams receive 404.
`GET /admin/feedback` uses `require_superadmin` and the admin pool, with category filtering and
bounded descending-ID pagination (`limit`, `before`, `next_before`). It returns internal
attribution too. There is no external notification, issue sync, public feed, or review workflow.
`Feedback` participates in `ORG_SCOPED_MODELS`, so team deletion removes its reports.

CLI `cmd_feedback` sends the same payload to its configured registry, reading a prepared message
from stdin when the message argument is `-` (a terminal is rejected instead of blocking).
`cmd_feedback_get` retrieves a report through the same team-scoped HTTP read. The CLI rejects
empty or oversized messages locally and emits structured errors without echoing rejected input;
transport failures leave submission outcomes explicitly unconfirmed. Both MCP surfaces expose `feedback` with an enum in
their input schema, relay to the same HTTP intake, and declare a non-destructive, non-idempotent
local write. Their existing call permissions and transport boundaries remain distinct.

`skill.md` mentions feedback in its description and links to `{BASE}/feedback.md`, served by
`feedback_md` with the deployment's base URL. Detailed syntax and privacy guidance live in that
one document; CLI help and MCP share `FEEDBACK_DESCRIPTION`. The plugin generator propagates the
short skill instructions to each installation format. Self-hosted submissions stay on the
configured registry.
