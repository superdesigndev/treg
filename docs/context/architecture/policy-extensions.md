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
- **Credit checks fail closed.** An absent handler uses the public `grant` default. An installed
  handler that times out, raises or returns an invalid outcome instead yields `defer`; sign-in
  and team creation continue, and eligibility remains available. The `policy_failed` log and
  `PolicyFailed` fault event record only the exception type, never a private rule or message.
- **Handlers must not block.** The budget cancels a handler at its next `await`; a handler that
  blocks the event loop is not interrupted.
- **One registry per process.** `policy.registry` holds the handlers of the most recent
  `load_extensions`; tests that build several apps restore it themselves.
- **Refusals do not reveal the rule.** treg logs and shows only the point's public outcome. Which
  rule fired stays with the extension.

## Decision points

| Point           | Called in                                                | Outcomes          | Budget | On timeout, error or bad outcome |
| --------------- | -------------------------------------------------------- | ----------------- | ------ | -------------------------------- |
| `signup_credit` | `signup._grant_signup_promo`, before `claim_signup_promo` | `grant`, `defer` | 300 ms | `defer`, reported                |

### `signup_credit`

Asked once per team creation, and once on the sign-in that first verifies a user who already owns
a team (`signup.grant_signup_promo_on_proof`, for the oldest such team), only for a user who could
claim right now
(`promotions.signup_promo_email`: verified, still eligible, not suspended, not demo), and only
when the grant amount is above zero. The context is the user and team IDs, the login email and its
canonical inbox (`promo_inbox`). Additive, defaulted context fields also carry `client_ip` from the
trusted-proxy adapter and an immutable tuple of `SignupCreditIdentity` observations: provider,
hashed durable subject, and an optional provider-reported account creation time (naive UTC).
GitHub supplies that timestamp; Google does not. Missing or invalid timestamps remain unknown.
These are stored authenticated observations, including after switching to OTP; they are not public
profile matches or caller-submitted risk scores. The hash is a correlation key, not proof of a
unique human; age is only a supporting signal. Existing API-v1 handlers remain compatible.
The eligibility/evidence read and bounded expired-bucket cleanup commit before the decision, so no database
connection waits on the extension. Private policy can use these facts without an extra signup step;
the public default remains automatic credit, and no new scoring service is installed by this code.

`defer` consumes no credit state: no claim row, no ledger entry, and `signup_promo_available` stays true. The
team (or the sign-in) proceeds as usual. The point is asked again when the same user creates their
next team, so a later `grant` is the release path and needs no new ledger entry type. A first
proof happens once, so a deferral there is never re-asked by a later sign-in. treg logs
`event=signup_credit_deferred org=<id> user=<id>`. After a grant the user is no longer eligible
and is never asked again.

An explicit `grant` still passes through any enabled transactional network/domain/global limits described in
[money](money.md#signup-credit-eligibility). Extensions cannot bypass those limits or the inbox
claim or the durable provider identity claims. A policy failure cannot release credit that the
extension was installed to restrict. Policy context is a snapshot; the atomic claim re-reads current
identity links and consumption before money moves.

A user who believes their signup credit was withheld in error contacts the deployment's support;
an operator can grant credit through the ordinary admin grant.
