# Parallel Decisions through treg

Connect your team's Parallel API key once. Calls with this key are billed by Parallel to your
Parallel account and do not debit your treg balance.

```bash
treg secret add parallel --value "$PARALLEL_API_KEY"
treg catalog get parallel.ai-judge.decide
```

One predicate question:

```bash
treg call parallel.ai-judge.decide --data '{
  "state": {"company": "Parallel", "website": "https://parallel.ai"},
  "questions": {
    "has_api": {"type": "noul", "instructions": "Does Parallel offer an API?"}
  },
  "include_excerpts": false
}'
```

Several questions can share the same state and web evidence in one request:

```bash
treg call parallel.ai-judge.decide --data '{
  "state": {"company": "Parallel", "website": "https://parallel.ai"},
  "questions": {
    "has_api": {"type": "noul", "instructions": "Does Parallel offer an API?"},
    "product_type": {
      "type": "choice",
      "instructions": "Which description best fits Parallel?",
      "criteria": {"web_api": "Offers a web API", "desktop_app": "Offers a desktop app"}
    },
    "documentation_level": {
      "type": "score",
      "instructions": "How detailed are Parallel's public API docs?",
      "criteria": ["No docs", "Partial docs", "Detailed docs"]
    }
  }
}'
```

Read each result under `answers.<question_name>`. The response includes the vendor's `decision_id`,
`mode`, token `usage`, and shared `citations`. A question can return `type: "refusal"` instead of a
decision value. See [Parallel's question types](https://docs.parallel.ai/decisions/question-types)
for criteria and limits.

## Verification ledger

Checked against Parallel's [API reference](https://docs.parallel.ai/api-reference/decisions/make-web-grounded-typed-decisions),
[question types](https://docs.parallel.ai/decisions/question-types),
[best practices](https://docs.parallel.ai/decisions/best-practices),
[pricing](https://docs.parallel.ai/getting-started/pricing), and
[rate limits](https://docs.parallel.ai/getting-started/rate-limits) on 2026-10-30.
The documented rate is $0.001 per `fast` request with up to 100 questions.

`tests/test_parallel_decisions.py` uses a mocked upstream for single and multiple questions,
response fields and citations, upstream error statuses, own-key precedence, and the platform-key
gate. `src/treg/catalog/examples/parallel.ai-judge.decide.json` is synthetic and sanitized; it is
not a captured live response. No live Parallel Decisions call or vendor billing observation was
made. Platform-funded access remains disabled until a maintainer verifies the platform key can
call Decisions, confirms actual charges and error billing against the shared account, and selects
settlement behavior from that evidence.
