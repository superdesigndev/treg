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

A catalogued endpoint can be served on **treg's own key** — no provider signup for the caller — which
means treg pays the provider and bills the team. That needs a balance, a way to top it up, and a way
to prove afterwards that the numbers were real. Three modules, one job each:

Two wallets of treg's spend through this machinery, and only these two: **tier-4 platform keys**
(`TREG_PLATFORM_KEY_*`) and **oauth-billed apps** — providers like X whose upstream bills the app
owner per use, so even a call on the org's *own* connection spends treg's prepaid credits
(`MarketplaceCall.billed_oauth`; detection and rates live in
[auth-secrets](auth-secrets.md)). Both run the same reserve→relay→settle path in `routers/call.py`, share the
fail-closed daily cap, and are distinguished in ledger meta by `tier: platform` vs `tier: oauth`.
An org's own key/credential on any *other* provider is never metered — there the org's account pays.

On an oauth-billed provider a **`free` catalog price is a bug, never a fact**: the upstream charges us
whatever the route costs, so a zero there means the entry is stale, not that the call is free. The
estimator must fall through to the provider rate rather than reserve nothing — it used to rest on
`0.0` being falsy, which read as both "no price recorded" and "the price is nothing", and let the
catalog publish $0 while the balance lost the fallback. Whatever the catalog publishes for these
providers is what the reserve takes, and a test walks the provider asserting the two agree.

| Module | Job | May it write money? |
|---|---|---|
| `domain/money` | the only code path that moves money | **yes — exclusively** |
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
micro and every crossing goes through `micro_to_cents` / `cents_to_micro` in `application/billing.py` — the one
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
| `reserve` / `reserve_in_transaction` | balance down by the estimate, `Hold` opened — committed by the compatibility wrapper or the call application |
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
apart — and it has to, because the error evidence that would otherwise explain the difference is
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
The response still reports `X-Treg-Cost-Micro: 0`, which is what the call ends up costing — but the
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
refundable and disputable — so spending promo first keeps the refundable pool as small as possible
for as long as possible.

**Margin is applied inside the module** (`with_margin`), at reserve AND settle, and the rate in force
is recorded on every entry — so a rate change cannot retroactively rewrite what a call cost, and two
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
row. The worker claims due rows with `FOR UPDATE SKIP LOCKED`, polls through the normal credential
injector (a static poll's parameter rides as `query_items` or a path substitution; the relay forwards
no URL-embedded query, which once left MiniMax v1 polls empty; a path parameter is substituted by its
declared location and percent-encoded; the body is capped at `MAX_POLL_BODY_BYTES`), takes terminal
evidence only from a 2xx poll (an error envelope that happens to say `succeeded` backs off like any
other non-2xx, the same rule the CLI applies), settles success, fully releases failure and backs off nonterminal states. **At the 24-hour
deadline it releases the hold in full**, marks the row `timed_out` with `reconcile_review`, and logs
an ERROR-level alert: an outcome nobody observed is the platform's cost, never the customer's, and a
provider that silently changed its status field shows up as absorbed timeouts in
`reconcile.async_task_settlement` (`absorbed_timeouts`) rather than as a quiet overcharge.
Platform-key poll and fetch calls are authorized against the caller org's row before relay. A
successful caller-driven poll may see a fetch-mode result id before the worker does, so the buffered
terminal response records that id on the same row; the worker records it as part of settlement too.
This makes the durable record both the hold owner and the authority for later shared-account objects.
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
application-level SELECT is an optimisation, not the guarantee — two concurrent deliveries of one
PaymentIntent both miss it. (Fixed in #45; the unique constraint is part of the Alembic baseline
schema — the legacy startup migration that once added it is deleted.)

## Stripe (`application/billing.py` and `infra/stripe.py`)

**Credit happens on the WEBHOOK, never on the browser's return from Checkout.** The success redirect
is a URL the payer controls; treating it as proof of payment would let anyone mint balance by typing
it. The one exception is the off-session auto-top-up charge, where the server itself holds the
PaymentIntent's confirmed status — nothing attacker-supplied is involved — so it credits immediately
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
subclassing dict, so `.get()` on one raises, and every consumer —
plus every test fake, which returns plain dicts through this same funnel — reads dict-style. Keep it
that way: a consumer written against the object API would pass prod and break the fakes, and the
last divergence shipped a webhook handler that 500'd on every live checkout while the suite was green.

`_credit` also emits the `topup_completed` product-analytics event (`analytics.capture`, PostHog),
riding the same `fresh` flag as the receipt email so a redelivery re-emits nothing. `capture` is
synchronous and swallowing by construction — analytics is the one side effect in the webhook that is
allowed to fail, and it must fail silently, because a raise here would 500 the handler and make Stripe
retry a payment that already credited. Amounts travel as canonical integer `amount_micro`; the
`amount_usd` on the event is display-only.

On the same `fresh` branch, `_credit` also queues a `paid` Google Ads conversion (`adsconv.queue`) when
the org has a click to attribute to — but this one is **not** atomic with the credit: the credit is
durable before the conversion is queued, and the conversion is a second, separate commit. A crash
between the two loses the conversion permanently (the money is still correctly credited). Found in
review and accepted deliberately (2026-08-17): coupling the credit's fate to the conversion commit
would be backwards, because the credit must stand whatever happens after it; full reasoning and the
cheap future fix in [ads-conversions](ads-conversions.md).

**Invoices exist on the manual path only.** The top-up Checkout sets `invoice_creation`, so a
one-off purchase produces a real Stripe Invoice — number, PDF, billing address, tax ID — which is the
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
needs no deploy). Because the webhook credits `amount_total` — what Stripe actually collected — 20%
off means $40 paid and $40 credited. "Pay $40, get $50" would be a `ledger.grant` on top and is not
built. A 100%-off code collects nothing, so the session credits nothing: `_on_checkout_completed`
drops it as `zero amount`. Grant free balance through `ledger.grant`, never through a Stripe coupon.

**The top-up bonus IS that `ledger.grant` on top — tiered, and manual only.** `topup_bonus_tiers`
(`{10: 0, 50: 5, 100: 10, 200: 15}`, `{min_usd: percent}`) gives a manual top-up a `bonus` block
worth the highest tier at or below the amount (`bonus_for_topup`: $99 earns the $50 rate, $250 the
$200 rate; integer `amount * pct // 100`). It is granted inside `_credit`'s `fresh` branch — the one
point that knows money moved for the first time, so a webhook redelivery grants nothing — as a
**separate block** with `_KIND_ORDER` rank 0: it burns with promo and referral credit, before the
purchased block, and the purchased block stays exactly what the card paid. That is the whole
reason it is not folded into `ledger.topup`: purchased credit is a refundable liability, the bonus
is marketing spend. Automatic refills (`auto=True`) earn nothing — they repeat a chosen amount, and
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
now per-org) and `POST /billing/topup` with no amount uses it — which is what `treg topup` sends.
Presets are four (`[10, 50, 100, 200]`) plus "Other": with eight cards from $5 up nobody ever
picked $100+ and repeat payers stayed flat; the minimum is $10 (fee math, and the referral
qualifying amount). The threshold for auto top-up is validated separately (`validate_threshold_usd`,
≥ $1): it is not a charge, so the top-up minimum must not apply to it — raising the minimum
without that split would have rejected the default $5 threshold on every enable.

**A saved card arms a consented policy from either webhook.** The modal records consent first
(`set_autotopup` → `no_card`) and relies on the top-up Checkout to save the card, so there is no
SetupIntent in that flow: `_set_default_pm` — called by both `_on_checkout_completed` and
`_on_setup_succeeded` — runs `_arm_if_waiting_for_card`, which turns the policy on only from the
explicit `no_card` state. A decline, 3DS, or a deliberate off (reason `None`, consent still on
file) stays off; a redelivered payment webhook must not switch a policy back on.

Turning `invoice_creation` on makes Stripe emit `invoice.created` / `invoice.paid` for every top-up.
`handle_webhook_event` drops them, deliberately: crediting on an invoice event as well as on the
PaymentIntent would be a second door onto the same money. The invoice is a document; the
PaymentIntent is the payment. Note also that `invoice_creation` on one-time Checkout is **priced
separately** by Stripe, and invoice emails only go out with Customer emails → Successful payments
enabled in the dashboard.

**`list_payments` reads rows from us and documents from Stripe.** The payment list is built from our
own `CreditBlock` rows — the same table the balance is computed from, so the history can never show a
payment the balance disagrees with, and amounts and dates need no network call. Stripe is asked only
for the links, in two list calls (`Charge.list` + `Invoice.list`, joined in memory) rather than two
per row; a failure degrades to rows without links and reports `stripe_ok: false`, because a Stripe
hiccup should cost the payer a download button, not their payment history. Both Stripe windows cap at
100 payments, so a very old top-up on a busy account comes back link-less — the portal is the
unbounded archive.

**`create_portal_session` is the self-serve surface** for card, billing address, tax ID and the full
invoice archive: hosted, because every one of those is a form we would otherwise own and the tax-ID
rules go stale per country. It requires a portal configuration saved in the Stripe dashboard, and it
refuses an org with no `stripe_customer_id` rather than minting one — a customer exists once someone
has paid, and an empty portal has nothing to show. `billing_state.portal` is the flag the UI hides
the button on, so a new team never sees a button that would 422.

**Auto-top-up is guarded in depth**, because it is the part that can go wrong expensively: recorded
consent (the PSD2/SCA mandate, a compliance requirement rather than a checkbox), a monthly cap, a
cooldown stamped in the DB *before* the charge so a second web worker sees it, a consecutive-failure
limit, and an idempotency key derived from the threshold crossing — so a burst of concurrent calls
that all notice the low balance produces exactly ONE charge.

Authorization splits by WHAT, not by who. `_billing_org` (the `/billing/*` routes — cards, top-ups,
auto-top-up policy, payment history, the portal) requires **admin or owner**: a card, a spend policy
and an invoice archive are the org's money, not a member's preference.

`GET /orgs/{id}/balance` is different, and deliberately so. Any **member** sees the figure and the
in-flight holds; the **funding detail** (credit blocks, the ledger) stays admin+. It used to be
admin-only, which meant a machine identity could not read the balance it was spending — while every
402 already hands the caller `balance_micro`, and both `llms.txt` and `skill.md` tell an agent to run
`treg balance` after a call. Refusing the number there while shipping it in an error was incoherent.
(Reported by Jason, 2026-08-07.)

The 402 also carries `autotopup_enabled` and an `auto top-up:` line in `message`. Off → the one
command that turns it on. On → the amount, threshold, cooldown and monthly cap, plus the flags that
raise them — because a team that is out of money *with* auto top-up on is being held by the cooldown
or the cap, and "add funds" alone reads as "auto top-up is broken" (cobl.ai, 2026-08-25: ~1,500
refusals between hourly $20 refills against a $60/day burn). The org fields are read **before**
the application reservation transaction: its rollback cannot be a source for refusal rendering after
the session closes. The MCP path still scrubs the payment link from the same
body (`mcp.py`, ChatGPT digital-goods rule) — the auto top-up line survives because it names a CLI
command, not a URL.

## The spend ceiling (`application.call.reserve`)

`_enforce_platform_daily_cap` is a per-org, per-UTC-day ceiling on platform spend, and it is
**fail-closed** — unlike the per-user call cap, which may let a few extra through under load. A query
that cannot answer refuses the call, because this one meters *our* money. The balance alone is not
enough: auto-top-up refills it, so the cap is the blast radius of both a runaway agent and a pricing
mistake in the catalog.

An endpoint whose price is unknown never reaches this path at all: `catalog_store.platform_eligible`
requires `cost_view(...)["usd"] is not None`, so "we don't know" is refused rather than served free —
see [catalog](catalog.md).

## Checking the work (`reconcile.py`)

Read-only, query-time, no scheduler. Three questions, each needing its own source of truth:

- **`price_drift`** — did the catalog's price stay true? Compares, per endpoint, the estimate
  RESERVED against the cost the provider REPORTED, both on the same `CallRecord` row. Providers
  re-price whenever they like; a silent 10% climb turns a positive margin negative with nothing on
  fire, and this report is the only thing that notices.
- **`provider_spend`** — reads the **ledger**, not the audit table, because it is the number a human
  holds next to an invoice. Audit rows are fire-and-forget and may be missing; ledger rows may not.
- **`repeat_rate`** — measurement only: how much of the bill was the same query twice. Answering it
  first is what makes a cache a decision rather than a guess.

Two aggregations happen in Python rather than SQL on purpose — the ledger's provenance lives in a JSON
`meta` column (portable JSON extraction across SQLite and Postgres is not worth a report), and these
are admin-scale windows over a bounded number of metered calls, the same tradeoff `admin_stats` makes.

## Where a call's money actually moves

    resolve → _platform_offer (priced + eligible?) → application.call.reserve (spend caps)
            → ledger.reserve_in_transaction (the UPDATE gate; 402 if short) → application commit
            → relay upstream
            → settle at the observed cost when the provider reports one
              (dataforseo `cost`, scrapecreators `credits_charged`, akta `credits_consumed` —
              the last is what makes akta's per-section enrich billable: the estimate is an
              upper bound, the settle is the real charge; Crustdata reports `X-Credits-Used` in
              a response header; Exa reports dollars as `costDollars.total` on every body —
              the per-result rider beyond 10 and each contents type live only there), else at
              the estimate;
              release instead when the call was not billable

Two providers do not report a charge but have a rule the RESPONSE decides, so `_observed_cost_micro`
derives it rather than letting the estimate stand: apollo answers a miss with 2xx and charges nothing
(`organization: null`, an empty `organizations` page), and **hunter's domain search bills one whole
search credit per 10 emails returned, rounded up, with an empty domain free**. Hunter is the case that
shows why a per-row estimate is not a settlement: its catalog price has to be flattened to
$0.00245/result (1 credit ÷ 10) for a price list, and settling on that number billed a zero-email
search the 20-row default page — $0.0490 for results nobody got, 20x the published per-result price —
while `limit=1` on a domain that did answer settled at a tenth of the credit Hunter actually took.
Where a provider's real rule is "whole units, rounded up, free on a miss", only the body knows.

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

The same family carries two more derived rules. **Hunter's email finder** is the flat case: one whole
search credit when an email comes back, nothing on a miss — Hunter documents a miss as free, but a
miss still answers HTTP 200 with `email: null`, so the estimate billed the full $0.0245 for a name
Hunter had nothing on (a customer measured exactly this against the catalog's own "a miss is free"
note). **TikHub** reports billing in prose instead of a number: its envelope says whether the request
incurs a charge. A 2xx whose payload is an embedded error ("dead_page", a job listing that
redirected away) still says it will be charged, and TikHub really does charge us for it — verified
live 2026-07-30 — so those settle at the estimate *faithfully*; only the explicit no-charge phrasing
settles at zero. The distinction matters when a caller disputes a dead-page charge: for Hunter the
old behaviour was an over-charge and is now fixed, for TikHub the charge is a passthrough of a real
upstream cost, and the answer is to probe with a provider whose misses are free (scrapecreators
404s cost nothing) before spending TikHub calls on unverified slugs.

**Bright Data** is the derived rule in the other direction — the UNDER-charge, and the largest one
found: it bills $1.50 per 1000 records *delivered*, reports no charge field, and one call can
deliver thousands of records, so settling per_result calls at the estimate billed a ~6,000-record
Google Play reviews job as one record. Three weeks of traffic consumed $13.61 upstream and billed
orgs $0.35 (2026-08-24, a 39x gap). `_brightdata_record_count` counts the response instead — a
JSON array or csv/ndjson lines are the records; any JSON *object* is zero records (the >60s sync
fallback's and /trigger's `snapshot_id` handoff, an early download's `status: running`), because
the async job's records bill when `brightdata.web.scrape.job.results` downloads them (that
endpoint's price moved from free to per_result for exactly this — which also means re-downloading
a snapshot bills the caller again; the catalog note says to download once). A gzipped
(`compress=true`) or buffer-truncated body settles at the estimate: when we cannot count, the
estimate is the honest number. This needed `unit_micro` (the per-ROW price) to ride the
`MarketplaceCall` on every tier, where before only oauth-billed calls carried it.

**Aviato** adds another derived family without changing the relay: fixed route prices settle at the
estimate; catalog `cost.modifiers` make observed preview, email and rescrape flags change the reserve
before relay; synchronous bulk enrichment settles from the number of successful records in the response. Simple people search
reserves its fixed 0.25-credit base and, when `enrich=true`, the documented one-credit-per-result
add-on. Two multi-row `enrich=true` probes returned only id rows and each consumed the 0.25-credit
base. The endpoint's catalog `settle: base` rule therefore releases the reserve rider after a
successful response. The hold stays conservative while the final charge follows observed vendor
behavior. Repeated balance probes also showed that company single and bulk rescrape, person single
rescrape, and person bulk email riders listed on the authenticated price page are not charged.
Person single email and person bulk rescrape riders are charged. The YAML models the measured rules
with `reserve_only` on the documented but live-unbilled riders. The hold remains conservative;
`settle: modifiers` releases those riders and charges only the measured combination. Crustdata differs because it
reports the complete answer directly: `_platform_settle` passes response headers to
`_observed_cost_micro`, which converts `X-Credits-Used` through the same `fx.yaml` rate used by the
catalog.

Closing the hold runs on its **own session** (the request's may be mid-rollback from the very error
being released for) and **never raises** — the caller already has their answer, and a ledger hiccup
must not turn a served call into a 500. A hold that fails to close is not lost money either: the
reaper releases it, which errs in the org's favour. That "errs in the org's favour" is still forfeited
revenue, so the one failure that is transient by nature — no pool slot within `pool_timeout`
(`sqlalchemy.exc.TimeoutError`) — gets exactly one retry after 0.5 s before the log line; anything else
falls straight through. The request session itself is committed before the relay precisely so this
second session never has to wait on the first (see [proxy-model](proxy-model.md) § Connection
discipline).

## Shared-plan pricing: flat-fee providers, and the rate treg sets

A flat-fee provider (a monthly subscription with a rate limit or unlimited calls) has no per-call
vendor price, which kept every one of them out of the catalog. The ladder that admits them:

| The provider sells | The price of one call |
|---|---|
| real credits | vendor price ÷ credits (the normal fx entry) |
| a monthly request cap | fee ÷ cap — same arithmetic |
| a rate limit only | fee ÷ theoretical max is the FLOOR; the rate sits above it at a stated break-even |
| unlimited | a treg-set rate with the break-even printed |

The honesty rule that makes the last two rungs defensible: **we never claim these are vendor
prices.** What treg sells there is its own service — subscription custody, the key, a share of the
rate limit — at a published rate whose fee and break-even are printed beside it (fx.yaml
`kind: treg_shared_plan`; `check_fx` makes the marker impossible to carry dishonestly). The price is
also congestion control: at $0, one looping agent exhausts a shared rate limit for every team at
once.

Mechanically a shared-plan provider is just a credit provider whose credit is "one call on treg's
shared plan" — `cost_view`, holds, caps and settlement needed zero changes. What is new:

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
- **`price_drift` never sees these providers** — drift compares our estimate against the provider's
  own reported charge, and a flat-fee provider never reports one. Pinned by a test that fires if an
  observed-cost parser is ever added for one, because at that point the drift report would be
  policing a price treg itself set.

### Trial pools: the $0 rung

A third treg-set rate, `kind: treg_trial` (fx.yaml): a provider served on treg's own FREE-tier key
at exactly $0, capped per team per day (`trial_calls_per_team_day`, enforced by
`api._enforce_trial_allowance` — successes only, fail-closed, refusal 429 `trial_allowance_reached`
with a connect-your-own-key hint). The strategy: the pool is the demand probe — a hot pool is the
buy signal for the provider's commercial tier, negotiated with real volume numbers. Failed calls
never burn allowance (the same line billability draws), and another org's usage never touches this
org's pool (tested). At $0 the allowance is the only brake, so the validator refuses a trial entry
without one.

## Retries: a call must not be paid for twice

Prompted by a public question — *"how does result pricing handle retries, agents need idempotent
billing before this works"* — and it matters more here than for a human-facing API, because agents
retry far more than people do.

**Most retries were already free**, which is what makes the real gap narrow. `_platform_billable`
never bills a 5xx, a 3xx, a timeout or a network error, and bills a 4xx only under `per_call` where
the provider charges for accepting the request at all. Result pricing settles on the provider's own
reported number (`_observed_cost_micro`), so a `per_success` lookup that finds nothing costs nothing.

The gap is one case: treg reached the provider, the provider succeeded **and charged us**, and the
response was lost on the way back. The agent retries and we pay twice.

### Why remembering the charge is not enough

The cheap fix is to note that a key was already billed and skip the second charge. It does not work:
treg would still make the second upstream call, so we would still pay the provider and would simply
move the double cost onto ourselves. The second request has to not reach the provider at all, which
means storing the first response and replaying it.

### The surface

`application.call.idempotency` owns the claim and replay state behind `Idempotency-Key: <label>` on
`/call/`, or the `idempotency_key` argument to the MCP `call` tool. A
replay answers with `X-Treg-Idempotent-Replay: true` and `X-Treg-Cost-Micro` set to what the FIRST
call cost, so a caller can report the charge honestly rather than implying a second one. Over MCP the
result carries `replayed: true`.

Nothing happens without it. A caller who sends no label sees byte-identical behaviour to before the
feature existed, which is what made it safe to ship.

### The key belongs to the caller

`IdempotentCall` is keyed on `(membership_id, key)`. Every door — a personal token, an agent token,
an OAuth grant — resolves to one `Membership`, so a single rule covers a human, an agent, and two
agents in the same team: **the label belongs to whoever called**.

Not `key` alone: clients choose their own labels, the same string will be picked twice, and that
collision would serve one team's stored response to another. It is the only failure in this feature
that leaks data rather than money. Not per-org either — two lazily written agents in one team both
reach for `retry-1` and would collide for no reason.

The key is never derived from the request. That was proposed and rejected: two identical searches an
hour apart are new work, and treg cannot tell that from a retry. A server-invented key would turn a
correctness feature into a 24-hour cache that quietly serves stale data.

### What is stored, and for how long

Metered successes, plus a routed waterfall's terminal failure after one or more paid children, for
24 hours. A team calling on its **own** key is billed by the provider, so there is nothing to protect
and no reason to hold their response. An uncharged failure frees its label immediately so the caller
can retry; a partially charged routed failure stores the same status, `{"detail": ...}` body,
`charged_micro` and call id, because rerunning its children would pay the providers twice.

That is also what bounds storage: bodies are kept only for calls that actually cost money, for a day.

### Concurrency, and giving the label back

A `pending` row is written in an application-owned short transaction **before** the upstream call,
and that row is the lock: two retries
arriving together race on the unique constraint, and the loser is told to wait (409) instead of
duplicating the spend. Same reasoning as the conditional UPDATE in `ledger.reserve` — where two paths
can read before either writes, the database has to arbitrate.

A request that dies after claiming must give the label back, or a single bad parameter would hold it
for the full window and answer every retry with 409 — worse than the problem this solves. The release
happens in the `StarletteHTTPException` handler, the one place every refusal passes through; the call
handler has a dozen raise points and releasing at each would be a dozen chances to miss one.

Expired rows are swept lazily at claim time, scoped to the calling caller, exactly like the hold
reaper: no scheduler, no leader election on a multi-instance deploy.

## Tag-based billing — a builder reselling treg to their own users

A builder embeds treg in their product and bills their own users. treg's job is exactly three things:
**attribution**, **enforcement**, **export**. treg never bills their end user, never holds their card
and never sets their price; margin stays 0%.

They run one org, one balance, one token, and tag each call with their own ids:

```
X-Treg-Meta: customer=cust_8123, workspace=ws_9, feature=email-finder
```

Up to 5 pairs. It is a **header, never a tool argument** — a model asked to pass an id drops it
somewhere in a chain, and a figure you cannot reconcile is worse than no figure. The builder's backend
already sets `Authorization` on the request; this is the same call site. `application.call.intake` parses
it **once** per request, before the idempotency block, and everything downstream reads that one
object. A second parse site would be a second chance to disagree about who pays.

Validation refuses rather than repairs: an oversized value is a 422, never a `[:128]`. A truncated id
merges two of their users into one invoice line, and a dropped tag is usage nobody bills. Values
containing `@` are refused outright — the ledger is append-only, so an email written today cannot be
erased on request tomorrow.

### Any tag can be reported; declared tags can be enforced

The split is **reporting versus per-call enforcement**, not money versus counts.

Reporting groups by any key with real money attached, because an invoice query runs occasionally over
a bounded window at admin scale — the same reason `reconcile.provider_spend` folds in Python.
Enforcement is different: it runs on *every* proxied call and must be an indexed aggregate, so a key
only becomes budgetable when the team **declares** it (`Org.budget_dims`, capped at 3). Declaring a
key is what buys it an index. The cap exists because each declared dimension is another row written
per call and another place settle-vs-reserve correction can go wrong; a team budgeting on `session`
would write an aggregate row per conversation.

**Budgets stack.** `workspace=ws_9` at $50/day and `customer=cust_8123` at $5/day are two `TagBudget`
rows and both apply to a call carrying both tags. Every declared dimension is evaluated and the first
breach in declaration order refuses, so the outcome is deterministic. The refusal **names the
dimension** — a builder running stacked budgets otherwise cannot tell a workspace breach from a
per-user one.
Validation and dimension selection share the `domain.governance.budgets` owner across the call and
control surfaces. `application.call.reserve` owns the call-side spend caps and tag-budget lookup. A newly observed tag returns
an explicit `created` result without committing; the call intake and governance router commit at the
same boundary that makes the row visible.

### `TagSpend` — why the money side is a table, not a JSON key

`ledger.reserve` writes one `TagSpend` row per tag, in the same transaction as the balance movement.
Each row carries the **full** call amount, so the same dollar appears under `customer` and under
`workspace` — cost-allocation-tag semantics. Summing *within* a dimension reconciles to the org total;
summing *across* dimensions deliberately double-counts, which is why every report names its key.

`amount_micro` tracks the hold: the estimate while in flight, rewritten to the consumed figure at
settle, and deleted on release. So a cap counts in-flight work at its estimate and errs toward
refusing — the right direction for money — while an invoice reads settled rows only, because an open
hold is not spend and billing it would charge again when it settles. Hence two deliberately separate
reads, `tag_spent_since` (cap) and `tag_invoice_since` (invoice), named so nobody "deduplicates" them.

### The caps are SOFT, and must never be sold as hard

`ledger.reserve` is exact because the balance is a materialized column: its check and its debit are
one conditional UPDATE. A per-tag total is an aggregate over rows, so N concurrent calls can each read
a compliant figure and together exceed the cap. Overshoot is bounded by `concurrency × per-call
estimate`, and that is acceptable **only** because the hard gates sit behind it — the org balance and
the per-org daily cap.

Making it exact would need a second materialized authority on spend: reset daily, decremented on
release, corrected on settle divergence. Four new ways to disagree with `domain/money`, which is the one
module allowed to move money. Not worth it. Never document these caps to builders as hard limits.

### Refusal bodies are not the org's

A tag refusal is the response a builder renders **to their own end user**. It shares no code with the
org-level 402, which carries `balance_micro` and a top-up URL — the builder's private numbers. Shape:
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

A tag is a **label, not a boundary** — anyone holding the token can send any tag. That is fine when
the only budgets and reports it touches are the builder's own. When a token will run on an end user's
*own machine*, the builder mints an agent token pinned to that user (`Membership.pinned_tags`,
`treg org agent-new --pin customer=cust_A`). The pin **beats the header**: naming a different value is
a 403, because otherwise the holder could retag their calls and walk out of their own budget, which is
the entire point of giving them a scoped token.

### The per-org daily cap has two owners

`Org.daily_cap_micro` is the team's own ceiling; `platform_daily_cap_usd` is ours (**$500/day**
since 2026-08-29 — at $100 a normal agent workload tripped it while the team still held balance).
The effective cap is
`min(the two)` (`api._effective_daily_cap`). A team may lower theirs freely and see it at
`GET /orgs/{id}/settings` — a limit nobody can see becomes a support ticket the first time an agent
trips it. Raising past our ceiling is **refused, not clamped**: a builder who thinks they set $500/day
and silently got the platform default discovers it as an outage mid-launch. That refusal is the commercial conversation,
and it replaces editing one env var that would lift the blast-radius rail for every team at once.

## Referrals — paying for growth out of the one margin we have

`domain/referrals.py` decides; `domain/money` moves. The only crossing is `ledger.grant(...)`, exactly as
`application/billing.py`'s only crossing is `ledger.topup(...)`.

**Why a flat bounty and not a percentage.** `platform_margin` is 0.0 and "we add no markup" is a
public promise (terms §08, landing 04), so there is no gross margin on a catalog call to share. The
only thing treg actually keeps is the gap between what a team tops up and what it consumes. A
percentage of top-ups would therefore be a permanent share of pass-through GMV — and, worse, it
scales the reward with effort, which is the definition of a farmable incentive. Flat figures ($5 to
each side, `config.referral_*`) are budgetable as CAC, bounded by construction, and not worth
building a fake-account farm for. The two sides are deliberately SYMMETRIC: it makes the offer one
sentence to explain, and neither party can feel short-changed by the other's share.

**The qualifying event is a PAID TOP-UP, never a signup.** `promo_grant_micro` is granted per ORG
and nothing caps orgs per user, so a signup-triggered bounty is a faucet pointed at itself.

**The threshold is cumulative, and falling short is not fatal.** This first shipped as "the first
top-up must clear the minimum, or the referral is rejected", and that was wrong in the one way that
mattered: **$5 is the first preset on the billing page and the minimum is $10**, so the most obvious
button silently destroyed the reward, permanently, with no way back even if the team added $100 the
next day. It punished exactly the person the program exists to convert and removed their reason to
add the rest. A short payment now leaves the row `pending`, counts toward the total, and the billing
page keeps the offer up — asking for the REMAINDER, because repeating the full figure reads as
though the money already paid did not count.

This costs nothing in abuse terms: the money still has to arrive, so $5 + $5 buys a referral on
exactly the terms $10 does, and the fingerprint and the cap are untouched. It also let the old
"must be the first purchase" rule go — that existed to stop a second bounty, and `pending` already
does it, since `qualify` only ever selects a pending row.

**Referral credit burns first.** `_KIND_ORDER` gives `referral` the same rank as `promotional`,
because both are marketing spend we can never be asked to return, and spending them first keeps the
refundable/disputable purchased pool as small as possible. This is not cosmetic: an unrecognised
kind sorts LAST (`.get(kind, 99)`), so omitting the entry would have made the bonus burn *after*
money someone actually paid us. Pinned by a test.

**The `Referral` row is the idempotency guard, not `grant(once=True)`.** `grant`'s `once` check is a
SELECT with no backing unique index — fine for a signup promo that is merely retried, wrong for
money owed to a third party, where two concurrent redemptions can both miss it. So every referral
grant passes `once=False`, and two UNIQUE columns arbitrate instead: `referred_org_id` (an org is
referred once, ever) and `qualifying_payment_intent` (one payment funds one qualification). Same
reasoning as the conditional UPDATE in `reserve` and the unique `stripe_payment_intent` in `topup` —
where two paths can read before either writes, the database has to be the one that says no.

`_pay` **claims before it grants**: the row flips to `paid` in its own committed statement, and only
then does credit move. The opposite order would mean the loser of a race had already granted. The
cost is the mirror failure — a crash between claim and grant pays nobody and says otherwise — which
is the right way round for money, is visible in `/admin/referrals` as a paid row with a null block
id, and errs toward paying once rather than twice. The grants and the block-id stamp are then
committed together, in one transaction after the separate claim commit.

**The two sides are paid at different times, on purpose.** The REFEREE is credited the instant they
qualify; only the REFERRER waits out `referral_hold_days` (7).

They are not in the same position. The referrer has a Referrals page listing every invitation and
what it is worth, so a pending reward there is legible. The referee has no such page — for them the
**balance is the only feedback that exists**, and a bonus that is merely "coming" is
indistinguishable from one that never happened. That is not hypothetical: it was reported exactly
that way (topped up, saw the plain total, assumed it had failed), and an "earned, lands on <date>"
banner was built and then discarded in favour of just paying them, because explaining a delay is
worse than not having one.

The price is half the clawback window, and it is worth paying: exposure is one bounty per card (the
fingerprint gate still binds), the referee has just handed us the qualifying payment, and a
chargeback already costs us that payment plus the dispute fee — against which $5 is marginal.
`_grant_referee` is guarded on `referred_block_id`, which is both the once-only guard and the
record, so `sweep`'s referee branch survives only as a fallback for a failed instant grant.

**The hold is the only clawback window there is**, and it now covers the referrer's half alone. `charge.dispute.created` / `charge.refunded` — the first reversal events treg has ever
handled — cancel a bonus still inside that window. Anything already granted is **logged for a human,
never auto-reversed**: referral credit burns first and is usually spent by then, and reversing it
would mean a second code path able to drive a balance negative. The clawback touches the *bonus*
only; it never refunds the top-up, because that has always been a human decision.

**The gates** (`qualify`): first top-up only, at or above the minimum; not a self-referral; the
paying card's Stripe fingerprint has not already claimed a referral; and the referrer is under their
lifetime cap. The fingerprint is the load-bearing one — an email address is free and a card is not —
and it is read via `expand=["payment_method"]` on the `PaymentIntent.retrieve` that
`_on_checkout_completed` was already making. It is not card data and it lives on the `referral` row
alone, never on an `Org`.

**There is deliberately no "the referrer must have topped up first" gate.** It was built and then
removed, and the reasoning is worth keeping because it will be proposed again. A top-up is not a cost
to a self-dealer — it converts into credit they keep — so the attack it appears to block survives it
untouched: requiring one of the *referrer* too just adds a step that returns its own money. The cap
is per-referrer and referrer accounts are free, so a farm's real constraint is CARDS, and the gate
added roughly one card per twenty referrals: a ~5% tax. Against that it hid the link from every
free-tier user, who on a product pitched as "$1.00 free, no card" are most of the userbase and the
likeliest people to tell a friend — a ~90% tax on legitimate referrers. **Before adding any new
eligibility rule here, price it against cards, not accounts.**

The remaining ceiling to be aware of: because the cap is per-referrer and accounts are free, global
exposure is bounded only by how many cards an attacker has. A platform-wide monthly payout budget is
the fix if that ever matters; it is not built.

A refusal is **recorded, not dropped**, and `capped` is deliberately distinct from `rejected`: one is
"you ran out of self-serve allowance", the other is "a gate said no". "I referred someone and got
nothing" is the ticket this program generates, and the answer has to be on the page.

**No scheduler, as everywhere else.** `sweep()` runs from `billing._credit` (any top-up advances the
queue) and the referral application journey for `GET /referrals` (someone checking on their reward is
the one who makes it land), following the same lazy, caller-pays bargain as `reap_stale_holds`. It
never raises: neither its Stripe webhook caller nor its page-load caller may fail over a bonus.

The recovery paths have to honour that contract *in their own logging*: the rollback that contains a
failed payout expires every ORM row the session tracks, and reading an expired attribute in an async
session is implicit async I/O (MissingGreenlet) - so the warning line itself was the raise, exactly
when it mattered. `_grant_referee` and `sweep` therefore copy the row ids to primitives before the
try and log those (sweep copies the whole batch before the loop, since one rollback expires every
due row), and the page journey (`application/referrals.py`) logs the `user_id` it was called with
and revives the expired user row (`sa_inspect(...).expired` then `refresh`, the signup idiom) before
rendering the summary.

**The referee is told, on the screen where it changes their behaviour.** `offer_for_org` is the
mirror of `summary`: a team that arrived through a link has a `pending` row and no idea a bonus
exists. `GET /billing` carries a `referral_offer` (merged by the application journey so the referral
domain remains separate) and the dashboard names the MINIMUM there. The qualifying presets say
`+$5 bonus` on themselves; a note alone sits above the place the decision is actually made (the tier
bonus and the referral bonus stack on the same card). The offer is returned only while `pending` - after qualifying the
money is already on its way through `sweep`, and still advertising it would read as a second bonus —
and it names the referrer MASKED (`mask_email`, `j•••@domain`). Not anonymous — "you were invited"
with nobody attached reads as marketing copy, and someone who clicked a link off a tweet last week
genuinely may not recall whose it was. Not in full either: **a referral link is public by design**,
so the full address would publish one influencer's email to every stranger who signs up through it,
a harvestable list at exactly the volume this program is built to produce. The domain survives the
mask because it is what makes a real friend recognisable; the local part collapses to one character
plus a FIXED bullet run, so the mask cannot leak its own length.

Note the asymmetry against `summary`, which returns referee addresses in FULL: there the referrer has
no other way to tell which of their invitations converted. Here the referee needs no identity at all
to decide whether to add funds, so the same exposure would buy nothing. It is worth stating plainly
that this protects the person who opted into the program and exposes the person who merely signed up
— justified only by that attribution need, and not a precedent to extend.

**Cash payouts are not built.** The self-serve program pays in credit only, and the cap refusal is
the commercial conversation that replaces an uncapped percentage. When an influencer tier lands, it
reads `/admin/referrals` — the same table, filtered — and the payout rail (and its W-9/1099
obligations) is what gets bought rather than built.

## Not money: the capacity mark

`application.call.settle._note_capacity_signal` writes a ratestore row (`capacity:lock:<key>`)
after a tier-4 balance/quota signature, and `_note_capacity_recovery` removes it after a probe's
2xx. Neither touches a balance, hold or ledger row - the lock is a hint for the NEXT caller's
resolution - and both are listed in the dataplane write allowlist on their own
(`capacity_exhausted_mark`), not under the money entries. See `ops/capacity.md`.

## Overflow money

The overflow child (`application.call.overflow`) is an ordinary metered cycle on its own hold
(`{call_ref}:overflow`): `_platform_reserve` at the route's aggregator price, `_platform_settle` with
`observed_override` = the aggregator's in-band charge — the caller pays exactly that, 0% markup —
and `cost_source: "aggregator"` + `served_via` in the ledger `meta`, so `reconcile` needs no join.
`OverflowSpend` (per aggregator per UTC day) is updated inside that same settle transaction; it is
accounting for the $20/day budget, not a balance. Shadow mode places no hold and charges nothing.

## A per_success miss the adapter cannot see (2026-08-29)

`per_success` settles at the estimate unless something can say "this body is a miss". The routing
adapter's predicate covers routed children — but **1330 of 1517** per_success endpoints have no
adapter, and they are the scrapers, whose failure mode IS an HTTP 200 carrying an error code.

Those providers publish a success rule and the catalog already records it as `expect`
(`{json_path, equals}`), read until now only by `scripts/catalog_verify.py`. `settle.py` consults it
when no adapter applies, and `store.py` carries it onto the endpoint — including inherited from the
provider FILE, the way `cache` already is, because a vendor's "HTTP 200 always, read `code`"
convention is one fact about the whole provider and an ingest that adds routes must not ship them
ruleless (33 of justoneapi's 260 were).

Found live: `justoneapi.x.linkedin-search-user-v1` answered `{"code": 301, "message": "COLLECT
FAILED, SEND REQUEST AGAIN"}` — free on the vendor's own published terms ("only a code-0 response is
billed") — and treg settled $0.0295 against the caller. Adapter first, `expect` second, estimate
last; an undecidable rule still settles at the estimate rather than guessing in the caller's favour.

STILL OPEN: tikhub's 919 per_success endpoints have neither an adapter nor an `expect` anywhere.
That is the larger half of the exposure and needs its success convention verified against the
vendor's docs before a file-level rule is written.
