---
title: Affiliate program on Dub Partners - click, lead outbox, Stripe tag
status: shipped
sources:
  - src/treg/application/affiliates.py
  - src/treg/infra/dub.py
  - src/treg/routers/signup_cookies.py
  - src/treg/alembic/versions/0118_affiliate_lead.py
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
  The unique `org_id` makes it once per team.
- An affiliate click wins over a friend code: a team with a staged lead skips `_redeem_referral`, so
  one signup is never paid by both programs.
- `affiliates.worker` drains due rows every `DRAIN_INTERVAL_S` with `POST /track/lead`
  (`clickId`, `eventName` = `dub_lead_event`, `customerExternalId` = the org id, and the creator's
  email unless it is a machine address, which Dub's risk checks compare with the partner's). Rows are
  read, the session is closed, Dub is called, then a new session records the outcomes: no database
  connection waits on Dub.
- 2xx acknowledges. 400/404/409/410/422 dead-letter (`failed_at`, `error`). Anything else, network
  errors and 401/403 included, retries with exponential backoff capped at a day, so a fixed key
  drains the backlog. Dub deduplicates leads on (`customerExternalId`, `eventName`), which makes a
  retry after an ambiguous outcome safe; it is also why `dub_lead_event` must not change after launch.

## Sales and refunds

Dub's Stripe app reads them. treg only tags an attributed team's Stripe objects with
`dubCustomerExternalId` = the org id (`affiliates.stripe_metadata`): on the Customer at creation
(`billing.get_or_create_customer`) and on the top-up Checkout Session. Manual top-ups complete a
Checkout with `invoice_creation`, and auto top-ups are paid invoices, so both reach Dub as
`checkout.session.completed` / `invoice.paid`, and their refunds and disputes as `charge.*`. A team
no affiliate brought carries no Dub key. Team deletion removes its `AffiliateLead`.

## Not built

The in-app partner dashboard (Dub embed), CLI and agent attribution (`--ref`), the application
webhook and approval rules, and moving existing applicants into Dub.
