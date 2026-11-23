---
title: Affiliate program on Dub Partners - click or code, lead outbox, Stripe tag
status: shipped
sources:
  - src/treg/application/affiliates.py
  - src/treg/application/affiliate_bonus.py
  - src/treg/infra/dub.py
  - src/treg/routers/signup_cookies.py
  - src/treg/alembic/versions/0118_affiliate_lead.py
  - src/treg/alembic/versions/0125_affiliate_bonus.py
related:
  - architecture/money.md
  - architecture/ads-conversions.md
  - architecture/data-model.md
  - interface/onboarding.md
---

> Scope: how treg attributes a team to an affiliate's Dub link and tells Dub. Commissions, payouts,
> partner applications, messages and fraud review live in Dub, not here. The credit friend referral
> is a separate program ([money](money.md) "Referrals").

# Affiliate program on Dub Partners

Off unless `dub_api_key` is set (`affiliates.enabled()`): no cookie is parked, no lead is staged, no
Stripe metadata is added, and the friend referral behaves as before. Test environments use a
`dub_test_` key from Dub's staging workspace, which accepts only a Stripe test-mode account.

## Click

A partner link redirects to the program's destination URL with `?via=<link key>&dub_id=<click id>`
appended (Dub's contract). `_remember_affiliate_click` parks the id in the httponly `treg_dub` cookie for 90 days,
Dub's default attribution window, on `/` and `/app`. The id is checked against
`dub.CLICK_ID_RE` on write and on read (`_take_affiliate_click`). A later click overwrites an earlier
one: last click, Dub's default model. `/?dub_id=…` serves the landing, not the SPA, as `/?ref=` does,
together with any tracking-only parameter in `web._SHARE_LINK_PARAMS` (`via`, ad-click ids) or
`utm_*`; any other parameter still hands the request to the SPA.

## Lead

- Team creation (`signup.create_org`, reached by `POST /orgs`, `POST /onboard/team` and the OAuth
  first team) stages one `AffiliateLead` in the team's own transaction (`affiliates.stage_lead`).
  The unique `org_id` makes it once per team, and `affiliates.brought_before` makes it once per
  person: a partner earns on the person they brought, so a later team the same user, or the same
  inbox under an alias, makes stages no lead. An `AffiliateClaim` row (the `promo_inbox`) is staged
  with the lead and outlives team deletion, as `SignupPromoClaim` does (the CLI's `settleRef` and the cleared cookies below say the same thing client side).
- An affiliate click wins over a friend code: a team with a staged lead skips `_redeem_referral`, so
  one signup is never paid by both programs.
- `affiliates.worker` drains due rows every `DRAIN_INTERVAL_S` with `POST /track/lead`
  (`clickId`, `eventName` = `dub_lead_event`, `customerExternalId` = the org id, and the creator's
  email unless it is a machine address, which Dub's risk checks compare with the partner's). Rows are
  read, the session is closed, Dub is called, then a new session records the outcomes: no database
  connection waits on Dub. Every control process runs the worker, so `affiliates._due` claims each
  row with a conditional update that moves `next_attempt_at` past a claim window (`_CLAIM_S`); a
  row another worker claimed is skipped, and a worker that dies frees it when the window passes.
- A row moves `uploaded_at` (Dub holds the lead) then `verified_at` (its partner is known not to be
  the team's own owner, see Self-referral); only a verified row attributes. 400/404/409/410/422
  dead-letter (`failed_at`, `error`). Anything else, network
  errors and 401/403 included, retries with exponential backoff capped at a day, so a fixed key
  drains the backlog. Dub deduplicates leads on (`customerExternalId`, `eventName`), which makes a
  retry after an ambiguous outcome safe; it is also why `dub_lead_event` must not change after launch.

## Code (CLI and agent signups)

A signup in a terminal never clicks a link. An affiliate partner's code typed instead, in the binary
CLI only (`treg login --ref`, `treg org create --ref`, `TREG_REF`, or `install.sh --ref`, which leaves
it in `~/.treg/ref` for the binary; the frozen Python CLI has no `--ref`) reaches team creation as the `ref` body field of `POST /orgs` or `POST /onboard/team`;
the browser sign-in that `treg login` opens carries it as `/login?ref=`, which parks it in its own
`treg_partner` cookie (`_remember_partner_code`), read by the team doors when the body has none. The
CLI forgets the code once its owner is in a team. A typed code is only ever a Dub partner's link key
(`dub.PARTNER_CODE_RE`, `signup._partner_code`): one that equals a treg friend-referral code
(`User.referral_code`) is ignored, as is any code while Dub is off. The friend referral comes only
from the `?ref=` link cookie, unchanged. A link click wins over a typed code. `POST /orgs` and
`POST /onboard/team` clear both `treg_dub` and `treg_partner` (`_forget_affiliate`) once they answer.

That code is staged as a code-only `AffiliateLead` (`partner_code`, and `country` from `CF-IPCountry`,
else `dub_default_country`: Dub requires one for this lead; the check constraint keeps exactly one
of `click_id` and `partner_code`). The drain resolves it with `GET /links/info` on `dub_link_domain`,
then creates the customer under that partner with `POST /commissions` (`type: lead`), which is what
lets later Stripe sales for the org id pay the partner (`dub.partner_for_code`,
`dub.lead_for_partner`). An unknown code (404) or a link with no partner dead-letters; an unset
`dub_link_domain` retries. A dead-lettered lead attributes nothing: `affiliates.attributed` ignores
it, so its team gets no Stripe tag. The table has no foreign key: team deletion removes the row in
code (`ORG_SCOPED_MODELS`).

## Self-referral

A partner is never credited for a team its own inbox created or owns. The drain reads the partner's
email (`GET /partners/{id}`, `dub.partner_email`) and compares it with the creator's and every
owner's inbox under the signup credit's one-per-inbox rule (`promotions.promo_inbox`: `+tag` and
Gmail dots ignored), in `affiliates._is_self`.

- A `self_referral` outcome outranks any verification: it clears `verified_at` and
  `stripe_synced_at`, so `_sync_stripe` untags the Customer, and a later verified result never
  overwrites it.
- Code lead: the check runs before `POST /commissions`. A match dead-letters the row as
  `self_referral` and nothing reaches Dub; a failed partner lookup retries rather than block.
- Click lead: Dub has already recorded it, so the check reads `link.partnerId` from the
  `/track/lead` response afterwards and keeps it (`partner_id`). A match marks the row
  `self_referral` (uploaded and failed). A failed partner lookup leaves the row uploaded but not
  verified, retried with backoff on the partner check alone (the lead is never sent twice), so a
  Dub outage cannot skip the check. The response names no partner on a deduplicated repeat (the
  retry after a reply that timed out), so the check asks Dub's customer record instead
  (`GET /customers?externalId=`, `dub.customer_partner`); a customer Dub cannot find yet is
  re-checked, and one with no partner is verified with nothing to pay.

## Sales and refunds

Dub's Stripe app reads them. treg only tags a verified team's Stripe objects with
`dubCustomerExternalId` = the org id (`affiliates.stripe_metadata`): on the Customer at creation
(`billing.get_or_create_customer`) and on the top-up Checkout Session. The lookup reads on billing's
own session, and billing ends that read transaction before each Stripe call, so neither a second
pool connection nor an open transaction waits on Stripe.
Dub credits only a payment that already carries the tag when it is made (a tag added afterwards
links the customer but pays nothing for earlier payments: checked on Dub staging). So
`stripe_metadata` resolves the team's own pending lead on the spot (`affiliates._check_now`), before
billing creates the Customer or the Checkout: it claims the row like a worker, ends the
transaction, runs the same Dub calls bounded by `CHECK_NOW_S`, and records the result. A row another
process holds, one waiting out a retry, a timeout or a Dub outage leaves the lead to the drain and
that payment untagged; the payment itself never waits on more than `CHECK_NOW_S` and never fails
for it. After the drain records outcomes, `affiliates._sync_stripe` brings an existing Customer in
line (`Customer.modify`, which Dub sees as `customer.updated`): tagged when its lead is verified,
untagged on a self-referral; `stripe_synced_at` marks it done. Stripe refusing the request itself
(`InvalidRequestError`, such as a deleted Customer) settles the row with the error; any other failure
waits out the same growing delay in `next_attempt_at`, and rows go oldest first, so a few broken
Customers never hold up the rest. A lead whose team has no Customer yet stays unsynced until one exists, so a Customer billing
commits while the lead is being verified is still tagged on a later drain. No connection is held during the Stripe call either. Manual top-ups complete a
Checkout with `invoice_creation`, and auto top-ups are paid invoices, so both reach Dub as
`checkout.session.completed` / `invoice.paid`, and their refunds and disputes as `charge.*`. A team
no affiliate brought carries no Dub key. Team deletion removes its `AffiliateLead`.

The reward's 6-month window is Dub's: it starts at the customer's first sale of any kind, an
enterprise SSO payment on the same Customer included, so `/terms` counts it from the first payment.
Commissions Dub records on SSO payments are cancelled in Dub's review.

## Partner offer

The team a partner brings can get bonus credit on its paid top-ups (`affiliate_bonus`). The offer is
set in Dub, not in treg: a discount with the Custom provider (no Stripe coupon) on the program's
default group, another group, one partner or one link. Dub resolves which one a customer gets (link,
then partner, then group) and returns it on the customer record (`GET /customers?externalId=` with
`includeExpandedFields`, read by `dub.customer_partner` into `dub.Discount`). treg never changes a
Stripe price for it: it pays a `bonus` credit block through `ledger.grant`.

- `billing._credit` stages one `AffiliateBonus` row per new paid top-up of a team with a lead that
  is not dead-lettered (`affiliate_bonus.queue`), in the credit's own transaction: a crash or a
  failed staging leaves neither, and Stripe's redelivery stages both. The unique `payment_intent`
  makes it once per payment. Nothing reaches Dub from the webhook.
- `affiliates.drain_once` then runs `affiliate_bonus.drain_once`: rows are claimed with a conditional
  update, the offer of each verified team is read from Dub with no session open, and each row is
  decided in its own transaction under an advisory lock per team and per card (Postgres), so the
  grant and the row's `outcome` commit together. A row whose lead is not verified yet, or a Dub
  error, retries with backoff; after `GIVE_UP_AFTER` (7 days) it is decided `expired`.
- A flat offer (`amount` in cents) is paid once per team, when its paid top-ups, net of refunds,
  first reach `referral_min_topup_micro`, and once per card fingerprint across teams: the grant
  stages an `AffiliateCardClaim`, which has no team and outlives team deletion, like
  `AffiliateClaim`. Its duration is ignored. A payment credited from `payment_intent.succeeded` before its Checkout completion is
  staged with no card, so the worker reads it from Stripe (`PaymentIntent.retrieve`, no session
  open) before deciding; a failed lookup retries.
- A percentage offer is paid on each top-up of at least that minimum, for `maxDuration` months from
  the team's first paid top-up: 0 is the first such top-up only, none is every one.
- One grant never exceeds `affiliate_bonus_max_micro`. No offer, a team the friend referral
  rewards (a pending, qualified or paid `Referral`), a payment already refunded and a dead-lettered
  lead earn nothing; `outcome` says which.
- The grant's meta carries the `payment_intent`, so `billing.revoke_refunded` revokes it in
  proportion with the payment, like the top-up bonus. The decision row-locks the payment's purchased
  block before it reads the payment's refunds, the lock a refund takes too: either the decision sees
  the refund, or the refund runs after the grant commits and revokes it. The billing page's top-up history shows only
  the top-up bonus.

## Not built

The in-app partner dashboard (Dub embed), the application webhook and approval rules, and moving
existing applicants into Dub.
