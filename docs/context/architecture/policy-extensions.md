---
title: Policy extensions - decision points a private abuse policy can plug into
status: shipped
sources:
  - src/treg/policy.py
  - src/treg/bootstrap.py
  - src/treg/application/signup.py
  - src/treg/application/call/admission.py
  - src/treg/domain/identity/promotions.py
  - tests/test_policy.py
---

# Policy extensions

Every defense mechanism is public, but the values it is tuned with need not be: a public threshold
is a target a farm script can pace under. A deployment may therefore keep its abuse policy
(thresholds, lists, scoring) in a private extension that plugs into fixed decision points.
An extension sees only what a point hands it and answers only with that point's outcomes, so it can
restrict but never grant what treg would refuse, move money or change a public limit. Without an
extension every point takes its public default, which is the behavior the rest of these docs describe.

## Contract (`src/treg/policy.py`)

- `treg.policy` is the only treg module an extension may import; an incompatible change bumps
  `POLICY_API_VERSION`.
- `create_app` loads every `treg.extensions` entry point last. A version mismatch, a failing
  `install`, or a name in `TREG_REQUIRED_EXTENSIONS` that is not installed refuses startup, so a broken
  build never serves without the policy its operator relies on.
- One handler per point; a second registration is an error.
- A slow, raising or out-of-range handler falls back to the point's public default. An extension
  failure is the operator's bug, not something a caller can provoke, so it must cost users nothing
  beyond treg's default. It is reported (`event=policy_failed`, fault component `policy`) with the
  exception type only, never the message, which might name a private rule. An extension that wants to
  be strict when its own state is missing decides that itself and answers normally.
- The budget cancels a handler at its next `await`; a handler that blocks the event loop is not
  interrupted, so handlers must not block.
- Refusals show and log only the point's public outcome, never which rule fired.

## `signup_credit`

Outcomes `grant` or `defer`; on failure `grant`. Asked from `signup._grant_signup_promo` before the
claim, only for a user who could claim now (`promotions.signup_promo_email`) and a grant above zero:
once per team creation, and once on the sign-in that first verifies a user who already owns a team.
The eligibility read is committed before asking, so no database connection waits on the extension.

`defer` writes nothing (no claim row, no ledger entry) and leaves the user eligible, so the point is
asked again at their next team creation; a later `grant` is the release path and needs no new ledger
entry type. A first-proof deferral is never re-asked, since a first proof happens once.

Failure grants because the other signup-credit defenses (one credit per inbox, the human check, the
domain blocklist) still apply, so a broken extension returns to the public baseline instead of
withholding credit from every new user.

## `call_admission`

Outcomes `allow` or `deny`; on failure `allow`; budget 20 ms. Asked from `_execute_call` through
`application/call/admission.py` once a call has resolved to treg's own money (`mk.metered`: its key, or
its pay-per-use OAuth app), after the caller's ceiling is read and before both the direct reserve and an
overflow-only attempt. A call on the team's own credential never asks, and a deployment without a handler
reads nothing extra (`PolicyRegistry.asks_call_admission`).

The context carries what treg already knows about the call (team, caller, key, endpoint, provider, the
catalog row's `platform`, `funding`, the raw estimate) plus two reads made only for this point on the
session the call already holds: the team's first owner (`identity.access.first_owner_email`, the person
behind every agent identity) and whether the team ever bought credit (`money.has_purchased`). That session
is committed before asking, so no pooled connection waits on the extension.

`deny` is a 403 `policy_denied` whose body is `funds_required`: the endpoint, the provider, the top-up URL
and the own-key alternative, never the rule. Nothing is reserved, so nothing is released; the audit row
reads `refused_by=policy`, and a routed job treats it as a candidate-local refusal and moves on.

Failure allows: a broken extension returns treg to its public limits (balance, caps, quotas), which
still bound the spend, instead of refusing every metered call.
