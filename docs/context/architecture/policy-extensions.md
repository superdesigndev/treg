---
title: Policy extensions — decision points a private abuse policy can plug into
status: shipped
sources:
  - src/treg/policy.py
  - src/treg/bootstrap.py
  - src/treg/application/signup.py
  - src/treg/domain/identity/promotions.py
  - tests/test_policy.py
---

# Policy extensions

## Why

Every defense mechanism in treg is public, and self-hosters need all of them: the rate limiter,
the email-domain blocklist, Turnstile, the one-credit-per-inbox claim. The values those mechanisms
are tuned with are another matter. A public threshold is a target: a farm script can pace itself
just under it. So a hosted deployment may keep its abuse policy (thresholds, lists, scoring)
outside this repository and plug it in through a fixed set of decision points.

**treg.to does this.** It installs a private extension with its own abuse rules. The extension sees
only what a decision point hands it and can only answer with that point's outcomes, so it can
restrict, never more: it cannot grant anything treg would refuse, move money, or change a public
limit. Self-hosted deployments run without it and get treg's default at every point, which is the
behavior described everywhere else in these docs.

## Contract (`src/treg/policy.py`)

`treg.policy` is the only treg module an extension may import. It is versioned by
`POLICY_API_VERSION`; an incompatible change bumps it.

- **Loading.** An extension is an object under the `treg.extensions` entry-point group with an
  integer `api_version` and `install(app, registry)`. `create_app` loads every installed one, last,
  into a fresh `PolicyRegistry` and records their names on `app.state.extensions`. A version
  mismatch or a failing `install` refuses startup. `TREG_REQUIRED_EXTENSIONS` (comma-separated
  entry-point names) also refuses startup when a named extension is not installed, so a broken
  build never serves without the policy its operator relies on. Empty, the default, requires
  nothing.
- **One handler per point.** Registering a second handler for a point is an error.
- **treg owns failure handling, and a failure falls back to the default.** When a handler is slow,
  raises or returns something outside the point's outcomes, treg uses the point's public default.
  An extension failure is almost always the operator's own bug or misconfiguration, not something a
  caller can provoke, so it should cost users nothing beyond what treg without the extension would
  do. It is reported instead: a log line `event=policy_failed point=<point> error=<exception type>
  outcome=<default>` and a `PolicyFailed` fault event (component `policy`) for alerting, both with
  the exception type only, never the message, which might name a private rule. An extension that
  wants to be strict when its own state is missing decides that itself and answers normally.
- **Handlers must not block.** The budget cancels a handler at its next `await`; a handler that
  blocks the event loop is not interrupted.
- **One registry per process.** `policy.registry` holds the handlers of the most recent
  `load_extensions`; tests that build several apps restore it themselves.
- **Refusals do not reveal the rule.** treg logs and shows only the point's public outcome. Which
  rule fired stays with the extension.

## Decision points

| Point           | Called in                                                | Outcomes          | Budget | On timeout, error or bad outcome |
| --------------- | -------------------------------------------------------- | ----------------- | ------ | -------------------------------- |
| `signup_credit` | `signup._grant_signup_promo`, before `claim_signup_promo` | `grant`, `defer` | 300 ms | `grant`, reported                |

### `signup_credit`

Asked once per team creation, only for a user who could claim right now
(`promotions.signup_promo_email`: verified, still eligible, not suspended, not demo), and only
when the grant amount is above zero. The context is the user and team IDs, the login email and its
canonical inbox (`promo_inbox`). The eligibility read's transaction is committed before the
decision, so no database connection waits on the extension.

`defer` writes nothing: no claim row, no ledger entry, and `signup_promo_available` stays true. The
team is created as usual. The point is asked again when the same user creates their next team, so
a later `grant` is the release path and needs no new ledger entry type. treg logs
`event=signup_credit_deferred org=<id> user=<id>`. After a grant the user is no longer eligible
and is never asked again.

A failure grants, as treg without an extension would. The other signup-credit defenses (one
credit per inbox, the human check, the domain blocklist) keep applying, so a broken extension
returns the deployment to its public baseline rather than withholding credit from every new user
until someone notices.

A user who believes their signup credit was withheld in error contacts the deployment's support;
an operator can grant credit through the ordinary admin grant.
