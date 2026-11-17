---
title: Route exploration - a per-call random first provider on a routed capability
status: building
sources:
  - src/treg/application/call/route.py
  - src/treg/application/call/settle.py
  - src/treg/domain/catalog/routing/plan.py
  - src/treg/config.py
  - tests/test_route_explore.py
related:
  - architecture/route-rules.md
  - architecture/call-events.md
  - architecture/money.md
  - architecture/shadow.md
---

# Route exploration - a per-call random first provider on a routed capability

The router asks the provider it expects to be cheapest per hit first, so the others are asked
mostly about the people the first one missed: the harder ones. Their measured hit rates and
answer quality then describe a different population, and providers cannot be compared on
equivalent inputs. Shadow pairs (`shadow.md`, asking a candidate again after the fact) fix that
but pay for every question twice.

Exploration fixes it on live traffic instead. For a configured share of a routed capability's
calls, chosen at random per call, one provider drawn uniformly from an allow-list is asked first.
Everything else about the call is the router's: the rest of the order follows as the router built
it, so a miss or a failure there still falls through. The split only changes who answers first.

## Configuration

```sh
# Off by default: an empty value explores nothing.
TREG_ROUTE_EXPLORE='{"people.email.find": {"percent": 5, "providers": ["hunter", "tomba"]}}'
TREG_ROUTE_EXPLORE_SALT=treg-route-explore-v1   # changing it reshuffles which calls explore
```

Each capability names `percent` (0 to 100, decimals allowed) and `providers`, distinct lowercase
provider names. Anything else fails startup, so a typo cannot explore something nobody approved.
Only providers that passed provider admission belong in the list (treg-internal
`provider-admission` skill). Setting `percent` to 0 keeps recording the control arm and moves
nothing; removing the capability turns the split off completely.

## The draw

`plan.explore_draw` hashes `salt:call_id` with SHA-256. The first eight bytes decide whether the
call explores (`% 10000 < percent x 100`), the next eight which provider. The bucket is the
call id, never the team or the question (unlike `TREG_ROUTE_PROVIDER_SAMPLE_PCT`, which is stable
per question): every call is its own draw, so a team's traffic is spread over every arm and no
team is stuck with one provider. A repeat with the same idempotency key replays the stored
answer and draws nothing.

A call takes part only when the split can neither worsen nor raise the price of its answer:

- no `X-Treg-Route-Prefer` (the caller's order stands), and no quality or decisive ordering;
- the waterfall is on (a miss must fall through) and the contract is idempotent;
- not a page resume, and not a plan preview (`build_plan` without a `call_ref`);
- the router's own first pick is on treg's key: a team's own key or tool is never moved.

A provider can be drawn only when it is in this call's plan, on treg's key, not exhausted, called
directly (no overflow aggregator), synchronous (no async task), and expresses every filter the
caller sent. The draw is uniform over the allow-listed providers that pass these checks for this
call, so a provider that cannot take an input type (a LinkedIn-only finder on a name and domain
call) is never drawn for it. Compare providers within an input type.

When the drawn provider is already the router's first pick, nothing moves and nothing is capped:
the call runs exactly as the default would, and records that provider as its arm.

## What the explored attempt may do

It never ends the call:

- A refusal before the request leaves (a team policy, its larger hold against the balance, a
  spend cap, a cost ceiling, a rate limit) is recorded as `skipped`, `explored first; <kind>`.
- A vendor rejection (4xx) or a `not_found` answer is recorded as an `error` and the router's
  order runs; on a default call either one can end the call.
- Errors, timeouts and rejections spend none of the waterfall's allowances. Once the explored
  attempt is over and did not answer, the router's order runs with fresh error, rejection, busy
  and rate-limit counters, and the final outcome of a call nobody answered (miss, `incomplete`,
  `route_failed`, 402, 429) is judged without that attempt. A miss by the explored provider is a
  real answer and counts as one.

## Billing

How a routed call bills (`run_routed`): each child reserves its own hold at its catalog price, and
a child that bills leaves the hold open (`DeferredSettle`); the parent settles every open hold at
the end of a call that answers and releases them all when it fails. Every attempt that bills is
paid: a per-success provider bills a hit, a per-call one also bills a miss.

The explored attempt is billed so that the caller never pays more than the router's own order
would have charged:

- If it answers (a hit, or a weak hit kept as an answer), its open hold is lowered
  (`settle.cap_deferred`) to `cap_micro`, the lowest catalog price among the plan's candidates.
  Any order the router could have run ends its hit on one of those candidates at that price or
  more, so the explored hit costs no more than any default hit. The provider's own figure stays on
  the ledger row (`explore_uncapped_micro`, `explore_cap_micro`).
- If it does not answer (a miss, an error, a rejection), its hold is released: the caller pays
  nothing for it, and the rest of the call bills exactly as the default would.
- A `X-Treg-Route-Max-Cost` check counts the explored attempt at its capped price. The child still
  reserves its full price against the remaining ceiling and the team's balance; when that does not
  fit, the attempt is skipped and the router's order runs.

treg pays the provider the difference. A child that settled on its own (through an aggregator or a
provider-reported figure, which the eligibility rules above exclude) cannot be lowered and is left
as it is. Margin applies on top of the capped figure as on any other.

## What each call records

On the routed parent's call event, in `input_meta` beside the route-rule facts
([route-rules.md](route-rules.md), "What each call records"; children join by `parent_ref`):

- `route_explore`: the arm, for every call that took part: the drawn provider, or `default` for
  the control share. Absent when the split is off for the capability or the call could not take
  part, so "control" and "not in the experiment" stay distinct.
- `route_order: explore`: the first provider actually reached came from the exploration order (the
  drawn provider was asked and the request left). A drawn provider refused before sending leaves
  `default`. `route_retry_order` is unchanged.
- `route_explore_absorbed_micro`: what treg took off the caller's bill on this call (customer
  price, micro-USD), present when the drawn provider was moved first.

No input value or answer is added: an arm is a provider name.

## Reading the arms

Compare providers on the explored arm only, within `input_type` (and any other segment), against
each other and against `default`. A provider's hit rate, verify and bounce outcomes and review
signals on its explored calls describe the same population of questions for every provider,
because the draw does not depend on the team, the question or the time. The default arm is the
router as shipped; the explored arms together cost `route_explore_absorbed_micro` summed.
