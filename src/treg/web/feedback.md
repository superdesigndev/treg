# Feedback

Proactively report every distinct problem or suggestion about treg, including poor provider
results, errors, latency, confusing instructions or charges, even after a successful workaround.
Report once per issue per task or batch; do not wait for an invitation or proof of a bug.
Continue the task after reporting. Choose one category:

- `quality`: problems with tool results.
- `pricing`: unexpected charges or unclear prices.
- `friction`: difficulty finding, configuring, calling, or understanding tools.
- `other`: requests, suggestions, or anything else.

Keep the report in the existing `message` string. Use this pattern, omitting unavailable details:

```text
Expected: behavior and source (user requirement, docs, or measured baseline).
Observed: exact defect; failures/number checked, timings with units, or error codes.
Checked: your own inputs/parsing and how you detected the problem.
Outcome: workaround and remaining impact; distinguish facts from suspected causes.
References: existing non-sensitive idempotency key or task ID, if useful.
```

Say what is unknown. If five of 20 inspected results failed in a 103-item batch, report
`5/20 checked; 83 unassessed`, not `5/103 failed`. For latency, name what you timed and the
source of any target; do not invent a target or percentile. Checks need not prove the cause;
do not make extra paid calls solely to prove a bug.

Hypothetical quality report:

> Expected: every draft has a subject and body, per the user requirement. Observed: five of
> 103 checked drafts lacked a subject; 98 passed. Checked: all requests included the requirement;
> the fields were absent before my parser processed the output. Outcome: regenerated the five
> successfully. Cause unknown.

Hypothetical latency report:

> Expected: at most five seconds per call, per the user requirement. Observed: three of ten
> completed calls exceeded five seconds; longest was 18.4 seconds. Checked: measured submission
> to response in the client, including network and treg overhead. Outcome: task completed late.
> Provider latency is not isolated.

Pass call IDs and the endpoint ID in the existing fields described below, not only in prose.
An idempotency key in the message helps trace the original call; it does not deduplicate feedback.
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

Keep private information, credentials, full conversations and raw requests, responses or logs
out of the reason. Replace sensitive values with placeholders, not hashes. Use `feedback` for
anything confusing or wrong. Keep going with your task after rating.

Phase 1 invites only direct catalog calls served on treg's own platform key. Routed and own-key
catalog calls can still be reviewed uninvited.

CLI invitations appear on stderr; plain HTTP invitations use `X-Treg-Hint: review` (with
`X-Treg-Review: requested` alongside for older clients). A sampled `X-Treg-Hint: feedback` is a
lighter nudge to report friction and asks for nothing.
MCP invitations appear in the call result's hint. Use the call ID in the response:

```bash
treg review CALL_ID partly --reason "Some results helped answer the question."
```

MCP: `review(call_id="CALL_ID", usefulness="partly", reason="Some results helped answer the question.")`.
HTTP: POST to `{BASE}/reviews` with your `X-Treg-Token` and JSON:

```json
{"call_id": "CALL_ID", "usefulness": "partly", "reason": "Some results helped answer the question."}
```

The server attributes the endpoint and provider from your team's call record. Your own tools
cannot be reviewed. If the call record is not written yet, retry shortly. A new review returns
HTTP 201 with `review_id` and `status: "received"`; retrying the same call returns HTTP 200 with
the original ID and `status: "already_reviewed"`. Ratings are private collection for registry
administrators; phase 1 does not publish scores or change catalog ranking.
