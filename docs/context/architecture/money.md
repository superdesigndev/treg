---
title: Money — prepaid balance, the ledger, Stripe, and the reports that check it
status: shipped
sources:
  - src/treg/domain/money/__init__.py
  - src/treg/domain/money/settlement.py
  - src/treg/domain/asynctasks/__init__.py
  - src/treg/models.py
  - src/treg/application/billing.py
  - src/treg/application/call/idempotency.py
  - src/treg/application/call/intake.py
  - src/treg/application/call/resolve.py
  - src/treg/application/call/service.py
  - src/treg/application/call/reserve.py
  - src/treg/application/call/settle.py
  - src/treg/application/asynctasks.py
  - src/treg/alembic/versions/0017_async_task_record.py
  - src/treg/alembic/versions/0018_async_resource_ownership.py
  - src/treg/alembic/versions/0019_async_poll_failures.py
  - src/treg/application/referrals.py
  - src/treg/domain/governance/budgets.py
  - src/treg/infra/__init__.py
  - src/treg/infra/stripe.py
  - src/treg/reconcile.py
  - src/treg/domain/referrals.py
  - src/treg/api.py
  - src/treg/application/signup.py
  - src/treg/routers/admin.py
  - src/treg/routers/billing.py
  - src/treg/routers/call.py
  - src/treg/routers/orgs.py
  - src/treg/routers/referrals.py
  - tests/test_call_architecture.py
  - tests/test_asynctasks.py
related:
  - architecture/catalog.md
  - architecture/proxy-model.md
  - architecture/data-model.md
  - architecture/ads-conversions.md
---

# Money

A catalogued endpoint can be served on **treg's own key** - no provider signup for the caller - which
means treg pays the provider and bills the team. That needs a balance, a way to top it up, and a way
to prove afterwards that the numbers were real. Three modules, one job each:

Two wallets of treg's spend through this machinery, and only these two: **tier-4 platform keys**
(`TREG_PLATFORM_KEY_*`) and **oauth-billed apps** - providers like X whose upstream bills the app
owner per use, so even a call on the org's *own* connection spends treg's prepaid credits
(`MarketplaceCall.billed_oauth`; detection and rates live in
[auth-secrets](auth-secrets.md)). Both run the same reserve→relay→settle path in `routers/call.py`, share the
fail-closed daily cap, and are distinguished in ledger meta by `tier: platform` vs `tier: oauth`.
An org's own key/credential on any *other* provider is never metered - there the org's account pays.

On an oauth-billed provider a **`free` catalog price is a bug, never a fact**: the upstream charges us
whatever the route costs, so a zero there means the entry is stale, not that the call is free. The
estimator must fall through to the provider rate rather than reserve nothing - it used to rest on
`0.0` being falsy, which read as both "no price recorded" and "the price is nothing", and let the
catalog publish $0 while the balance lost the fallback. Whatever the catalog publishes for these
providers is what the reserve takes, and a test walks the provider asserting the two agree.

| Module | Job | May it write money? |
|---|---|---|
| `domain/money` | the only code path that moves money | **yes - exclusively** |
| `application/billing.py` | billing policy, transactions, and webhook orchestration | no (it calls `ledger.topup`) |
| `infra/stripe.py` | the only Stripe SDK, signature verification, and network adapter | no |
| `reconcile.py` | read-only reports that check the ledger against the world | no |

The money seam is one function: `ledger.topup(org, amount_micro, payment_ref)`. Billing orchestration
asks the Stripe adapter to authorize or verify a payment, then asks the ledger to stage the credit
and owns the commit that lands it; neither adapter reaches into the ledger.

## Units: integer micro-USD, everywhere

1 micro = 1e-6 USD. A catalog call costs ~600 micro ($0.0006), so **cents cannot represent one call**
and floats cannot be summed for a year without drifting. The only float is the margin *rate*, turned
into an integer immediately (`with_margin`). Stripe speaks integer **cents**, so 1 cent = 10,000
micro and every crossing goes through `micro_to_cents` / `cents_to_micro` in `application/billing.py` - the one
file where two unit systems meet. Whole dollars appear only in settings and in what a human types.
Every `*_micro` value has a display-only `*_usd` twin: **never compute against the USD field.**

## The money tables and the invariant

`Org.balance_micro` (materialized) · `CreditBlock` (one funding event, and what is left of it) ·
`Hold` (an open reservation) · `LedgerEntry` (append-only journal).

`AsyncTaskRecord` is the durable owner of an existing hold after a metered asynchronous submission.
It stores the catalog-derived settlement basis, request evidence, task id, an optional terminal
result id or allow-listed dynamic poll URL, attempts and terminal state. It does not create another
money movement.

    balance_micro == sum(block.remaining_micro) - sum(open hold.amount_micro)

The balance is a column rather than a query because `reserve` has to be one conditional UPDATE (see
below). Every operation writes its `LedgerEntry` **in the same transaction, synchronously,
in-request**. Never route a ledger write through `audit.py`: it drops rows past its queue bound and
swallows exceptions, which is right for analytics and fatal for money.

## The five operations (`domain/money`)

| Op | Effect |
|---|---|
| `grant` | new promotional block, balance up (org creation, the referral bonus, the top-up bonus) - staged; committed by the application (signup, billing) or the referrals saga checkpoint |
| `topup` | new purchased block, balance up (after Stripe authorized) - staged; committed promptly by the application (billing) |
| `reserve` / `reserve_in_transaction` | balance down by the estimate, `Hold` opened - committed by the compatibility wrapper or the call application |
| `settle` / `settle_in_transaction` | blocks down by the observed cost, hold closed, difference refunded - committed by the compatibility wrapper or the call application |
| `release` / `release_in_transaction` | hold closed, balance refunded in full - committed by the compatibility wrapper or the call application |

All five primitives stage only: `grant`, `topup`, `reserve_in_transaction`, `settle_in_transaction`,
and `release_in_transaction` never commit or roll back the caller's transaction. Commits are owned by
the application (signup, billing, the call application) or by the two documented exceptions: the lazy
stale-hold reap boundary (reserve's sweep calls the public committing `release`, so each old refund
remains durable even if the new reservation returns 402), and the referrals saga checkpoints
(`domain/referrals` commits at named recovery points - claim, stamp, qualify - on a session the
application opened). `tests/test_call_architecture.py` enforces the no-commit boundary over the real
bodies of all five, with a mutation self-check that proves an injected commit is still detected.

Release metadata distinguishes a failed call from a normal non-billable provider response, and says
which side failed. A provider that answered 5xx releases as `provider_failed_<status>`; a call treg
never got an answer for (timeout, connect error, SSRF refusal, a failed oauth refresh) releases as
`call_failed_<status>`; excluded provider statuses such as a per-success 400 retain
`not_billable_<status>`. Both failure kinds are usually a 502, so the prefix is what tells them
apart - and it has to, because the error evidence that would otherwise explain the difference is
purged after 14 days while the journal is permanent.

A caller or task cancelled after reserve releases as `call_cancelled`. Compensation runs under a
shield before the cancellation is re-raised: it closes an acquired upstream response exactly once,
releases the hold, and gives back any pending idempotency label. The release uses the call reference
minted before reserve rather than the later `MarketplaceCall.call_id`, because a database commit may
have succeeded before `reserve` returned to assign that field. The ledger's conditional hold claim
makes both outcomes safe: a committed hold is refunded and a rolled-back reserve is a no-op. A DB
failure still leaves the committed hold to the lazy reaper. The same shielded cleanup covers
cancellation after an idempotency claim but before reserve; with no hold yet, it gives the label
back immediately so the next attempt does not wait for claim expiry.

A release that itself fails is logged and left to the reaper (`_platform_settle` never raises).
The response still reports `X-Treg-Cost-Micro: 0`, which is what the call ends up costing - but the
balance only catches up when the hold is reaped, so a caller reading its balance immediately after
a failed call may still see the reserve withheld.

**The gate is one statement**, which is the heart of the design:

```sql
UPDATE org SET balance_micro = balance_micro - :est WHERE id = :org AND balance_micro >= :est
```

The WHERE is the check and the SET is the debit, so the *database* decides who gets the last cent.
`rowcount 0` means insufficient funds → `InsufficientBalance` → a 402 the agent can act on. No
SELECT-then-UPDATE, no application lock, same behaviour on SQLite and Postgres: N concurrent callers
against a balance that affords K get exactly K successes.

**Block consumption order** is promotional-first, then oldest-purchased-first. Promo credit is a
marketing expense and never refundable; purchased credit is a deferred-revenue liability and *is*
refundable and disputable - so spending promo first keeps the refundable pool as small as possible
for as long as possible.

**Margin is applied inside the module** (`with_margin`), at reserve AND settle, and the rate in force
is recorded on every entry - so a rate change cannot retroactively rewrite what a call cost, and two
call sites cannot disagree.

**The hold reaper is lazy**, at the top of the shared reserve operation, scoped to the calling org. A crash between relay
and settle would otherwise strand that money forever. A background timer would need a scheduler and
leader election on a multi-instance deploy, and would still only run on a timer; sweeping one org's
stale holds is paid by the caller who benefits from it, and an org that never calls again has no
balance to strand. Each stale release commits independently before the new balance gate. A later 402
rolls back only the failed reservation, never a refund the reaper already made durable.
Pending `AsyncTaskRecord` holds are excluded from this short request reaper. Their worker has a separate
24-hour deadline and always closes the hold by settle or release.

## Deferred asynchronous settlement

`domain/money/settlement.py` is the single data-derived calculation seam. Reserve time freezes a basis
with `when: response|terminal` and `amount.kind: table|usage|observed`; `settle(basis, evidence)` returns
raw integer micro-USD and never writes the ledger. `table_amount_micro` bounds a `times` multiplier by
the field's declared `min`/`max` (finite, positive when no minimum is declared): an out-of-range value
matches no row and prices at the fallback, so a caller can neither reserve zero nor bill past the
validated ceiling. Both the normal response path and the async worker
use it. Provider differences remain in catalog YAML; there are no provider billing adapters.

For a tier-4 endpoint carrying `async`, a successful submission keeps its hold and writes an
`AsyncTaskRecord` whose `settlement_basis` freezes the whole price rule with the request it was
applied to, so the settlement replays from the row alone. BYOK calls create neither hold nor task
row. An authorized caller poll and the fallback worker share `_finish_terminal`: terminal 2xx
evidence settles the original task once under its row lock. Caller success without required usage
only learns result ownership and leaves the hold for a later observation; worker fallback retains
its reserve-based settlement with a reconciliation alert. Settlement errors leave the provider
response unchanged and cron retries. Only the winning finalizer archives terminal evidence.

The worker selects due candidates, acquires provider/global concurrency slots, then atomically
claims each still-due row. `attempts` fences stale workers from changing a newer claim's state.
The 60-second lease exceeds the 30-second processing deadline; queued rows are not leased.
Polls have a 10-second total deadline, including body consumption, and use the normal credential
injector (a static poll's parameter rides as `query_items` or a path substitution; the relay forwards
no URL-embedded query, which once left MiniMax v1 polls empty; a path parameter is substituted by its
declared location and percent-encoded; the body is capped at `MAX_POLL_BODY_BYTES`), takes terminal
evidence only from a 2xx poll (an error envelope that happens to say `succeeded` backs off like any
other non-2xx, the same rule the CLI applies). Valid nonterminal responses reset consecutive
failures and use the normal interval, capped at 60 seconds. HTTP errors, invalid JSON and timeouts
increase a persisted failure counter with 2/4/8/15-minute backoff, capped at the task deadline.
These are eligibility delays; the two-minute cron cadence determines the actual next check.
There is no provider-wide circuit breaker. **At the 24-hour
deadline it releases the hold in full**, marks the row `timed_out` with `reconcile_review`, and logs
an ERROR-level alert: an outcome nobody observed is the platform's cost, never the customer's, and a
provider that silently changed its status field shows up as absorbed timeouts in
`reconcile.async_task_settlement` (`absorbed_timeouts`) rather than as a quiet overcharge.
Platform-key poll and fetch calls are authorized against the caller org's row before relay. A
successful caller-driven poll may see a fetch-mode result id before the worker does, so the buffered
terminal response records that id on the same row; the worker records it as part of settlement too.
This makes the durable record both the hold owner and the authority for later shared-account objects.
An authorized platform poll with an explicit `free` price and zero estimate is
`MarketplaceCall.free_owned_poll`, not a new metered operation. It skips `_platform_reserve` and
`_platform_settle`, including their spend checks, stale-hold sweep, auto-top-up scheduling and all
new poll Hold/LedgerEntry/TagSpend writes. Ordinary authorization, usage limits and provider rate smoothing
still apply. Its response reports `X-Treg-Cost-Micro: 0`; the original submission's hold remains
owned by the original task and may close on this poll's terminal evidence. This exception does not cover fetch utilities, BYOK,
billed OAuth, or a zero estimate on a paid endpoint.
A 2xx that is not an accepted submission (not JSON, fails the endpoint's `expect` rule, or carries
no task id / an off-allow-list poll URL: `application.call.service._submission_rejected`) never
becomes a row: it settles at zero on the request path and the caller sees the body and `$0`. The
worker never lets one row abort a tick (`_process` catches everything, `settle_due` gathers with
`return_exceptions`), because an unset platform key for one provider must not stall every other
provider's settlements. An overflow child (`application.call.overflow._child`) carries its own
observed-kind basis at the aggregator price, so an aggregator that reports no cost settles at the
aggregator reserve, not at the parent's price. A `settle: usage` row reserves what its rate-card table says THIS request costs
(the matrix ceiling had made a $0.05 call demand a $6 balance) and settles the provider's reported
figure, which may exceed the reserve: the ledger takes the difference from the balance, the next
reserve is the gate, and `reconcile.async_task_settlement` lists every such `overrun` (the team
paid it) and every settle whose `block_shortfall_micro` > 0 (`absorbed_shortfalls`: the platform
ate what the team's blocks could not cover). A success whose terminal response carries no usage
figure settles at the reserve with `reconcile_review` and an ERROR alert, never at the ceiling.
When the pending row itself cannot be persisted, the request path releases the
hold with reason `async_task_not_recorded` and logs an ERROR alert - the same doctrine, since nobody
will observe that task's outcome. The only usage unit real traffic has settled is `usd` (OpenRouter's
`usage.cost`); a token unit returns with the first metered token-priced listing, together with its fx
rule and a live test. Ledger writes remain exclusively through `domain/money`.

The audit row (`CallRecord`) froze the reserve as `cost_charged_micro` at submission, so displays
must not read it alone. `application.asynctasks.views_for(org_id, call_ids)` is the read side: it
joins the org's `AsyncTaskRecord`s, loads the archived terminal JSON for settled ones, and derives
the artifact with the pure `domain.asynctasks.artifact(descriptor, terminal)` - the first URL under
`result.path`, or the `{endpoint, name, value}` retrieval target for fetch-mode descriptors (the
view formats it as `treg call … -p name=value`), plus `ttl_note`. The CLI's `--await` calls the
same function; there is one reading of the descriptor, not a mirror. `/calls`,
`/calls/{ref}`, `treg audit` and the dashboard Activity page all render from this one view.

**Idempotency on `topup` is enforced by the database.** `stripe_payment_intent` is UNIQUE, and `topup`
FLUSHES its INSERT inside a SAVEPOINT, before the balance moves: the loser of a race rolls back only
that savepoint - the caller's other staged work survives - and its re-SELECT returns the winner's
committed block, the same answer as the sequential path. The loser's flush blocks until the winner's
transaction commits, which is why the caller must commit promptly after `topup` returns. The
application-level SELECT is an optimisation, not the guarantee - two concurrent deliveries of one
PaymentIntent both miss it. (Fixed in #45; the unique constraint is part of the Alembic baseline
schema - the legacy startup migration that once added it is deleted.)

## Stripe (`application/billing.py` and `infra/stripe.py`)

**Credit happens on the WEBHOOK, never on the browser's return from Checkout.** The success redirect
is a URL the payer controls; treating it as proof of payment would let anyone mint balance by typing
it. The one exception is the off-session auto-top-up charge, where the server itself holds the
PaymentIntent's confirmed status - nothing attacker-supplied is involved - so it credits immediately
and the webhook redelivery lands as a no-op.

The webhook lives at `POST /billing/stripe/webhook`, **deliberately separate from the landing demo's
`/stripe/webhook`** and signed by a different secret: they are different Stripe accounts' events with
different consequences, and sharing a path would let one secret authorize the other's effects. It
**404s when unconfigured**, so a deploy without the secret exposes no unauthenticated POST surface.
`verify_event` uses the SDK's `verify_header` (timestamp tolerance = replay protection, and it handles
the several-signatures case during rotation) rather than `construct_event`, so a genuine event of a
type this SDK version predates is accepted and then ignored, not rejected as forged. A handler failure
returns 500 **on purpose**: that is how Stripe is told to retry.

The Stripe SDK is synchronous, so the application `_sdk()` seam delegates every call to
`infra/stripe.py`, which runs it on a worker thread. A blocking network call on the event loop would
stall every in-flight request, including the proxy's hot path. The adapter also converts the SDK's
return value to a plain dict (`StripeObject.to_dict()`, which is deep): the SDK's objects stopped
subclassing dict, so `.get()` on one raises, and every consumer -
plus every test fake, which returns plain dicts through this same funnel - reads dict-style. Keep it
that way: a consumer written against the object API would pass prod and break the fakes, and the
last divergence shipped a webhook handler that 500'd on every live checkout while the suite was green.

`_credit` also emits the `topup_completed` product-analytics event (`analytics.capture`, PostHog),
riding the same `fresh` flag as the receipt email so a redelivery re-emits nothing. `capture` is
synchronous and swallowing by construction - analytics is the one side effect in the webhook that is
allowed to fail, and it must fail silently, because a raise here would 500 the handler and make Stripe
retry a payment that already credited. Amounts travel as canonical integer `amount_micro`; the
`amount_usd` on the event is display-only.

On the same `fresh` branch, `_credit` also queues a `paid` Google Ads conversion (`adsconv.queue`) when
the org has a click to attribute to - but this one is **not** atomic with the credit: the credit is
durable before the conversion is queued, and the conversion is a second, separate commit. A crash
between the two loses the conversion permanently (the money is still correctly credited). Found in
review and accepted deliberately (2026-08-17): coupling the credit's fate to the conversion commit
would be backwards, because the credit must stand whatever happens after it; full reasoning and the
cheap future fix in [ads-conversions](ads-conversions.md).

**Invoices exist on the manual path only.** The top-up Checkout sets `invoice_creation`, so a
one-off purchase produces a real Stripe Invoice - number, PDF, billing address, tax ID - which is the
document a finance team accepts; Stripe's card receipt is not. Auto-top-up charges a bare off-session
PaymentIntent and therefore has **no** invoice, only a receipt: attaching one would mean rebuilding
the automatic charge as InvoiceItem + Invoice paid off-session, rewriting the money path and its
idempotency guarantees for the minority of payments. Say "invoice" only about the manual path.

The address on that invoice comes from `billing_address_collection="required"` **plus**
`customer_update={"address": "auto", "name": "auto"}` on the same session. Both are needed: the first
asks, the second persists the answer onto the Customer so the next top-up and the portal's invoice
archive already have it. Collecting without `customer_update` looks like it works and stores nothing.

**A promotion code discounts the price, it does not bonus the balance.** `allow_promotion_codes=True`
puts the code field on the top-up Checkout (codes are created in the Stripe dashboard, so a campaign
needs no deploy). Because the webhook credits `amount_total` - what Stripe actually collected - 20%
off means $40 paid and $40 credited. "Pay $40, get $50" would be a `ledger.grant` on top and is not
built. A 100%-off code collects nothing, so the session credits nothing: `_on_checkout_completed`
drops it as `zero amount`. Grant free balance through `ledger.grant`, never through a Stripe coupon.

**The top-up bonus IS that `ledger.grant` on top - tiered, and manual only.** `topup_bonus_tiers`
(`{10: 0, 50: 5, 100: 10, 200: 15}`, `{min_usd: percent}`) gives a manual top-up a `bonus` block
worth the highest tier at or below the amount (`bonus_for_topup`: $99 earns the $50 rate, $250 the
$200 rate; integer `amount * pct // 100`). It is granted inside `_credit`'s `fresh` branch - the one
point that knows money moved for the first time, so a webhook redelivery grants nothing - as a
**separate block** with `_KIND_ORDER` rank 0: it burns with promo and referral credit, before the
purchased block, and the purchased block stays exactly what the card paid. That is the whole
reason it is not folded into `ledger.topup`: purchased credit is a refundable liability, the bonus
is marketing spend. Automatic refills (`auto=True`) earn nothing - they repeat a chosen amount, and
a bonus there would be a permanent 9–15% margin cut on every refill rather than a reason to come
back and buy bigger. A refund or dispute does **not** reverse it: like an already-granted referral
bonus it is logged for a human (`_on_payment_reversed` → `bonus_blocks_flagged`), because the ledger
has no path that drives a balance down and this is not the reason to add one. The grant entry's
meta carries `payment_intent`, `pct` and `topup_block_id`; `topup_history` joins on it to show
`bonus_micro` per payment, and the receipt says "$100 + $10 bonus" so a balance that rose $110 on a
$100 charge reads as intended rather than as a mistake.

**The preselected amount climbs a ladder, capped.** `next_default_usd` looks at the org's last
*manual* top-up and returns the first preset above it, never past `topup_default_cap_usd` ($50):
$10 → $50, $50 → $50, nothing yet → `topup_default_usd`. Auto refills are skipped so the ladder
cannot ratchet on its own. The dashboard modal preselects it (`GET /billing` → `topup.default_usd`,
now per-org) and `POST /billing/topup` with no amount uses it - which is what `treg topup` sends.
Presets are four (`[10, 50, 100, 200]`) plus "Other": with eight cards from $5 up nobody ever
picked $100+ and repeat payers stayed flat; the minimum is $10 (fee math, and the referral
qualifying amount). The threshold for auto top-up is validated separately (`validate_threshold_usd`,
≥ $1): it is not a charge, so the top-up minimum must not apply to it - raising the minimum
without that split would have rejected the default $5 threshold on every enable.

**A saved card arms a consented policy from either webhook.** The modal records consent first
(`set_autotopup` → `no_card`) and relies on the top-up Checkout to save the card, so there is no
SetupIntent in that flow: `_set_default_pm` - called by both `_on_checkout_completed` and
`_on_setup_succeeded` - runs `_arm_if_waiting_for_card`, which turns the policy on only from the
explicit `no_card` state. A decline, 3DS, or a deliberate off (reason `None`, consent still on
file) stays off; a redelivered payment webhook must not switch a policy back on.

Turning `invoice_creation` on makes Stripe emit `invoice.created` / `invoice.paid` for every top-up.
`handle_webhook_event` drops them, deliberately: crediting on an invoice event as well as on the
PaymentIntent would be a second door onto the same money. The invoice is a document; the
PaymentIntent is the payment. Note also that `invoice_creation` on one-time Checkout is **priced
separately** by Stripe, and invoice emails only go out with Customer emails → Successful payments
enabled in the dashboard.

**`list_payments` reads rows from us and documents from Stripe.** The payment list is built from our
own `CreditBlock` rows - the same table the balance is computed from, so the history can never show a
payment the balance disagrees with, and amounts and dates need no network call. Stripe is asked only
for the links, in two list calls (`Charge.list` + `Invoice.list`, joined in memory) rather than two
per row; a failure degrades to rows without links and reports `stripe_ok: false`, because a Stripe
hiccup should cost the payer a download button, not their payment history. Both Stripe windows cap at
100 payments, so a very old top-up on a busy account comes back link-less - the portal is the
unbounded archive.

**`create_portal_session` is the self-serve surface** for card, billing address, tax ID and the full
invoice archive: hosted, because every one of those is a form we would otherwise own and the tax-ID
rules go stale per country. It requires a portal configuration saved in the Stripe dashboard, and it
refuses an org with no `stripe_customer_id` rather than minting one - a customer exists once someone
has paid, and an empty portal has nothing to show. `billing_state.portal` is the flag the UI hides
the button on, so a new team never sees a button that would 422.

**Auto-top-up is guarded in depth**, because it is the part that can go wrong expensively: recorded
consent (the PSD2/SCA mandate, a compliance requirement rather than a checkbox), a monthly cap, a
cooldown stamped in the DB *before* the charge so a second web worker sees it, a consecutive-failure
limit, and an idempotency key derived from the threshold crossing - so a burst of concurrent calls
that all notice the low balance produces exactly ONE charge.

Authorization splits by WHAT, not by who. `_billing_org` (the `/billing/*` routes - cards, top-ups,
auto-top-up policy, payment history, the portal) requires **admin or owner**: a card, a spend policy
and an invoice archive are the org's money, not a member's preference.

`GET /orgs/{id}/balance` is different, and deliberately so. Any **member** sees the figure and the
in-flight holds; the **funding detail** (credit blocks, the ledger) stays admin+. It used to be
admin-only, which meant a machine identity could not read the balance it was spending - while every
402 already hands the caller `balance_micro`, and both `llms.txt` and `skill.md` tell an agent to run
`treg balance` after a call. Refusing the number there while shipping it in an error was incoherent.
(Reported by Jason, 2026-08-07.)

The 402 also carries `autotopup_enabled` and an `auto top-up:` line in `message`. Off → the one
command that turns it on. On → the amount, threshold, cooldown and monthly cap, plus the flags that
raise them - because a team that is out of money *with* auto top-up on is being held by the cooldown
or the cap, and "add funds" alone reads as "auto top-up is broken" (cobl.ai, 2026-08-25: ~1,500
refusals between hourly $20 refills against a $60/day burn). The org fields are read **before**
the application reservation transaction: its rollback cannot be a source for refusal rendering after
the session closes. The MCP path still scrubs the payment link from the same
body (`mcp.py`, ChatGPT digital-goods rule) - the auto top-up line survives because it names a CLI
command, not a URL.

## The spend ceiling (`application.call.reserve`)

`_enforce_platform_daily_cap` is a per-org, per-UTC-day ceiling on platform spend, and it is
**fail-closed** - unlike the per-user call cap, which may let a few extra through under load. A query
that cannot answer refuses the call, because this one meters *our* money. The balance alone is not
enough: auto-top-up refills it, so the cap is the blast radius of both a runaway agent and a pricing
mistake in the catalog.

An endpoint whose price is unknown never reaches this path at all: `catalog_store.platform_eligible`
requires `cost_view(...)["usd"] is not None`, so "we don't know" is refused rather than served free -
see [catalog](catalog.md).

## Checking the work (`reconcile.py`)

Read-only, query-time, no scheduler. Three questions, each needing its own source of truth:

- **`price_drift`** - did the catalog's price stay true? Compares, per endpoint, the estimate
  RESERVED against the cost the provider REPORTED, both on the same `CallRecord` row. Providers
  re-price whenever they like; a silent 10% climb turns a positive margin negative with nothing on
  fire, and this report is the only thing that notices.
- **`provider_spend`** - reads the **ledger**, not the audit table, because it is the number a human
  holds next to an invoice. Audit rows are fire-and-forget and may be missing; ledger rows may not.
- **`repeat_rate`** - measurement only: how much of the bill was the same query twice. Answering it
  first is what makes a cache a decision rather than a guess.

Two aggregations happen in Python rather than SQL on purpose - the ledger's provenance lives in a JSON
`meta` column (portable JSON extraction across SQLite and Postgres is not worth a report), and these
are admin-scale windows over a bounded number of metered calls, the same tradeoff `admin_stats` makes.

## Call settlement and provider evidence

The metered path is `_platform_offer` → spend caps → `ledger.reserve_in_transaction` →
application commit → relay → settle or release. Settlement uses the frozen basis and provider
evidence through `_observed_cost_micro`; it falls back to the estimate when evidence is unavailable.
Provider-specific calculation stays outside the faithful relay.

| Evidence | Settlement behavior |
|---|---|
| Reported charge | DataForSEO `cost`, ScrapeCreators `credits_charged`, Akta `credits_consumed`, Lusha `billing.creditsCharged`, Exa `costDollars.total`; credit amounts use the catalog FX rate |
| Crustdata | Read `X-Credits-Used` from response headers using the same FX rate |
| Apollo | Known empty organization results are free |
| Hunter domain search | One whole search credit per ten returned emails, rounded up; an empty result is free |
| Hunter email finder | One whole credit when an email is present; a known miss is free |
| TikHub | Honor explicit no-charge prose; an embedded error that says it is charged still costs the estimate |
| Bright Data | Count delivered JSON-array records or CSV/NDJSON lines; a JSON object containing a status/snapshot handoff has zero records |
| Aviato | Fixed routes use the estimate; bulk enrichment counts successful records; catalog `settle: base` and `settle: modifiers` release documented-but-unbilled `reserve_only` riders |

Bright Data snapshot downloads are billable per result, including repeat downloads. Gzip or a
buffer-truncated response falls back to the estimate because the record count is unknown.
`MarketplaceCall.unit_micro` carries the raw per-row price on every credential tier.

The row-count signal for that estimate (`resolve._LIMIT_PARAMS` / `_body_limit`) reads the caller's
`limit`/`count`/`size`/`per_page`… in the query or body, the camelCase spellings (`pageSize`,
`numResults`, `perPage`, `maxResults`, lusha's per-company `contactsLimit`), a nested `pagination.{size,…}`, and — for providers that
bill one row per listed item — the length of `targets`/`keywords`/`domains`/`urls`/`lookups`/
`emails`. Each of those was a live overcharge first (2026-08-28: companyenrich `pageSize: 2`
settled 20 rows, moz's one `targets` entry settled 20 quota rows; 2026-09-02: lusha decision-makers,
catalogued FREE, answered 44 contacts for one domain and settled $5.49 from `billing.creditsCharged`
with nothing reserved). Without any signal it is the
20-row page, and a settle-at-estimate provider then charges that page.

The page default has no meaning at all when the catalog prices per INPUT entity, and the estimator
knows the difference since 2026-09-05: a `per_result`/`quota_rows` cost whose `unit` is `target`,
`domain`, `keyword` or `call` (`resolve._ENTITY_UNITS`) is counted by `_entity_count` — repeated or
comma-separated query values under an entity key, an entity array in the body (top level or inside a
JSON-RPC `params`, serpstat's shape), one per task object in a DataForSEO-style array, else exactly
one; `call` is always one; capped at 10,000, never at the 100-row page max, because a 5,000-keyword
export really does cost 5,000 keywords. Before this the 20-row default billed a one-target
`seranking.web.backlinks.summary` $0.358 for a $0.0179 call and a one-domain
`serpstat.google.domain.overview` $0.05 for $0.0025 — 32 and 166 calls across 12 and 38 orgs since
2026-08-12, refunded by hand — and the same number was the catalog's `~$/call` display, so the caller
saw the wrong price before the call too. On these providers nothing reports a cost after the fact,
so the reserve IS the charge: a wrong entity count is a wrong bill, not a hold the settle trues up.

The estimate is never a substitute for an available response-derived charge.

`_platform_settle` uses its own short session and never turns a served response into a 500.
A pool timeout gets one retry after 0.5 s; other failures are logged and the remaining hold goes
to the reaper. The request session must be committed before relay so settlement cannot wait on
a connection held by that same request. See [connection discipline](proxy-model.md#connection-discipline-a-call-in-flight-holds-no-db-connection).

## Shared-plan pricing: flat-fee providers, and the rate treg sets

A flat-fee provider (a monthly subscription with a rate limit or unlimited calls) has no per-call
vendor price, which kept every one of them out of the catalog. The ladder that admits them:

| The provider sells | The price of one call |
|---|---|
| real credits | vendor price ÷ credits (the normal fx entry) |
| a monthly request cap | fee ÷ cap - same arithmetic |
| a rate limit only | fee ÷ theoretical max is the FLOOR; the rate sits above it at a stated break-even |
| unlimited | a treg-set rate with the break-even printed |

The honesty rule that makes the last two rungs defensible: **we never claim these are vendor
prices.** What treg sells there is its own service - subscription custody, the key, a share of the
rate limit - at a published rate whose fee and break-even are printed beside it (fx.yaml
`kind: treg_shared_plan`; `check_fx` makes the marker impossible to carry dishonestly). The price is
also congestion control: at $0, one looping agent exhausts a shared rate limit for every team at
once.

Mechanically a shared-plan provider is just a credit provider whose credit is "one call on treg's
shared plan" - `cost_view`, holds, caps and settlement needed zero changes. What is new:

- **A 429 is never billable**, under any cost type. Capacity refusing a request is not the caller's
  bad input, and on a shared key it is treg's own saturation. This also fixed a pre-existing wrong:
  `per_call` used to bill upstream 429s, and no vendor bills a request it refused to accept.
- **A relayed 405 is never billable**, under any cost type. A catalog caller cannot choose a stale
  method: `_resolve_marketplace_call` rejects a mismatch before relay. The only method the provider
  can reject is therefore treg's recorded method, so settling a `per_call` hold would charge the
  team for catalog metadata treg owns. `_NOT_THE_CALLERS_FAULT` makes that path release the hold.
- **A 4xx that the signature table reads as OUR account running dry is never billable**, whatever
  its status: Apollo says "out of credits" with a 422, which `per_call` would otherwise charge to
  the caller as an input error - and, once overflow serves the same request through an aggregator,
  charge them twice. `_platform_settle` asks `signatures.classify` before settling any platform-tier
  4xx and releases the hold with reason `capacity_<kind>` (`test_apollo_out_of_credits_on_a_per_call_
  endpoint_*` in `tests/test_capacity_overflow.py`).
- **`shared_plan_recovery`** (`GET /admin/reconcile/shared-plans`): fee versus collected per
  treg-set rate, with `suggested_usd = fee ÷ measured calls` and an action at ±50% thresholds. It
  REPORTS; a human edits fx.yaml monthly. An auto-adjusting price would move under an agent's feet,
  and a rate card that moves on its own is not a rate card. The fee is scaled to the report's
  window, so a 7-day report compares against a quarter of the fee.
- **`price_drift` never sees these providers** - drift compares our estimate against the provider's
  own reported charge, and a flat-fee provider never reports one. Pinned by a test that fires if an
  observed-cost parser is ever added for one, because at that point the drift report would be
  policing a price treg itself set.

### Trial pools: the $0 rung

A third treg-set rate, `kind: treg_trial` (fx.yaml): a provider served on treg's own FREE-tier key
at exactly $0, capped per team per day (`trial_calls_per_team_day`, enforced by
`api._enforce_trial_allowance` - successes only, fail-closed, refusal 429 `trial_allowance_reached`
with a connect-your-own-key hint). The strategy: the pool is the demand probe - a hot pool is the
buy signal for the provider's commercial tier, negotiated with real volume numbers. Failed calls
never burn allowance (the same line billability draws), and another org's usage never touches this
org's pool (tested). At $0 the allowance is the only brake, so the validator refuses a trial entry
without one.

## Idempotency and retries

`application.call.idempotency` prevents a lost successful response from causing a second upstream
charge. It must replay the response as well as remember the charge; skipping only our debit would
still pay the provider twice.

- The caller supplies `Idempotency-Key` on `/call/`, or `idempotency_key` over MCP. No label means
  no deduplication. Never derive a label from request contents: identical requests can be new work.
- The database key is `(membership_id, key)`, additionally partitioned by the primary caller tag.
  Neither a global nor an org-wide key safely separates independent callers.
- A short application transaction claims a pending row before relay. The unique constraint
  arbitrates concurrent claims; the loser gets 409. Reusing a label with another fingerprint is 422.
- Metered successes and partially charged routed failures retain status, body, charge and call id
  for 24 hours. Uncharged failures, BYOK calls and owned free polls release the label immediately.
- Replays return `X-Treg-Idempotent-Replay: true` and the original `X-Treg-Cost-Micro`;
  MCP returns `replayed: true`. An async submission replay repeats its original reservation.
- Refusal and cancellation cleanup return an acquired label. Expired entries are swept lazily,
  scoped to the caller, at claim time.

This protects retries that opt in. Ordinary non-billable failures already release their hold;
a retry without a label after a paid success remains a new charge.

## Tag-based billing - a builder reselling treg to their own users

A builder embeds treg in their product and bills their own users. treg's job is exactly three things:
**attribution**, **enforcement**, **export**. treg never bills their end user, never holds their card
and never sets their price; margin stays 0%.

They run one org, one balance, one token, and tag each call with their own ids:

```
X-Treg-Meta: customer=cust_8123, workspace=ws_9, feature=email-finder
```

Up to 5 pairs. It is a **header, never a tool argument** - a model asked to pass an id drops it
somewhere in a chain, and a figure you cannot reconcile is worse than no figure. The builder's backend
already sets `Authorization` on the request; this is the same call site. `application.call.intake` parses
it **once** per request, before the idempotency block, and everything downstream reads that one
object. A second parse site would be a second chance to disagree about who pays.

Validation refuses rather than repairs: an oversized value is a 422, never a `[:128]`. A truncated id
merges two of their users into one invoice line, and a dropped tag is usage nobody bills. Values
containing `@` are refused outright - the ledger is append-only, so an email written today cannot be
erased on request tomorrow.

### Any tag can be reported; declared tags can be enforced

The split is **reporting versus per-call enforcement**, not money versus counts.

Reporting groups by any key with real money attached, because an invoice query runs occasionally over
a bounded window at admin scale - the same reason `reconcile.provider_spend` folds in Python.
Enforcement is different: it runs on *every* proxied call and must be an indexed aggregate, so a key
only becomes budgetable when the team **declares** it (`Org.budget_dims`, capped at 3). Declaring a
key is what buys it an index. The cap exists because each declared dimension is another row written
per call and another place settle-vs-reserve correction can go wrong; a team budgeting on `session`
would write an aggregate row per conversation.

**Budgets stack.** `workspace=ws_9` at $50/day and `customer=cust_8123` at $5/day are two `TagBudget`
rows and both apply to a call carrying both tags. Every declared dimension is evaluated and the first
breach in declaration order refuses, so the outcome is deterministic. The refusal **names the
dimension** - a builder running stacked budgets otherwise cannot tell a workspace breach from a
per-user one.
Validation and dimension selection share the `domain.governance.budgets` owner across the call and
control surfaces. `application.call.reserve` owns the call-side spend caps and tag-budget lookup. A newly observed tag returns
an explicit `created` result without committing; the call intake and governance router commit at the
same boundary that makes the row visible.

### `TagSpend` - why the money side is a table, not a JSON key

`ledger.reserve` writes one `TagSpend` row per tag, in the same transaction as the balance movement.
Each row carries the **full** call amount, so the same dollar appears under `customer` and under
`workspace` - cost-allocation-tag semantics. Summing *within* a dimension reconciles to the org total;
summing *across* dimensions deliberately double-counts, which is why every report names its key.

`amount_micro` tracks the hold: the estimate while in flight, rewritten to the consumed figure at
settle, and deleted on release. So a cap counts in-flight work at its estimate and errs toward
refusing - the right direction for money - while an invoice reads settled rows only, because an open
hold is not spend and billing it would charge again when it settles. Hence two deliberately separate
reads, `tag_spent_since` (cap) and `tag_invoice_since` (invoice), named so nobody "deduplicates" them.

### The caps are SOFT, and must never be sold as hard

`ledger.reserve` is exact because the balance is a materialized column: its check and its debit are
one conditional UPDATE. A per-tag total is an aggregate over rows, so N concurrent calls can each read
a compliant figure and together exceed the cap. Overshoot is bounded by `concurrency × per-call
estimate`, and that is acceptable **only** because the hard gates sit behind it - the org balance and
the per-org daily cap.

Making it exact would need a second materialized authority on spend: reset daily, decremented on
release, corrected on settle divergence. Four new ways to disagree with `domain/money`, which is the one
module allowed to move money. Not worth it. Never document these caps to builders as hard limits.

### Refusal bodies are not the org's

A tag refusal is the response a builder renders **to their own end user**. It shares no code with the
org-level 402, which carries `balance_micro` and a top-up URL - the builder's private numbers. Shape:
`{error, dim, val, spent_micro, cap_micro, period, estimated_cost_micro, message}`, and the checks are
ordered so a tag refusal can never fall through to the org 402.

### Invoices read the ledger. Always.

`GET /orgs/{id}/usage/by-tag` takes **money from the ledger** and call counts from `CallRecord`. Audit
rows are fire-and-forget and the queue sheds them under exactly the load a successful builder
generates; an invoice built on them would under-bill silently and unrecoverably. The money query lives
in `domain/money`, so presentation code cannot casually reach for `CallRecord`.

The response reports **`unattributed_micro`** explicitly rather than dropping it. The identity a
builder's books rest on is `attributed + unattributed == the org's settled spend for the window`, and
it must hold whichever dimension they slice by.

### Tag for counting, token for control

A tag is a **label, not a boundary** - anyone holding the token can send any tag. That is fine when
the only budgets and reports it touches are the builder's own. When a token will run on an end user's
*own machine*, the builder mints an agent token pinned to that user (`Membership.pinned_tags`,
`treg org agent-new --pin customer=cust_A`). The pin **beats the header**: naming a different value is
a 403, because otherwise the holder could retag their calls and walk out of their own budget, which is
the entire point of giving them a scoped token.

### The per-org daily cap has two owners

`budget_policy._effective_daily_cap` takes the minimum of the team's `Org.daily_cap_micro`
and the deployment's `platform_daily_cap_usd` ceiling (default $500/day). The team can lower its
limit and inspect it through `GET /orgs/{id}/settings`. A request above the platform ceiling is
refused, not silently clamped.

## Referrals

`domain/referrals.py` owns policy; credit moves only through `ledger.grant`. Rewards are flat,
symmetric credit bonuses (defaults in `config.referral_*`), not a percentage of pass-through spend.
Cash payouts and a platform-wide payout budget are not implemented.

### Eligibility and limits

A referral is redeemed at first team creation. Qualification requires cumulative purchased credit
to reach `referral_min_topup_micro`; a smaller top-up leaves the row pending and the billing offer
shows the remainder. It need not be the first payment.

`qualify` checks the referrer's availability, an available card fingerprint against previous
qualified/paid referrals, and the referrer's lifetime cap. Self-referrals are rejected by the
redemption flow. There is no requirement that the referrer previously topped up: funding their own
usable balance would add little abuse resistance. Evaluate eligibility rules against scarce payment
instruments, not freely created accounts. Without a global payout budget, the per-referrer cap
does not bound platform-wide exposure.

The Stripe fingerprint comes from the expanded payment method, is optional, and lives only on
`Referral`. Refusals remain recorded: `capped` means the self-serve allowance ran out;
`rejected` records another failed gate.

### Payment, concurrency and recovery

The referee receives credit on qualification; the referrer waits `referral_hold_days` (default 7).
Referral credit burns alongside promotional credit before purchased credit. The referee block id
guards its grant; the lazy sweep retries a missed instant grant.

Database uniqueness on `referred_org_id` and `qualifying_payment_intent` prevents duplicate
qualification. Referral grants use `once=False`: `grant(once=True)` is a SELECT check without
a unique constraint and is not a concurrency guard.

`_pay` commits the paid claim before granting, then commits grants and block-id stamps together.
A crash between those transactions can leave a paid row with missing block ids, visible through
`/admin/referrals`; this prevents double payout but requires reconciliation of incomplete payout.

`charge.dispute.created` and `charge.refunded` cancel rewards still in the hold window.
Already granted rewards are flagged for human review, never automatically reversed. These handlers
do not refund the purchased top-up.

`sweep` runs from the top-up and referral-page journeys, without a scheduler, and must not fail
either caller. Copy identifiers before a rollback can expire ORM objects; refresh expired rows
before rendering the response. See `_grant_referee`, `sweep` and `application/referrals.py`.

### Visibility and privacy

`offer_for_org` supplies `GET /billing` with a pending-only offer, cumulative paid amount and
remaining threshold. Referral and manual top-up bonuses stack. The offer masks the referrer's email
with one local-part character plus a fixed bullet run, retaining the domain.

The referrer's own `summary` exposes full referee emails for conversion attribution; that exception
is scoped to the referrer and disclosed in the privacy policy. It must not be extended to the
public referral-link flow. HTTP routes and first-team redemption are documented in
[the API fragment](../interface/api.md#referrals).

## Not money: the capacity mark

`application.call.settle._note_capacity_signal` writes a ratestore row (`capacity:lock:<key>`)
after a tier-4 balance/quota signature, and `_note_capacity_recovery` removes it after a probe's
2xx. Neither touches a balance, hold or ledger row - the lock is a hint for the NEXT caller's
resolution - and both are listed in the dataplane write allowlist on their own
(`capacity_exhausted_mark`), not under the money entries. See `ops/capacity.md`.

## Overflow money

The overflow child (`application.call.overflow`) is an ordinary metered cycle on its own hold
(`{call_ref}:overflow`): `_platform_reserve` at the route's aggregator price, `_platform_settle` with
`observed_override` = the aggregator's in-band charge - the caller pays exactly that, 0% markup -
and `cost_source: "aggregator"` + `served_via` in the ledger `meta`, so `reconcile` needs no join.
`OverflowSpend` (per aggregator per UTC day) is updated inside that same settle transaction; it is
accounting for the $20/day budget, not a balance. Shadow mode places no hold and charges nothing.

## Per-success response rules

HTTP 200 alone does not prove a billable success. `settle.py` checks the routing adapter first,
then the catalog's `expect` rule (`{json_path, equals}`), and otherwise falls back to the estimate.
`store.py` inherits provider-file `expect` rules onto endpoints so new routes retain the provider's
success convention. An undecidable rule does not imply a free call.

Coverage remains a catalog concern: providers without an adapter or `expect` can still return
embedded errors. In particular, verify TikHub's success convention before adding a file-level rule;
its existing explicit charge/no-charge prose handling is a separate billing signal.
