---
title: Money - prepaid balance, the ledger, Stripe, and the reports that check it
status: shipped
sources:
  - src/treg/policy.py
  - src/treg/catalog/tavily.yaml
  - src/treg/catalog/tinyfish.yaml
  - tests/test_tinyfish.py
  - src/treg/domain/money/__init__.py
  - src/treg/domain/money/settlement.py
  - src/treg/domain/asynctasks/__init__.py
  - src/treg/models.py
  - src/treg/application/billing.py
  - src/treg/routers/billing.py
  - frontend/src/state/billing.js
  - frontend/src/state/data.js
  - frontend/src/pages/TeamPage.vue
  - src/treg/application/enterprise_billing.py
  - src/treg/routers/enterprise_sso.py
  - src/treg/application/call/idempotency.py
  - src/treg/application/call/intake.py
  - src/treg/application/call/resolve.py
  - src/treg/application/call/octen.py
  - src/treg/application/call/service.py
  - src/treg/application/call/async_bridge.py
  - src/treg/application/call/route.py
  - src/treg/application/call/reserve.py
  - src/treg/application/call/settle.py
  - src/treg/application/call/cost.py
  - tests/test_catalog_v2_runtime.py
  - src/treg/catalog/tomba.yaml
  - src/treg/application/asynctasks.py
  - src/treg/alembic/versions/0017_async_task_record.py
  - src/treg/alembic/versions/0018_async_resource_ownership.py
  - src/treg/alembic/versions/0019_async_poll_failures.py
  - src/treg/application/referrals.py
  - src/treg/domain/governance/budgets.py
  - src/treg/infra/__init__.py
  - src/treg/infra/money_admission.py
  - src/treg/infra/money_session.py
  - src/treg/application/hub/runner.py
  - src/treg/infra/stripe.py
  - src/treg/reconcile.py
  - src/treg/application/key_spend_rollup.py
  - src/treg/alembic/versions/0070_api_key_spend_rollup.py
  - tests/test_key_spend_rollup.py
  - src/treg/domain/referrals.py
  - src/treg/api.py
  - src/treg/application/signup.py
  - src/treg/domain/identity/promotions.py
  - src/treg/alembic/versions/0033_signup_promo_eligibility.py
  - src/treg/alembic/versions/0071_signup_promo_claim.py
  - src/treg/routers/admin.py
  - src/treg/routers/billing.py
  - src/treg/routers/call.py
  - src/treg/routers/orgs.py
  - src/treg/routers/referrals.py
  - tests/test_call_architecture.py
  - tests/test_money_lock_order.py
  - tests/test_money_admission.py
  - tests/test_money_admission_application.py
  - tests/test_money_admission_postgres.py
  - tests/test_archive_batch_postgres.py
  - scripts/bench_money_admission.py
  - tests/test_marketplace_call.py
  - tests/test_anyapi_cost_ceiling.py
  - tests/test_asynctasks.py
related:
  - architecture/catalog.md
  - architecture/proxy-model.md
  - architecture/data-model.md
  - architecture/ads-conversions.md
---

# Money

A catalogued endpoint can be served on **treg's own key**: treg pays the provider and bills the
team's prepaid balance. Two wallets of treg's spend run through this machinery, and only these two:
**tier-4 platform keys** (`TREG_PLATFORM_KEY_*`) and **oauth-billed apps**, providers whose upstream
bills the app owner per use, so even a call on the team's own connection spends treg's credits
(`MarketplaceCall.billed_oauth`; detection and rates in [auth-secrets](auth-secrets.md)). Both run
the same reserve, relay, settle path, share the fail-closed daily cap, and are told apart in ledger
meta by `tier: platform` vs `tier: oauth`. A team's own key on any other provider is never metered.

An endpoint declared `platform_auth: anonymous` is also unmetered: after the own-key tiers miss,
`_anonymous_offer` relays its verified public route with no credential and creates no reserve,
settle or release entry. These calls still pass authorization and any per-member daily call cap
(default unlimited). Sandbox and public-demo teams cannot use the real anonymous fallback.

On an oauth-billed provider a **`free` catalog price is a bug, never a fact**: the upstream charges
whatever the route costs. The estimator falls through to the provider rate rather than reserve
nothing (a `0.0` price must not read as "no price recorded"), and a test asserts the published price
equals the reserved one.

Only `domain/money` writes money. `application/billing.py` owns billing policy, transactions and
webhook orchestration and calls `ledger.topup`; `infra/stripe.py` is the only Stripe SDK user
(signature verification, network); `reconcile.py` is read-only. Billing asks the Stripe adapter to
authorize or verify a payment, then asks the ledger to stage the credit and owns the commit.

## Units: integer micro-USD, everywhere

1 micro = 1e-6 USD. A catalog call can cost a fraction of a cent, so **cents cannot represent one
call** and floats drift when summed. The only float is the margin *rate*, turned into an integer
immediately (`with_margin`). Stripe speaks integer cents (1 cent = 10,000 micro); every crossing goes
through `micro_to_cents` / `cents_to_micro` in `application/billing.py`. Every `*_micro` value has a
display-only `*_usd` twin: **never compute against the USD field.**

## The money tables and the invariant

`Org.balance_micro` (materialized), `CreditBlock` (one funding event and what is left of it), `Hold`
(an open reservation), `LedgerEntry` (append-only journal).

    balance_micro == sum(block.remaining_micro) - sum(open hold.amount_micro)

The balance is a column because `reserve` must be one conditional UPDATE. Every operation writes its
`LedgerEntry` **in the same transaction, synchronously, in-request**. Never route a ledger write
through `audit.py`: it drops rows past its queue bound and swallows exceptions.

**Column width.** A Postgres `integer` holds at most $2,147.48 in micro-USD. Running totals and caps
(`Org.balance_micro`, `daily_cap_micro`, the auto-top-up amounts, `CreditBlock.amount_micro` and
`remaining_micro`, `Hold.amount_micro`, `TagBudget`'s caps, `spent_today_micro`) are `BIGINT`
(revision 0093). Per-row journal amounts (`LedgerEntry`, `CallRecord`, `TagSpend`) stay `integer`:
one row holds one call's or one top-up's amount, which `billing.TOPUP_MAX_USD` keeps far below the
limit, and changing a type there rewrites a very large table under an exclusive lock. A new column
that sums or caps money is `BIGINT` from its first revision.

`AsyncTaskRecord` is the durable owner of an existing hold after a metered asynchronous submission;
it creates no other money movement.

## The six operations (`domain/money`)

| Op | Effect |
|---|---|
| `grant` | new promotional block, balance up (org creation, referral bonus, top-up bonus) |
| `topup` | new purchased block, balance up (after Stripe authorized); committed promptly by billing |
| `reserve` / `reserve_in_transaction` | balance down by the estimate, `Hold` opened |
| `settle` / `settle_in_transaction` | blocks down by the observed cost, hold closed, difference refunded |
| `release` / `release_in_transaction` | hold closed, balance refunded in full |
| `revoke_in_transaction` | a refunded payment's blocks down, balance down by the same, never below zero |

The primitives stage only and never commit or roll back. Commits belong to the application (signup,
billing, the call application) or the two documented exceptions: the lazy stale-hold reap (reserve's
sweep calls the committing `release`, so each old refund stays durable even if the new reservation
returns 402) and the referrals saga checkpoints (claim, stamp, qualify). `tests/test_call_architecture.py`
enforces the no-commit boundary over the real bodies, with a mutation self-check.

Release metadata says which side failed: `provider_failed_<status>` for a provider 5xx,
`call_failed_<status>` when treg got no answer (timeout, connect error, SSRF refusal, failed OAuth
refresh), `not_billable_<status>` for excluded provider statuses. Both failure kinds are usually a
502, and error evidence is purged after `ERROR_EVIDENCE_TTL_DAYS` while the journal is permanent, so
the prefix is the lasting explanation.

A call cancelled after reserve releases as `call_cancelled`. Compensation runs under a shield before
the cancellation is re-raised: it closes an acquired upstream response once, releases the hold and
gives back any pending idempotency label. The release uses the call reference minted before reserve,
because the commit may have succeeded before `reserve` returned; the conditional hold claim makes a
committed hold refunded and a rolled-back reserve a no-op. A release that itself fails is logged and
left to the reaper (`_platform_settle` never raises); the response still says `X-Treg-Cost-Micro: 0`,
but the balance catches up only when the hold is reaped.

**The gate is one statement**:

```sql
UPDATE org SET balance_micro = balance_micro - :est WHERE id = :org AND balance_micro >= :est
```

The WHERE is the check and the SET the debit, so the database decides who gets the last cent.
`rowcount 0` is `InsufficientBalance`, a 402 the agent can act on. No SELECT-then-UPDATE, no
application lock: N concurrent callers against a balance that affords K get exactly K successes.

**Block consumption order** follows `_KIND_ORDER`: the free kinds (promotional, referral, bonus)
first, then `earned`, then purchased, oldest first within a kind. Promo
credit is marketing spend and never refundable; purchased credit is a refundable, disputable
liability, so spending promo first keeps the refundable pool small.

**Margin is applied inside the module** (`with_margin`) at reserve and settle, and the rate in force
is recorded on every entry, so a rate change cannot rewrite what a call cost.

**The hold reaper is lazy**: at the top of reserve, scoped to the calling org. A crash between relay
and settle would otherwise strand money; sweeping one org's stale holds is paid by the caller who
benefits, and needs no scheduler or leader election. Each stale release commits independently before
the new balance gate. Pending `AsyncTaskRecord` holds are excluded; their worker always closes them.

### Lock ordering

`_consume_blocks` locks `CreditBlock` rows through `_lock_blocks` with `ORDER BY org_id, id FOR
UPDATE`, refreshing objects already in the identity map so a stale read cannot overwrite a
concurrent deduction; consumption priority is applied after locking. A single settlement updates
Org only when the refund or daily-spend delta requires it; there is no general Org lock.

Several closes in one transaction must use `close_holds_in_transaction` with `HoldClose` items: it
claims all Hold ids in sorted order before any block or balance lock, locks all needed blocks once,
and applies closes in Org id order. Querying blocks again after a balance update could lock a newly
added block in reverse order. Duplicate Hold ids close once. Enter it before other money writes in
the transaction. `close_deferred` uses it, then writes archive-use marks
(`archive.note_org_uses_in_transaction`) in `(org_id, key_hash)` order in the same transaction.
`settle_to_in_transaction` locks payer and payee Org rows in id order with `FOR NO KEY UPDATE`;
referral payouts stage grants in Org id order. Real PostgreSQL tests exercise these compositions;
they do not cover arbitrary transactions that write other tables first, and lock ordering does not
bound how long a slow transaction holds a lock.

`infra.money_trace` attaches transaction identity and stage labels to money entries without extra
queries, so slow-transaction snapshots can tie a PostgreSQL blocker PID to an open money transaction
([data-model](data-model.md#product-analytics-writer-analyticspy)). Tracing changes neither lock
order nor accounting.

Lock order does bound which rows wait, not for how long. Every transaction a `money_session` begins
sets `idle_in_transaction_session_timeout` (`IDLE_IN_TRANSACTION_TIMEOUT`): a process that stops
running between two statements (an event-loop stall) is disconnected by Postgres, its transaction
rolls back and the team's org and credit-block rows are free again. The stalled request then fails
as on any database error; a rolled-back settle leaves its hold open for the reaper, so each hold is
still closed exactly once by its claim.

### Optional admission before the settlement session

`infra.money_admission.admit` optionally queues same-org balance work by payer Org before the
application opens a database session, using a process-local lock plus a token-owned, expiring,
renewed Redis lease (multi-org batches lease in sorted id order). Waiting holds no database
connection. The lease spans session acquisition, ledger work, commit or rollback and session cleanup
(`infra.money_session.money_session` joins close/rollback even under repeated cancellation). One
admit wraps the session; nothing inside calls `admit` again. It changes no commit boundary.

- **Operations** (by payer identity, which the application already knows from `MarketplaceCall`,
  `DeferredSettle`, the async task snapshot, the Hub caller or the reserving org): `reserve`,
  `close`, `deferred`, `async`, `hub`, `release`. Grants, top-ups, signup credit, referral payouts
  and auto-top-up do not admit; a Hub payee is not an extra key. Skip-direct overflow reserves no
  parent, so the authenticated caller's org is required before its child reserves and before its
  cancellation cleanup. `require_balance_orgs` refuses an empty, missing or non-positive org before
  checkout on every session that touches a balance row.
- **Only `reserve` may shed load.** When its budget (`money_admission_wait_s`) expires it releases
  partial gates and raises `ReserveAdmissionTimeout`, which `reserve.reserve_admission` maps to a 429
  (`money_admission_timeout`, blame `treg`) with a randomized `Retry-After` between
  `money_admission_retry_after_s` and twice it. No hold or upstream call starts. Every other
  operation, on a missing identity, unavailable Redis, store error, lease loss or expired budget,
  releases partial gates and runs the original database path; it never raises, because settlement
  and cleanup must run.
- **Wait bounds.** Non-reserve operations wait `max(money_admission_wait_s, 2 * lease)` (a dead
  holder keeps its lease for one TTL), but never more than half of `hold_grace_s`, and never past
  the reaper age of the oldest affected hold: the call records a monotonic `reserved_at` before
  reserve on `MarketplaceCall` and each `DeferredSettle` (the Hub runner does the same for its price
  hold), and close, deferred batches and cancellation pass `max_wait_s = hold_ttl_s() - oldest hold
  age - hold_grace_s / 2`. The async worker passes what remains of the row's processing deadline. A
  settlement that enters the database without the lease queues on the CreditBlock lock while
  holding a pooled connection, which is why these bounds exist.
- Renewal and deletion are owner-checked, so an old holder cannot extend or delete a newer owner's
  lease. TTL cannot prove an old database transaction has stopped, which is why database locks stay
  the correctness mechanism. Fallbacks preserve correctness but not isolation and are counted
  separately.

A holdless free call (below) never admits: it touches no balance row.

This is best-effort contention isolation, not a global money concurrency cap, durable queue or FIFO.
Serializing the whole session can lower hot-org throughput, and Redis round trips add latency.
Default off; `money_admission_org_ids` selects an opt-in subset (all orgs when empty and enabled).
Rollout and measurement: [deployment controls](../ops/deploy.md#optional-money-admission). Timing
details live in `infra/money_admission.py` and its tests.

## Signup credit eligibility

`application.signup._grant_signup_promo` calls `identity.promotions.claim_signup_promo` for the
creating user. A conditional UPDATE consumes `User.signup_promo_available` only for a verified,
active, non-demo user, then inserts the user's inbox into `SignupPromoClaim`, whose primary key
refuses a second claim for one mailbox. It commits with `ledger.grant(once=False)`; a failed commit
rolls everything back. Meta records `source=signup` and the `user_id`. The per-org
`grant(once=True)` check is not a concurrency arbiter.

`User.email_verified_at` is set only by successful OTP, verified social login or an inbox-only
invitation link; legacy `/users` and admin-visible invite codes prove nothing. An unverified account
may create teams with zero credit. The sign-in that first verifies it
(`find_or_create_user(..., proven=)`, then `signup.grant_signup_promo_on_proof` after that commit)
offers the claim to the oldest eligible team it owns, or its first eligible team, without a second
ad conversion. A failed or interrupted grant leaves the user eligible for their next team. The amount
is `promo_grant_micro`; zero skips the claim as well as the credit.

Before claiming, the `signup_credit` decision point ([policy extensions](policy-extensions.md)) may
`defer`, which writes nothing and leaves the user eligible. Without an extension, or when one is slow
or failing, it grants.

Eligibility lives on the user, so team deletion, leaving and ownership changes cannot restore it;
revision `0033` defaults historical users to ineligible and new users start eligible. The inbox is
`promotions.promo_inbox(email)`: lowercased, trailing domain dots and `+tag` dropped for every
domain, and for `gmail.com` / `googlemail.com` dots removed and the domain folded to `gmail.com`.
Aliases remain separate logins; only the gift is shared, and a user whose inbox was already claimed
loses eligibility in the same UPDATE. The claim row has no FK and outlives the user, so account
deletion does not free an inbox. This stops one mailbox verifying many accounts, not someone holding
many real mailboxes. Revision `0071` seeds the table from every verified, no-longer-eligible user and
is a rollback floor: dropping it would reopen a gift to every alias of an already-gifted inbox.

## Deferred asynchronous settlement

`domain/money/settlement.py` is the single data-derived calculation seam. Reserve freezes a basis
with `when: response|terminal` and `amount.kind: table|usage|observed|terminal_presence`;
`settle(basis, evidence)` returns integer micro-USD and never writes the ledger. Provider differences
stay in catalog YAML. `table_amount_micro` bounds a `times` multiplier by the field's declared range
(always positive): an out-of-range value prices at the fallback.

- An async row settles at the terminal answer when its price is a table, a `usage` meter, or a plain
  `per_call` price (`observed`, fixed price as fallback). A provider that bills on submission has
  its reported charge frozen into the basis (`submission_charge_micro`), used when the terminal
  answer reports none, `billed_failure` included.
- Async `per_success` rows without a table settle on the submission response unless they declare
  `settle: terminal_presence` (catalog.md). An optional `presence.request_field` marks a supplied
  billable input at submission. A missing or malformed terminal result settles the reserve; a
  confirmed terminal failure releases.
- `terminal_on_submission` with a success word already present is not deferred.
- `settle: usage` reserves what the rate-card table says THIS request costs and settles the reported
  figure, which may exceed the reserve: the ledger takes the difference, the next reserve is the
  gate, and `reconcile.async_task_settlement` lists every `overrun` and every
  `absorbed_shortfalls` settle (`block_shortfall_micro > 0`). A success with no usage figure settles
  at the reserve with `reconcile_review` and an ERROR alert, never at the ceiling. The unit's
  micro-USD worth is frozen as `amount.unit_micro` at reserve, so a later rate edit never re-prices a
  task in flight; a non-USD basis without a frozen rate settles at the reserve.

For a tier-4 endpoint carrying `async`, a successful submission keeps its hold and writes an
`AsyncTaskRecord` whose `settlement_basis` freezes the price rule with its request, so settlement
replays from the row alone. BYOK creates neither hold nor row. A caller poll and the worker share
`_finish_terminal`: terminal 2xx evidence settles the task once under its row lock; an unexpired
success or billed failure without required usage only records result ownership; a failure or a
state past the window releases. Only the winning finalizer archives terminal evidence. The same
transaction stores the adapter hit verdict and verdict word on the task; the audit path copies them
to the submission row whichever is written first. A 2xx that is not an accepted submission
(`_submission_rejected`: not JSON, fails `expect`, no task id or an off-allow-list poll URL) never
becomes a row and settles at the provider-reported charge, else zero. If the row cannot be
persisted, the request path releases with `async_task_not_recorded` and an ERROR alert.

Routed tools and Enrich Arena wait through the shared async bridge; every poll still uses the ordinary
call path, so BYOK stays unmetered and platform polls enforce ownership. Server-created polls
continue the admitted submission without another quota increment. A foreground timeout returns a
pending result with the reservation open, and the worker later settles or releases it.

The worker selects due rows, takes provider/global concurrency slots, then claims each still-due row;
`attempts` fences stale workers, and `CLAIM_LEASE_S` exceeds `PROCESS_TIMEOUT_S`. Polls have a
`POLL_TIMEOUT_S` total deadline including body consumption, use the normal credential injector (a
static poll's parameter rides as `query_items` or a declared, percent-encoded path substitution; the
relay forwards no URL-embedded query), cap the body at `MAX_POLL_BODY_BYTES`, and take terminal
evidence only from a 2xx. HTTP errors, invalid JSON and timeouts back off
(`next_failure_check`: 2, 4, 8, then 15 minutes); there is no provider-wide circuit breaker. The
worker never lets one row abort a tick, and loads the catalog once in a thread before polling so the
poll timeout measures the provider alone. **At the deadline (`MAX_AGE`, or the descriptor's shorter
`max_age`) it releases the hold in full**, marks the row `timed_out` with `reconcile_review` and logs
an ERROR: an outcome nobody observed is the platform's cost, never the customer's, and appears as
`absorbed_timeouts` in `reconcile.async_task_settlement`.

Platform poll and fetch calls are authorized against the caller org's row and any membership pins.
`defer_submission` freezes effective tags on the task and `_remember_resource` propagates them;
missing legacy tags fail closed for pinned readers. An authorized platform poll with an explicit
`free` price and zero estimate is `MarketplaceCall.free_owned_poll`: it skips `_platform_reserve`
and `_platform_settle` entirely (no new Hold, LedgerEntry or TagSpend) while authorization, limits
and rate smoothing still apply. This does not cover fetch utilities, BYOK, billed OAuth or a zero
estimate on a paid endpoint. An overflow child carries its own `observed` basis at the aggregator
price.

Any other direct platform call with an explicit `free` price and zero estimate is
`MarketplaceCall.holdless_eligible` unless it is billed OAuth, a routed child, an owned poll, an
async submission, a resource producer or usage-settled. For an org listed in
`holdless_free_org_ids` (default `[]`: none; the money queue also brakes free bursts, so this rolls
out per org until an explicit free-call rate limit exists) and with no caller tags (`X-Treg-Meta`
or a pin), `_platform_reserve(allow_holdless=True)` sets `mk.holdless` instead of reserving: no Hold,
LedgerEntry or TagSpend, no money admission, no balance-row lock. Only the platform daily-cap read
runs, so a team past its cap is still refused. `_platform_settle` then writes nothing; a computed
charge is logged and absorbed. A tagged call keeps its $0 hold because its settled tag rows count
toward `calls_per_day`. Overflow children always reserve, since their aggregator spend is recorded
in the settle transaction; `_child` never inherits the parent's flags. Cancellation of a holdless
parent releases nothing unless `mk.overflow_hold_possible` (set before the child reserve starts),
in which case it releases only `{call_ref}:overflow`.

The audit row froze the reserve as `cost_charged_micro` at submission, so displays must not read it
alone. `application.asynctasks.views_for` joins the org's tasks, loads archived terminal JSON and
derives the artifact with `domain.asynctasks.artifact`; `/calls`, `/calls/{ref}`, `treg audit`, the
dashboard Activity page and the CLI's `--await` all read this one view.

## Stripe (`application/billing.py` and `infra/stripe.py`)

**Credit happens on the WEBHOOK, never on the browser's return from Checkout.** The success redirect
is a URL the payer controls. The one exception is the off-session auto-top-up charge, where the
server itself holds the PaymentIntent's confirmed status; it credits immediately and the webhook
redelivery is a no-op.

The webhook is `POST /billing/stripe/webhook` and **404s when unconfigured**, so a deploy without the
secret exposes no unauthenticated POST surface. `verify_event` uses the SDK's `verify_header`
(timestamp tolerance is replay protection, and it handles several signatures during rotation) rather
than `construct_event`, so a genuine event of a type this SDK version predates is accepted and
ignored, not rejected as forged. A handler failure returns 500 **on purpose**: that is how Stripe is
told to retry.

The Stripe SDK is synchronous, so `infra/stripe.py` runs every call on a worker thread, and converts
results to plain dicts (`StripeObject.to_dict()`): SDK objects no longer subclass dict, and test fakes
return dicts through the same funnel, so a consumer written against the object API passes tests and
breaks prod or vice versa. Keep every consumer dict-style.

**Idempotency on `topup` is enforced by the database.** `stripe_payment_intent` is UNIQUE, and `topup`
flushes its INSERT inside a SAVEPOINT before the balance moves: the loser of a race rolls back only
that savepoint and re-SELECTs the winner's block. The loser's flush blocks until the winner commits,
which is why the caller must commit promptly after `topup` returns. The application-level SELECT is
an optimisation, not the guarantee.

On the `fresh` (first credit) branch, `_credit` also:

- emits `topup_completed` (`analytics.capture`), which is synchronous and swallows errors by
  construction: a raise would 500 the handler and make Stripe retry an already credited payment;
- queues a `paid` Google Ads conversion when the org has a click. This is a separate commit after
  the credit, deliberately: the credit must stand whatever happens after it, so a crash between the
  two loses only the conversion ([ads-conversions](ads-conversions.md));
- passes normalized product attribution (`entry_surface`, `checkout_source`, fixed surface names,
  `unknown` when missing) from Session and PaymentIntent metadata into ledger meta and the event.
  No query inputs, URLs, keys or provider results are copied.

### Top-ups, invoices and auto top-up

**Every top-up has an invoice.** Manual Checkout sets `invoice_creation`. Automatic top-up creates a
USD draft invoice for the existing Customer, adds one item, finalizes with `auto_advance=false`
(which disables Stripe's retries and dunning) and pays off-session; pending items and inherited
discounts are excluded so the invoice charges exactly the refill. Each write has a deterministic
idempotency key derived from the credited-purchase count and invoice generation, unchanged across
midnight or an uncertain Stripe reply.

Before a new charge, `attempt_auto_topup` pages the customer's invoices (listing, not Stripe's delayed
search index) since one day before the last credited automatic top-up, or the full history before
the first, to recover a draft, open or paid attempt:

- an unfinished automatic invoice with an older base key blocks while its payment may settle; a paid
  invoice whose PaymentIntent has no ledger credit also blocks;
- an open invoice whose PaymentIntent failed or was canceled is voided first; one requiring
  authentication is voided when the saved card changed; a `processing` payment stays open and
  blocks. Finalization's `requires_confirmation` PaymentIntent is safe to pay with the existing key;
- an incomplete lookup counts toward the consecutive-failure limit without changing the key; at the
  limit auto top-up pauses as `stripe_unconfirmed` with a banner and email. A confirmed automatic
  payment (synchronous credit or webhook) re-arms it; a manual payment does not, and a manual
  top-up does not advance the lookup boundary.
- a Stripe refusal naming the payment method charged nothing: the card is forgotten and the policy
  waits as `no_card`.
- an authentication-required invoice is voided only after Stripe confirms the failure and the void;
  auto top-up switches off and the customer is sent to a manual Checkout with bank verification. A
  successful manual payment re-arms a policy paused for authentication; a delayed failed-payment
  webhook for an already-void invoice cannot disable a re-armed policy. If the void cannot be
  confirmed, the policy pauses as `stripe_unconfirmed`.

The cooldown, cap, ledger crossing, last automatic credit and billing email are read first; the
database transaction is then released, and no Stripe list, create, finalize or pay call holds a
connection. An invoice-created PaymentIntent does not inherit invoice metadata, so a metadata-free
payment webhook checks the customer against local orgs before `_invoice_metadata_for_payment`
resolves the invoice through `InvoicePayment.list`; the PaymentIntent's ledger uniqueness makes the
later path a no-op.

Stripe emits `invoice.created` / `invoice.paid` for every top-up; `handle_webhook_event` drops them,
because crediting on the invoice as well as the PaymentIntent would be a second door onto the same
money. Invoice creation may be priced separately by Stripe, and Stripe invoice emails depend on the
dashboard's "Successful payments" customer-email setting; treg's receipt email is independent.

The invoice address needs `billing_address_collection="required"` **plus**
`customer_update={"address": "auto", "name": "auto"}` on the same session: the first asks, the second
persists the answer onto the Customer. Collecting without `customer_update` stores nothing.

**A promotion code discounts the price, it does not bonus the balance.** `allow_promotion_codes=True`
puts the field on Checkout, and the webhook credits `amount_total`, what Stripe collected. A 100%-off
code collects nothing and is dropped as `zero amount`. Grant free balance through `ledger.grant`,
never a Stripe coupon.

**The top-up bonus is a separate `grant`, manual only.** `topup_bonus_tiers` (`{min_usd: percent}`)
gives a manual top-up a `bonus` block at the highest tier at or below the amount (`bonus_for_topup`,
integer arithmetic), granted inside `_credit`'s `fresh` branch so a redelivery grants nothing. It is
its own block that burns with promo credit, so the purchased block stays exactly what the card paid:
purchased credit is a refundable liability, the bonus is marketing spend. Automatic refills earn
nothing (a bonus there would be a permanent margin cut on every refill). A refund or dispute does not
reverse it on its own: a refund revokes the unspent bonus in proportion to what was refunded
(see "Refunds revoke unspent credit"); a dispute only logs it for a human (`bonus_blocks_flagged`). The grant's meta carries `payment_intent`, `pct` and `topup_block_id`, which
`topup_history` joins to show `bonus_micro`.

**The preselected amount** (`next_default_usd`) is the first preset above the org's last *manual*
top-up, capped at `topup_default_cap_usd`; auto refills are skipped so the ladder cannot ratchet on
its own. `POST /billing/topup` with no amount uses it. The auto-top-up threshold is validated
separately (`validate_threshold_usd`): it is not a charge, so the top-up minimum must not apply to it.

**A saved card arms a consented policy from either webhook.** Consent is recorded first
(`set_autotopup` to `no_card`) and the top-up Checkout saves the card; `_set_default_pm` (from
checkout, setup and payment webhooks) runs `_arm_if_waiting_for_card`. It re-arms from `no_card`
once a card exists, and from repeated declines only when the card is proven (it just paid, or is
newly saved). A deliberate off (reason `None`, consent on file) stays off; a redelivered webhook must
not switch it back on.

**Auto top-up is guarded in depth**: recorded consent (the PSD2/SCA mandate, a compliance
requirement), a monthly cap, a cooldown stamped in the DB *before* the charge, a consecutive-failure
limit, and an idempotency key from the threshold crossing, so a burst of calls that all notice the
low balance produces ONE charge.

- The stamp is a compare-and-set on `autotopup_last_attempt_at`: `_lock` is per process, so only the
  first process to match the value it read proceeds and the rest return `cooldown` before any Stripe
  call. Without it, losers got 409s that counted as failures and switched auto top-up off while the
  real payment went through. A 409 that still arrives (`stripe_busy`) is logged, not a failure.
- The monthly cap counts automatic top-ups only (`monthly_autotopup_spend`) and refuses a charge that
  would pass it (`autotopup_cap_reached`). A stored 0 resolves to `TOPUP_MAX_USD`; a cap below one
  refill is refused.
- The wait between charges (`billing.autotopup_wait_s`) is `autotopup_cooldown_s` after a failed
  charge, else an hour divided by the team's `autotopup_max_per_hour` (0 means
  `autotopup_default_per_hour`). The number is part of the mandate text.
- A call refused for balance also calls `maybe_schedule_autotopup` (one primary-key read), which
  checks the wait in memory first, so a team at $0 refills without starting a task per refused call.
- The inline credit emits `topup_completed` (`auto: true`); the following webhook emits nothing.

The 402 carries `autotopup_enabled` and an `auto top-up:` line: off gives the command that turns it
on; on gives amount, threshold, charges per hour, monthly cap and the flags that raise the refill
rate, because a team out of money with auto top-up on is being held by the cooldown or cap. The org
fields are read **before** the reservation transaction, whose rollback cannot be a source afterwards.
The MCP path scrubs the payment link from the body (ChatGPT digital-goods rule); the auto top-up line
survives because it names a CLI command, not a URL.

### Refunds revoke unspent credit

A refund is made in the Stripe dashboard; treg follows it. `charge.refunded` hands the charge's
cumulative `amount_refunded` to `billing.revoke_refunded`, which names the payment's purchased block
and the bonus block its grant meta points at, and stages `money.revoke_in_transaction` with one
cumulative target per block (the bonus target is `bonus * refunded // paid`).

- **Only that payment.** Revoke locks the named blocks in id order, then the Org row (`FOR NO KEY
  UPDATE`), the order settle takes them in, and never touches another block, so a refunded $20
  refill leaves the $10 top-up beside it whole.
- **Never below zero.** A take is bounded by the block's remaining credit and by the balance read
  under the Org lock. What is not taken is `shortfall_micro` and is logged; the part still on the
  block, held by calls in flight, is `held_micro`, which the operator's re-run takes once they
  finish. Rare: it needs more in flight than the rest of the balance at the moment of refund.
- **Cumulative, so idempotent.** What each block has given up is read from its own `revoke` entries
  under the block lock. A redelivered or out-of-order event finds nothing owed and writes nothing;
  a second partial refund takes the difference. An entry is written when something is taken or the
  refunded total rises, so a refund of fully spent credit is still on record.
- **Auto top-up goes off** (`payment_refunded`) in the revoke's own transaction when the refunded
  total rises above every earlier one: the revoked balance can sit below the threshold, and the
  next call would charge the refunded card again. Collecting held credit later is not a new refund
  and leaves an owner's re-enabled auto top-up alone. It never re-arms by itself.
- **The operator's retry** is `treg-worker admin revoke-refund <payment_intent>`
  (`revoke_refunded_payment`): it reads `amount_refunded` from the PaymentIntent's latest charge and
  runs the same use case, for a missed webhook or held credit.
- A refund that later fails in Stripe leaves the credit revoked; there is no entry that reverses a
  revoke, so a human grants it back.
- Disputes stay out: a won dispute returns the money, and there is no entry that gives revoked
  credit back.
- History rows carry `refunded_micro` (the largest refunded total the block's revoke entries
  recorded), which is what the card got back, not what was revoked. `topup_refunded` reaches
  PostHog with both.

### Who sees what, history and portal

`_billing_org` (every `/billing/*` route) requires **admin or owner**: cards, spend policy and
invoices are the org's money. `GET /orgs/{id}/balance` shows any **member** the figure and in-flight
holds (a machine identity must read the balance it spends, and every 402 already carries
`balance_micro`); credit blocks and the ledger stay admin+.

`get_payment_history` builds top-up rows from our own `CreditBlock` rows, so history can never show a
payment the balance disagrees with. Stripe is asked only for links, in two list calls (`Charge.list`
+ `Invoice.list`), and a failure degrades to link-less rows with `stripe_ok: false`. Both windows cap
at 100 payments; the portal is the unbounded archive. SSO subscription invoices
(`_subscription_invoices`: the team's customer invoices whose subscription metadata names this team or
whose id matches its current subscription) use Stripe's total, status and links, never add to the
balance, and report failure as `subscription_invoices_ok: false`. A `kind` filter and per-source
cursor build one newest-first page.

`create_portal_session` is the self-serve surface for card, address, tax ID and invoices (hosted,
because tax-ID rules go stale per country). It requires a portal configuration in the Stripe
dashboard and refuses an org without `stripe_customer_id` rather than minting one;
`billing_state.portal` hides the button for such teams.

### Enterprise SSO subscription

- Each offered plan needs a reusable, active, flat, monthly USD Stripe Price
  (`TREG_ENTERPRISE_SSO_BUNDLE_PRICE_ID`; `TREG_ENTERPRISE_SSO_ONLY_PRICE_ID` when
  `TREG_ENTERPRISE_SSO_BUNDLE_ONLY=false`). A pricing change creates a new Price ID for future
  checkouts; existing subscriptions keep theirs unless migrated. Per-plan minimum prices hide and
  reject a cheaper Price.
- `billing.catalog` reads Prices outside any database session. `billing.status` does no Stripe I/O for
  entitled teams and never fetches Prices for non-owners, so a Price API failure cannot hide setup or
  cancellation controls. `billing.start_checkout` rejects a stale Price ID and re-verifies the Price
  before Checkout. One pending reservation per team plus an idempotent Stripe session request stop
  parallel subscriptions.
- The signed billing webhook records subscription status; WorkOS setup and sign-in are gated on
  `active`, `trialing` or `past_due` (Stripe is still retrying). A Checkout redirect grants nothing.
  Subscription metadata binds the event to a team and plan, the Stripe customer must match, and
  older events cannot restore an entitlement after a later cancellation.
- Owners can schedule cancellation at period end. Cancellation removes only SSO-created memberships
  and their keys. After the paid period ends, the cleanup worker deletes every WorkOS connection and
  directory of the team's verified WorkOS organization, retrying failures and blocking new checkout
  and team deletion until done, without holding a connection across WorkOS I/O. Team deletion waits
  until the subscription has ended.
- The subscription never credits or debits the prepaid balance. A paid invoice links its receipt
  when its Charge has one; a missing receipt never hides the invoice.

## The spend ceiling (`application.call.reserve`)

`_enforce_platform_daily_cap` is a per-org, per-UTC-day limit on platform spend, applied only when
one is set. When it applies it is **fail-closed**, unlike the per-user call cap: a query that cannot
answer refuses the call. When none applies, the prepaid balance and the auto-top-up monthly cap bound
spend. An endpoint with an unknown price never reaches this path (`platform_eligible` requires a
computable `usd`, see [catalog](catalog.md)).

`budget_policy._effective_daily_cap` is the team's `Org.daily_cap_micro` when set, else the
deployment's `platform_daily_cap_usd` (0 = no limit, the shipped default). The team sets any figure
through `PATCH /orgs/{id}/settings` (0 = follow the default) and reads it with `GET`
(`platform_default_micro` beside it). Nothing is clamped.

`ledger.spent_today` runs on every metered call inside the reserve transaction, fail-closed, so it is
ONE primary-key read of `Org.spent_today_micro` / `spent_today_day`. `domain/money` keeps that counter
inside the same UPDATE that moves the balance: reserve adds the estimate, settle adds the consumed
amount and removes the estimate, release removes the estimate, each only when the hold was opened
today. The first movement of a new UTC day resets it (`_spent_today_values`). A journal aggregate
cannot replace it: a busy org's rows touch most heap pages of the day, so no index makes the
aggregate cheaper than reading the day. `spent_today_from_ledger` computes the same number for
reconciliation, and `tests/test_daily_spend_counter.py` asserts they agree.

## Checking the work (`reconcile.py`)

Read-only, query-time, no scheduler:

- **`price_drift`**: estimate RESERVED against cost REPORTED on the same `CallRecord`, per endpoint. A
  silent provider re-price turns margin negative with nothing on fire; this is what notices.
- **`provider_spend`** reads the **ledger**, not audit rows, because it is the number held next to an
  invoice and audit rows may be missing.
- **`repeat_rate`**: how much of the bill was the same query twice.
- **`shared_plan_recovery`** (`GET /admin/reconcile/shared-plans`): see "Shared-plan pricing".

Aggregations over the JSON `meta` column run in Python, because portable JSON extraction across
SQLite and Postgres is not worth it for admin-scale windows.

## Spend per API key (`application/key_spend_rollup.py`)

Ledger `settle` entries are the only money; `CallRecord.call_ref` supplies only the key. A routed
attempt (`<ref>:r1`) or overflow hold (`<ref>:overflow`) falls back to the part before the first
colon, and the audit row must belong to the same team. A charge with no keyed audit row (dashboard
session, MCP connector login, hub run, shed audit row) lands on the team's **not attributed** line;
per-key lines plus that line equal the ledger.

`treg-worker spend rollup` folds new settle entries into `ApiKeySpendDay` (team + key, `0` = not
attributed, + tool + UTC day: `spent_micro`, `charges`, where `charges` counts settle entries, not
calls). The fold:

- walks the ledger in `(created_at, id)` order from `ApiKeySpendCursor` (ledger ids are random), with
  the watermark as the scan's lower bound (`created_at >= watermark AND (created_at > watermark OR
  id > watermark_id)`), never inside one OR the planner cannot bound;
- leaves entries younger than a few minutes, because the audit row naming the key is queued after
  the settle;
- reads small batches, each one transaction under the cursor row lock, so overlapping runs never fold
  an entry twice;
- starts 31 UTC days back, capped per run (`--max-rows`); until caught up once, pages say history is
  being prepared.

Once a UTC day, the last two complete days are recounted per team from the ledger and REPLACE that
team's rows (late settles and late audit rows); a difference is logged. The recount is a lease on the
cursor row: a run finding a live lease skips, each team is replaced in one transaction that re-checks
the lease and records progress, and `--max-recount-seconds` bounds one run.

## Call settlement and provider evidence

The metered path is `_platform_offer`, spend caps, `ledger.reserve_in_transaction`, application
commit, relay, then settle or release. The request session must be committed before relay
([connection discipline](proxy-model.md#connection-discipline-a-call-in-flight-holds-no-db-connection)).
Two pure functions hold the arithmetic: `resolve.price_call` (estimate, units, frozen basis) and
`settle.decide_settle` (charge or release, and how much); the replay engine calls the same two
([replay](replay.md)).

Settlement prefers response evidence through `_observed_cost_micro` and falls back to the estimate
only when evidence is unavailable; **the estimate is never a substitute for an available
response-derived charge.** Generic evidence is declared on the catalog row (`reported_charge`, charge
headers, `settle: usage | count | quantity | modifiers | base | terminal_presence`, an `expect` rule,
a declared `miss`; [catalog](catalog.md#request--and-response-derived-billing-declarations)).
Provider-specific readers in `settle.py` and `resolve.py` (row counts, result presence, credit fields
in the body) exist only where the generic declarations cannot express the provider's bill; each such
row's cost `note` states the rule. Their shared rules:

- A response-derived row count is capped at the hold. An empty list is free where the provider bills
  per returned row; a documented minimum (one credit on an empty page) is honoured.
- Missing, malformed, negative or non-finite evidence keeps the frozen estimate for reconciliation;
  it never settles as free and never as the ceiling.
- Rates and credit conversions are frozen when the call resolves, so a later fx edit cannot re-price
  it. `MarketplaceCall.unit_micro` is the raw per-row price, which on a `per_success` row is the whole
  call, so a response-reported credit COUNT settles against `reported_charge_unit_micro` (one frozen
  credit), never `unit_micro`.
- A balance or account read (`remainingCredits`, a wallet total that updates late) is never per-call
  evidence.
- BYOK calls never enter this path.

**Sizing the hold.** Without `cost.reserve`, the row-count signal (`resolve._LIMIT_PARAMS` /
`_body_limit`) reads the caller's page-size fields in query or body (including camelCase spellings and
a nested `pagination`), or for providers that bill per listed item the length of the target array;
with no signal it is a 20-row page. A cap key only belongs there if the provider honours it as the
spend cap; otherwise the reservation follows a cap the bill ignores. A `per_result`/`quota_rows` cost
whose unit is an input entity (`resolve._ENTITY_UNITS`) is counted by `_entity_count` (repeated or
comma-separated query values, a body array including a JSON-RPC `params`, one per task object, else
one; capped at 10,000, not the page max), because on those providers nothing reports a cost
afterwards: the reserve IS the charge, and a wrong count is a wrong bill. A row declaring
`cost.reserve` skips that inference and its counted settle is capped at the hold.

`_platform_settle` uses its own short session and never turns a served response into a 500. A pool
timeout or PostgreSQL deadlock (`DBAPIError` with `orig.sqlstate == "40P01"`, never message matching)
gets one retry of the whole settlement/release transaction, including overflow spend, in a fresh
session. A second failure leaves the hold to the reaper and preserves the upstream response. No
upstream retry or extra ledger operation is introduced.

### What is never billable

- **A 429**, under any cost type: capacity refusing a request is not the caller's bad input, and on a
  shared key it is treg's own saturation.
- **A relayed 405**: `_resolve_marketplace_call` rejects a caller method mismatch before relay, so a
  provider 405 indicts treg's recorded method (`_NOT_THE_CALLERS_FAULT`).
- **A caller-input 4xx (400/404/422) under `per_call`** bills only a non-zero charge the vendor itself
  reports in the body; otherwise it releases as `rejected_unbilled_<status>`, because a rejection
  served nothing (`test_a_4xx_bills_only_what_the_provider_reports`).
- **A platform-tier 4xx that `signatures.classify` reads as OUR account running dry** releases as
  `capacity_<kind>`, whatever its status; otherwise the caller would pay for treg's exhausted account,
  and pay twice once overflow serves it (`tests/test_capacity_overflow.py`).
- **HTTP 200 alone does not prove a billable success.** `settle.py` checks the catalog's `expect`
  rule first, then the routing adapter, else the estimate; a value in `expect.misses` settles at zero
  but stops the hit verdict calling a free miss a failure. `store.py` inherits provider-file `expect`
  rules onto endpoints. An undecidable rule does not imply a free call.

## Rows in the v2 spelling

A row in the v2 spelling (architecture/catalog-schema.md) is priced and settled by
`application/call/cost.py` through the same two seams. `price_call` returns its hold and its cost
terms, frozen at resolve (`Pricing.terms`, carried as `MarketplaceCall.cost_terms`: the unit price,
the reported unit's rate, the plugin rates and the row's outcome and bill rules), and
`decide_settle` follows the v2 settlement order; no provider-specific settlement rule reads a v2
row.

- **The hold.** `price` gives the price of `per` units in its `currency` at the rate frozen at
  resolve: `value`, or the first `table` row whose `when` holds on the request (input defaults
  applied, values coerced by their declared type), times a bounded `times` field, else the
  `fallback`; `rates` prices plugin quantities only, so the schema requires a `reserve.plugin` beside
  it and refuses `bill.miss` in units. `reserve` gives the units: `rows` (the caller's integer clamped to `[1, max]`, else
  its default; with no default treg's key refuses a request without it), `entities` (list items,
  comma-separated query values and scalars, added up, from one to `max`), or a `plugin` quantity
  priced at the frozen rates and capped at `max`; absent, one unit. A reserve plugin that raises
  or returns no quantity refuses the call before any hold (`catalog_price_invalid`, 503). `round:
  up` bills whole `per` blocks, in the hold and in a count. Amounts round up to whole micro-USD.
- **The order.** No answer, a 3xx and a 5xx release. A 4xx releases unless `settle.reported` reads
  a charge above zero and the status is not one of `_NOT_THE_CALLERS_FAULT`; then that is the bill.
  On a 2xx, `bill.free_when` makes the call free; a readable `settle.reported` (after `scale`,
  zero included) is the charge, uncapped; otherwise the outcome picks `bill.error` or `bill.miss`
  (free by default, `charged`, or a number of units at the unit price capped at the hold) and a hit
  is charged. A free outcome with no settle evidence observes a charge of 0, as a row in today's
  spelling records a free miss, so the call record and the stats fallback read it alike. A charged answer costs its `count` (non-null items) or `plugin` quantity capped at the
  hold, its `usage` meters, which may exceed it, or the hold when there is no `settle` or no
  evidence. A `count` path is unreadable, so the hold, when it does not select a list: for a path
  with `*` or a filter, when any list one of its `*` or filter steps iterates is missing or not a
  list (an empty one counts 0). An answer that would be billed is released when it carries a
  capacity signature.
- **The frozen basis** keeps the vocabulary both spellings share, so either settles through the
  same ledger code: a usage settle freezes its meters as today's `usage` basis, every other v2
  settle is `observed` with the hold as its fallback. An async v2 row's terms ride its persisted
  basis (`cost.persisted`); the worker settles a terminal answer through `cost.settle_terminal`: a
  `billed_failure` by its `settle` form, a `success` by the order from `free_when` on, with a
  charge the provider reported on the submission standing in for a missing one.
- The served `cost` of a `rates` row carries the reserve `max` as its `fallback`, the most one
  call is held at, so eligibility on treg's key reads a price for it.
- `MarketplaceCall.unit_micro` is one `price.value` (a table's fallback) and
  `reported_charge_unit_micro` one frozen credit, as for a row in today's spelling; quotes, the
  access check and the arena price a v2 row through `cost.price` as well.

## Pricing a cached hit

A hit from the archive settles through the same hold as a live call. Per team and question
(`ArchiveKeyOrg`, written inside the settle transaction) a team's first billed call pays full price
whether the vendor or the archive answered; from its second, a hit settles at
`archive_hit_repeat_price_percent` of the RAW amount (floor division), then margin. Another team's
first hit is full price. The settle meta says `cached: true` and `cache_price_percent`. Own-key hits
are never metered. No new entry kind: the hold settles for less and the rest is released.
Detail in [archive](archive.md#pricing-a-hit).

## Shared-plan pricing: flat-fee providers, and the rate treg sets

| The provider sells | The price of one call |
|---|---|
| real credits | vendor price / credits (the normal fx entry) |
| a monthly request cap | fee / cap |
| a rate limit only | fee / theoretical max is the FLOOR; the rate sits above it at a stated break-even |
| unlimited | a treg-set rate with the break-even printed |

**We never claim the last two are vendor prices.** treg sells its own service (custody, the key, a
share of the rate limit) at a published rate with fee and break-even beside it (`kind:
treg_shared_plan`, enforced by `check_fx`). The price is also congestion control: at $0 one looping
agent exhausts a shared rate limit for every team. Mechanically it is a credit provider whose credit
is "one call on treg's plan"; holds, caps and settlement are unchanged.

`shared_plan_recovery` reports fee versus collected per treg-set rate, scaled to the report window,
with `suggested_usd = fee / measured calls` and an action at plus or minus 50%. It REPORTS; a human
edits fx.yaml monthly, because a rate card that moves on its own under an agent is not a rate card.
`price_drift` never sees these providers (they report no charge); a test fires if an observed-cost
parser is ever added for one, because drift would then police a price treg set.

**Trial pools** (`kind: treg_trial`): $0 on treg's free-tier key, capped per team per UTC day by
`_enforce_trial_allowance` (successful platform calls with a non-free catalog cost only, fail-closed,
429 `trial_allowance_reached` with a connect-your-own-key hint). Free tools, failed calls and own-key
calls never burn it; another org's usage never touches this org's pool. A hot pool is the buy signal
for the provider's commercial tier. The count reads the team's whole audit day, so a team found at
its allowance is remembered per process until 00:00 UTC, keyed with the team's creation time,
and refused without counting again (a day's count only grows). Its `used_today` is then a lower
bound, as the audit lag already made it.

## Idempotency and retries

`application.call.idempotency` keeps a lost successful response from causing a second upstream
charge. It must replay the response, not just remember the charge; skipping only our debit would
still pay the provider twice.

- The caller supplies `Idempotency-Key` on `/call/`, or `idempotency_key` over MCP. No label means no
  deduplication. Never derive a label from request contents: identical requests can be new work.
- The key is `(membership_id, key)`, partitioned by the primary caller tag and every membership pin
  (`_scoped_idempotency_key`). A global or org-wide key would not separate independent callers. On
  treg's shared key, the label sent upstream is re-scoped per org and pin
  (`scope_shared_idempotency_key`), so two customers' identical labels never resolve to one upstream
  job; BYOK labels pass verbatim.
- A short transaction claims a pending row before relay; the unique constraint arbitrates (loser
  409); reusing a label with another fingerprint is 422.
- Metered successes keep status, body, charge and call id for 24 hours.
  `response_retention: forbidden` keeps a body-free 410 receipt instead, for BYOK too. Uncharged
  failures (including every routed failure), other BYOK calls and owned free polls release the label
  at once.
- Replays return `X-Treg-Idempotent-Replay: true`, `X-Treg-Cost-Micro: 0` and the first figure in
  `X-Treg-Original-Cost-Micro`; MCP returns `replayed: true`.
- Refusal and cancellation cleanup return an acquired label; expired entries are swept lazily per
  caller at claim time, skipping locked rows.
- A reserve admission 429 never reaches `_store_idempotent`; `_stamp_call_exit` releases its pending
  claim (both MCP surfaces too). If that cleanup fails twice, the row stays pending until the
  stale-claim resolver stores 410 `idempotency_outcome_unknown` when no money evidence exists.

## Tag-based billing - a builder reselling treg to their own users

treg's job for a builder is **attribution**, **enforcement** and **export**. It never bills their end
user, holds their card or sets their price; margin stays 0%. One org, one balance, one token, and
each call tagged:

```
X-Treg-Meta: customer=cust_8123, workspace=ws_9, feature=email-finder
```

It is a **header, never a tool argument**: a model asked to pass an id drops it somewhere in a chain.
`application.call.intake` parses it **once** per request, before idempotency, and everything
downstream reads that object. Validation refuses rather than repairs (an oversized value is a 422,
never truncated, because a truncated id merges two of their users). Values containing `@` are refused
outright: the ledger is append-only, so an email written today cannot be erased on request tomorrow.
Limits live in `domain.governance.budgets`.

**Any tag can be reported; declared tags can be enforced.** Reporting groups by any key over a
bounded window. Enforcement runs on every call and needs an indexed aggregate, so a key is budgetable
only once the team **declares** it (`Org.budget_dims`, capped): each declared dimension is another
row written per call. **Budgets stack**: every declared dimension is evaluated and the first breach
in declaration order refuses, naming the dimension. `domain.governance.budgets` owns validation and
dimension selection; `application.call.reserve` owns the call-side caps; a newly observed tag returns
`created` without committing, and the caller commits.

**`TagSpend`**: `ledger.reserve` writes one row per tag in the balance transaction, each carrying the
**full** call amount (cost-allocation-tag semantics: summing within a dimension reconciles to the org
total, across dimensions double-counts, so every report names its key). `amount_micro` tracks the
hold: estimate in flight, consumed figure at settle, deleted on release. Hence two deliberately
separate reads: `tag_spent_since` (cap, counts in-flight work and errs toward refusing) and
`tag_invoice_since` (invoice, settled rows only). Do not deduplicate them.

**Tag caps are SOFT and must never be sold as hard.** A per-tag total is an aggregate, so concurrent
calls can each read a compliant figure; overshoot is bounded by concurrency times the estimate, which
is acceptable only because the org balance and per-org daily cap sit behind it. Making it exact needs
a second materialized authority with four new ways to disagree with `domain/money`.

**Refusal bodies are not the org's.** A tag refusal is rendered to the builder's end user, so it
shares no code with the org 402 (which carries `balance_micro` and a top-up URL): `{error, dim, val,
spent_micro, cap_micro, period, estimated_cost_micro, message}`, ordered so it never falls through to
the org 402.

**Invoices read the ledger.** `GET /orgs/{id}/usage/by-tag` takes money from the ledger and counts
from `CallRecord`, whose queue sheds rows under exactly a successful builder's load. The money query
lives in `domain/money`. `unattributed_micro` is reported so `attributed + unattributed == settled
spend` holds for any dimension.

**Tag for counting, token for control.** A tag is a label, not a boundary. A token that runs on an end
user's machine is pinned (`Membership.pinned_tags`, `treg org agent-new --pin customer=cust_A`); the
pin **beats the header**, and naming a different value is a 403. `reserve_in_transaction` writes
`meta.tags` from its authoritative `tags` argument, overriding caller provenance; that append-only
reserve entry outlives the hold and proves ownership for `/calls/{call_ref}` when audit was shed.

## Referrals

`domain/referrals.py` owns policy; credit moves only through `ledger.grant`. Rewards are flat,
symmetric credit bonuses (`config.referral_*`). Cash payouts and a platform-wide payout budget are not
implemented, so the per-referrer cap does not bound platform-wide exposure.

- A referral is redeemed at first team creation and qualifies when cumulative purchased credit
  reaches `referral_min_topup_micro`. `qualify` checks referrer availability, the card fingerprint
  against earlier qualified/paid referrals, and the referrer's lifetime cap. Evaluate eligibility
  against scarce payment instruments, not freely created accounts. The Stripe fingerprint is optional
  and lives only on `Referral`; refusals stay recorded (`capped`, `rejected`).
- The referee is credited on qualification; the referrer waits `referral_hold_days`. Uniqueness on
  `referred_org_id` and `qualifying_payment_intent` prevents duplicate qualification. Grants use
  `once=False` (`once=True` is a SELECT check, not a concurrency guard).
- `_pay` commits the paid claim before granting, then commits grants and block-id stamps together. A
  crash between them leaves a paid row with missing block ids (visible in `/admin/referrals`):
  no double payout, but it needs reconciliation.
- `charge.dispute.created` and `charge.refunded` cancel rewards still in the hold window; granted
  rewards are flagged for review, never reversed. A refund also revokes the top-up's unspent credit
  (`revoke_refunded`); a dispute does not.
- `sweep` runs from the top-up and referral-page journeys without a scheduler and must not fail
  either caller: copy identifiers before a rollback can expire ORM objects.
- `offer_for_org` masks the referrer's email (one local-part character, fixed bullets, domain kept).
  The referrer's own `summary` shows full referee emails for conversion attribution; that exception is
  disclosed in the privacy policy and must not extend to the public referral-link flow. Routes:
  [the API fragment](../interface/api.md#referrals).

## Not money: the capacity mark

`settle._note_capacity_signal` writes a ratestore row (`capacity:lock:<key>`) after a tier-4
balance/quota signature and `_note_capacity_recovery` removes it after a probe's 2xx. Neither touches
a balance, hold or ledger row; both are allowlisted on their own (`capacity_exhausted_mark`). See
`ops/capacity.md`.

## Caller cost ceilings

`MarketplaceCall.max_cost_micro` carries the caller's remaining ceiling. `_platform_reserve` checks
the actual estimate with margin before opening its transaction. Direct calls set it only from
`X-Treg-Route-Max-Cost`; routed children (including a Route-Verify check) always inherit their
route's remainder. A refusal is a 402 `route_max_cost` that moves no money. Overflow inherits the
field and checks its own estimate; a preceding direct charge reduces the remainder.

With a `ceiling_param` declared (AGENTS.md non-negotiable 4), the appended USD figure is the largest
provider charge that still settles within the ceiling after `with_margin` (which rounds up); at margin
0 it is the ceiling. The provider's refusal of a dearer source is a non-2xx uncharged relay, which a
routed waterfall treats as a failed attempt. For every other provider the ceiling is a
pre-reservation guard only: provider-reported settlement can still exceed the estimate it checked.

## Overflow money

The overflow child (`application.call.overflow`) is an ordinary metered cycle on its own hold
(`{call_ref}:overflow`): reserve at the route's aggregator price, settle with `observed_override` =
the aggregator's in-band charge (0% markup), and `cost_source: "aggregator"` + `served_via` in the
ledger meta. `OverflowSpend` (per aggregator per UTC day) is updated in the same settle transaction;
it is accounting for `TREG_OVERFLOW_DAILY_BUDGET_USD`, not a balance. Shadow mode places no hold.
Skip-direct places no parent hold and leaves `payer_org_id` empty; child cancellation requires the
authenticated caller and releases under that org.

The relay price is disclosed wherever a price is read: `X-Treg-Served-Via: overflow:<aggregator>`
with the child's charge on `/call/`, `served_via` plus a `hint` in the MCP `call` result on both
surfaces (MCP clients never see headers), and `overflow_price_usd` / `overflow_price_unit` /
`overflow_via` on catalog detail when the deployment can relay, so a catalog-free endpoint is never
silently a paid one. Not disclosed up front: *when* a free endpoint is diverted (the route enables
whenever the aggregator price is at most `FREE_ROUTE_MAX_USD`); the caller learns it from the answer.

An aggregator's own per-request refusal (a bare 400/422/404 with no vendor data) parses as `contract`
and releases with `overflow_contract`, without marking the aggregator. Only a non-JSON body, a 5xx or
a transport error is `malformed` (`AGGREGATOR_SIDE`), which marks the aggregator for every org.

## The hub seller's price (`earned`, `settle_to_in_transaction`)

The tool hub ([hub](hub.md)) adds one block kind and one primitive. `earned` is credit a maker's team
received as a seller's price; it spends after the free kinds and before `purchased`.
`settle_to_in_transaction` closes the payer's hold and grants the settled amount to the payee as an
`earned` block in the same transaction: `actual_micro=None` settles the full hold (a flat price), a
given `actual_micro` settles that and refunds the rest (a variable price capped by the declared
maximum). The payer's `settle` meta names `payee_org_id`; the payee's `grant` meta names
`payer_org_id` and the run. It does not commit (the hub runner owns the transaction) and is the only
cross-team money movement in treg.

## Response evidence size and free final downloads

Settlement evidence must be complete. The 8 MiB buffer raises a typed 502 on overflow and closes
upstream; the call releases its hold and idempotency claim. Partial JSON never falls back to an
estimate or settles as success.

An authorized, explicitly free final result GET with no body-evidence consumers streams unbuffered
(`MarketplaceCall.streamable_free_result`), closing its settlement with an explicit zero before
returning the stream (a no-op for a holdless call, above). It is not stored for idempotent replay. MIME type never decides billability.

A metered 2xx on a `spooled_response` endpoint settles in `_spool_response` from the paths its row
reads before headers go out, so `X-Treg-Cost-Micro` stays exact. A completed answer whose evidence is
missing or unparseable settles at the reserve (the media exists); an oversized or budget-refused body
releases as `response_buffer_limit`. The label is released, so an idempotent retry is a new, billed
generation.

## Apify dataset-row settlement

An Apify `per_result` row on the platform key requires `maxTotalChargeUsd` (above 0, at most $1), the
spend cap Apify enforces; `maxItems` does not bind actors whose own input sets the row count. Only
the run options `maxTotalChargeUsd`, `maxItems`, `memory` and `timeout` are accepted, each once and in
plain ASCII, because a dataset-view option (`limit`, `offset`, `format`, `unwind`) would make the
returned rows disagree with the events billed. `_marketplace_pricing` holds the cap plus
`cost.call_fee` (the flat per-run charge), multiplied by the body arrays named in `cost.call_fee_per`
for actors that bill a start per query. `_observed_cost_micro` settles returned rows times the row
price plus the fee; within two rows of the hold the cap was reached and the hold is the bill (a run
stops when its next event would pass the cap and may have billed an event it never pushed). The cap
must cover `call_fee` plus three rows, or an empty answer would bill in full.

`usageTotalUsd` trails a finished run by minutes, so it is not settlement evidence. A run that times
out, FAILS or is ABORTED answers 400 `run-failed` with no rows and releases although Apify may have
billed up to the cap; so does an answer over the evidence limit, and so does a caller who disconnects
mid-run. The $1 ceiling bounds each loss. `timeout` is required and at most
`min(90, call_timeout_s - 30)` seconds (`_apify_max_timeout`), because a run still going when Apify's synchronous wait, treg's upstream
read timeout or an MCP client's timeout ends leaves an unbilled failed call while the run keeps
billing. That failure body names the run, so **treg's Apify account must keep general resource
access Restricted**: with public access anyone could read the unbilled run's dataset by id.
