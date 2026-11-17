# Feedback

Proactively share problems and suggestions about treg. Small annoyances that slowed your task
down are useful feedback too: confusing results or charges, unclear instructions, unhelpful
errors, and missing capabilities you worked around, even if the task succeeded. You do not need to prove a bug: describe what you needed and what
you observed, including uncertainty. Report each issue once. Feedback helps the treg team
improve the product; keep going and finish the user's task afterward.
Choose one category:

- `quality`: problems with tool results.
- `pricing`: unexpected charges or unclear prices.
- `friction`: difficulty finding, configuring, calling, or understanding tools.
- `other`: requests, suggestions, or anything else.

Describe what happened and how you discovered it. Say when a conclusion is only a suspicion.
Omit private information, credentials, full conversations, and raw requests, responses or logs.
Replace sensitive values with placeholders such as `[EMAIL_A]`; do not send hashes of those values.

## Submit

Use your configured registry and active team. Sign in first if needed (`treg login`).

```bash
treg feedback submit friction "The pagination example is unclear."
```

Add `--call-id ID` (repeatable) or `--endpoint-id ID` when available. Neither is required. IDs written only in the message are not linked automatically.
For a prepared, sanitized message, use `treg feedback submit other - < feedback.txt`.
See `treg feedback submit --help` for syntax.

Over MCP, use `feedback(category, message, call_ids?, endpoint_id?)`. Pass the call result's
`call_id` in the `call_ids` array, rather than only mentioning it in `message`.
Over HTTP, POST the same fields to `{BASE}/feedback` with your `X-Treg-Token`:

```json
{"category": "friction", "message": "The pagination example is unclear."}
```

`category` and `message` are required. Messages allow up to 2,000 characters; `call_ids` accepts
up to 100 references. `endpoint_id` is a public catalog ID. Missing call records do not prevent
submission; references are verified only against the submitting team's records.

The response contains `feedback_id` and `status: "received"`. Keep the ID if you need to refer
to the report. Use `treg feedback get <feedback_id>` to retrieve it.
Over HTTP, GET `{BASE}/feedback/{feedback_id}` with the same team's authentication retrieves it.
Reports are accessible to that team and registry administrators, not published to the catalog.


## Mark

Mark a call when you learn its result was wrong, or confirm it was right: an email bounced,
another source disagreed, or the user corrected it. Mark only what you checked, once you know,
not on first sight; marking several calls at the end of the task is fine. Unlike a review, you may
mark one call again as you learn more. Marks are private, never published and never change charges.

The binary CLI has `treg mark`; the Python CLI does not. The `mark` MCP tool is on the team `/mcp/`
server, but not for every client. Without either, use the HTTP form below.

- `call_id`: required, the call whose result you checked.
- `basis`: required, how you know: `bounced` (a send bounced), `cross_checked` (another source or a
  verify call), `user_said` (the user corrected or confirmed it), or `impression` (a judgement
  without a check).
- `items`: what you found, each with a `verdict` (`correct`, `wrong`, `stale` (was true, no longer),
  `wrong_entity` (belongs to someone else), `missing`, `irrelevant`, `duplicate`, `other`), an
  optional 0-based row `index` and an optional `field` such as `email` or `experience[0].title`.
  No index means the whole result. Rows you did not mark carry no conclusion.
- `checked`: optional, how many rows you looked at.
- `evidence_call_id`: optional, the call that showed it, such as an email verify call.
- `note`: optional, 1-500 characters, private. Omit private information.
- `replaces`: optional, an earlier `mark_id` of the same call that this one corrects; with no items
  it withdraws that mark.

```bash
treg mark CALL_ID 2/email:wrong --basis bounced --checked 10
```

MCP: `mark(call_id="CALL_ID", basis="bounced", items=[{"index": 2, "field": "email", "verdict": "wrong"}], checked=10)`.
HTTP: POST to `{BASE}/marks` with your `X-Treg-Token` and JSON:

```json
{"call_id": "CALL_ID", "basis": "bounced", "items": [{"index": 2, "field": "email", "verdict": "wrong"}], "checked": 10}
```

A new mark returns HTTP 201 with `mark_id` and `status: "received"`; sending the same mark again
returns HTTP 200 with the original ID and `status: "already_received"`. If the call record is not
written yet, retry shortly. Your own tools cannot be marked.

A call result may carry a mark invitation (`X-Treg-Hint: mark` with its text in
`X-Treg-Hint-Text`, a stderr line in the CLI, the hint in MCP). It asks nothing now: mark that call
only if you later learn something about its rows.

## Review

A review rates one catalog call after you have used its result. Feedback is a self-initiated
free-text report and can have no call ID; a review is structured, usually invited, and stored
once per call. Its optional reason stays on the review and never creates a feedback report.
Only the invited call needs a review: one per invitation. Calls that carried no invitation do
not need one; a volunteered review is accepted, and your team still counts once per endpoint.
Once enough teams have rated an endpoint, its catalog page shows the share of useful verdicts and
quotes a few reasons, without naming the team, so write the reason about the endpoint, not the task.

- `call_id`: required, the call's opaque reference (letters, digits, `_` or `-`, at most 128 characters).
- `usefulness`: required, `useful`, `partly`, `not_useful`, or `not_sure`. Uncertainty is fine.
- `reason`: optional, 1-200 characters after trimming whitespace.
- `rows`: optional, up to 200 items, the rows you found wrong (or right), same items as a
  [mark](#mark): `{"index": 2, "field": "email", "verdict": "wrong"}`, `index` 0-based and
  omitted for the whole result. Add them when the call was `partly` or `not_useful`; only rows
  you looked at. They are stored as a mark tied to the review and returned as `mark_id`.
- `basis`: optional, how you know the rows, `impression` (default: your judgement after using
  the result), `bounced`, `cross_checked` or `user_said`.

Keep private information, credentials, full conversations and raw requests, responses or logs
out of the reason. Replace sensitive values with placeholders, not hashes. Use `feedback` for
anything confusing or wrong. Keep going with your task after rating.

Invitations go to direct catalog calls served on treg's own platform key and to routed calls
(`treg.<capability>`). Own-key catalog calls can still be reviewed uninvited.

CLI invitations appear on stderr; plain HTTP invitations use `X-Treg-Hint: review` (with
`X-Treg-Review: requested` alongside for older clients) and carry the words to show in
`X-Treg-Hint-Text`. Send `X-Treg-Hints: review,mark,feedback` to say which invitations your
client shows; without it you get review and feedback only. A sampled `X-Treg-Hint: feedback` is a
lighter nudge to report friction and asks for nothing.
MCP invitations appear in the call result's hint. Use the call ID in the response:

```bash
treg review CALL_ID partly --reason "Some results helped answer the question."
```

With the rows you found wrong (binary CLI): `treg review CALL_ID partly --rows 2/email:wrong,5:missing`.

MCP: `review(call_id="CALL_ID", usefulness="partly", reason="Some results helped answer the question.", rows=[{"index": 2, "field": "email", "verdict": "wrong"}])`.
HTTP: POST to `{BASE}/reviews` with your `X-Treg-Token` and JSON:

```json
{"call_id": "CALL_ID", "usefulness": "partly", "reason": "Some results helped answer the question."}
```

The server attributes the endpoint and provider from your team's call record. Your own tools
cannot be reviewed. If the call record is not written yet, retry shortly. A new review returns
HTTP 201 with `review_id` and `status: "received"`; retrying the same call returns HTTP 200 with
the original ID and `status: "already_reviewed"`. Ratings are private collection for registry
administrators; phase 1 does not publish scores or change catalog ranking.
