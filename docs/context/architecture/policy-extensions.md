---
title: Policy extensions - decision points a private abuse policy can plug into
status: shipped
sources:
  - src/treg/policy.py
  - src/treg/bootstrap.py
  - src/treg/application/signup.py
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
