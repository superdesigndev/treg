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
