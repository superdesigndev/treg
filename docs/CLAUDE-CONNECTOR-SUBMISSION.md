# Claude Connectors Directory — submission runbook

This runbook is the owner-facing release gate for Treg's catalog-only Claude connector. Do not
submit until every required engineering and human check below has passed. Store no reviewer
password, OAuth code, access token, refresh token, or provider secret in this repository or in test
evidence. No reviewer passwords, OTPs, or tokens are committed or placed in ordinary submission notes.

## Directory fields

| Field | Submission value |
|---|---|
| Name | Treg |
| Tagline | Live data and APIs for Claude, without API keys |
| Categories | Data; Sales and marketing; Productivity |
| Publisher | Superdesign |
| Transport | Streamable HTTP |
| Server URL | `https://treg.to/mcp/v2/` |
| Authentication | OAuth DCR/CIMD with S256 PKCE |
| Documentation | `https://treg.to/connectors/claude` |
| Privacy | `https://treg.to/privacy` |
| Support | `https://treg.to/support` |
| Logo | `plugin/assets/logo.png` (1024×1024 marketplace logo) |
| Allowed link URIs | None |
| Availability | Immediately after directory approval |

Describe Treg as a **third-party/community connector published by Superdesign**. Do not call the
listing official, Anthropic-built, Anthropic-approved, verified, or certified unless Anthropic has
explicitly granted the corresponding label.

## Owner prerequisites

- [ ] Obtain Owner access to a Claude.ai Team or Enterprise organization. A platform.claude.com API
  organization does not satisfy this requirement.
- [ ] Approve the name, tagline, categories, catalog-only scope, data-handling answers, and logo.
- [ ] Choose one reviewer-access option:
  - **Default:** a reviewer signs up with any verified email. A new Treg team receives **$1.00 in
    free credit**, requires no payment method, and can immediately use platform-provided catalog
    endpoints.
  - **Extended testing:** the reviewer sends Superdesign the exact verified Treg email used for
    sign-in. Superdesign sends an email-bound invitation to `claude-connector-review-org`, which has
    **$20.00 in review credit**. The invitation is bound to that email; it is not a universal or
    shareable code.
- [ ] After review, revoke the review OAuth grant and any temporary provider access used for
  acceptance calls.

## Engineering evidence

- [ ] Full automated suite passes, including the exact six-tool contract, annotations, schemas,
  method separation, catalog-only resolution, OAuth version isolation, transport protections,
  pricing, metering, audit, idempotency, and error attribution.
- [ ] MCP Inspector connects to `https://treg.to/mcp/v2/`, authenticates, lists exactly the six
  expected tools, and successfully invokes each tool.
- [ ] The deployed connector emits `claude-connector` attribution for catalog searches, requests,
  details, balance reads, and provider calls.
- [ ] The existing team MCP contract at `https://treg.to/mcp/` remains unchanged.
- [ ] Save dated, secret-free evidence: test output, Inspector result, endpoint ids, expected/actual
  balance deltas, audit ids, and screenshots with private values redacted.

## Mandatory production custom-connector test

Anthropic documents custom and directory connectors as using the same runtime. This test blocks
submission.

1. In Claude.ai, add `https://treg.to/mcp/v2/` as a custom connector.
2. Sign in with the verified reviewer email using the selected access option, select the resulting
   team, and verify the displayed identity, team, balance, and daily cap.
3. Ask Claude to search Treg for backlink endpoints for `example.com`, compare multiple priced
   options, and stop before making a provider call.
4. Choose one platform-key-enabled GET/HEAD/OPTIONS endpoint priced below $0.01. Ask Claude to run
   it; verify success without a write confirmation and record the exact balance and audit delta.
5. Choose one non-mutating POST-based search endpoint below $0.01. Ask Claude to run it; verify that
   Claude presents its write approval, approve it, then record success and the exact delta.
6. Ask Claude to check `balance`, then submit a clearly labelled test `catalog_request`.
7. Verify the tool list contains only `catalog_search`, `catalog_get`, `catalog_call_read`,
   `catalog_call_write`, `balance`, and `catalog_request`. Create or use a same-named team tool and
   verify it cannot shadow the catalog endpoint.
8. Disconnect the connector, reconnect it, and complete OAuth again.
9. Smoke-test the same account in Claude Code and Claude Desktop. Record mobile as tested only if it
   was actually tested.

For each step record pass/fail, UTC time, surface, endpoint id, expected charge, actual charge, and
the corresponding audit id. Never paste raw provider responses containing personal data into the
submission packet.

## Submission and review

- [ ] Owner reviews the complete field set, evidence, data-handling answers, and attestations.
- [ ] Submit from the Claude.ai organization's settings, not the API Console organization.
- [ ] Keep `/mcp/v2/` and its tool names stable during review.
- [ ] Forward reviewer feedback to engineering. Owner approves material scope, copy, security, or
  branding changes before resubmission.
- [ ] Verify the live listing, its links, OAuth flow, logo, community/verification label, and tool
  set before approving an announcement.

## Monitoring and rollback

For seven days after publication, review `claude-connector` traffic and Anthropic's health view
daily; review weekly afterward. Track OAuth failures, 401/403/421 rates, tool errors, provider error
rates, latency, spend, insufficient-balance refusals, and usage. Rollback is additive: unmount or
disable `/mcp/v2/`; do not change `/mcp/`. Existing v2 grants then remain stored but cannot reach an
active v2 resource.
