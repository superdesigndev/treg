---
title: Google Ads conversion tracking - capture, outbox, upload
status: shipped
sources:
  - src/treg/adsconv.py
  - src/treg/application/signup.py
  - src/treg/application/call/settle.py
  - src/treg/web/adtrack.js
  - src/treg/web/gtag.js
  - src/treg/routers/auth.py
  - src/treg/alembic/versions/0075_ad_click_stash.py
related:
  - architecture/money.md
  - architecture/multi-tenancy.md
  - architecture/data-model.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

> Scope: the Google click-id path (`treg_ad` -> `Org.ad_*` -> conversion upload). Generic
> `utm_*`/referrer attribution is the independent `treg_utm` -> `Org.utm_*` path in
> `web/sitetrack.js` ([data-model](data-model.md), [api](../interface/api.md)); a Google ad click
> with utm tags populates both.

# Google Ads conversion tracking

Off unless `google_ads_customer_id` and `ads_conv_refresh_token` are both set (`adsconv.enabled()`),
so tests and self-hosted instances stay inert. When off, `/adtrack.js` is empty and attribution
cookies are ignored. `google_ads_developer_token` is not part of the gate: Data Manager has no
developer-token header; that setting serves only the read-side Ads catalog calls.

## Capture

- `web/adtrack.js` reads `gclid`, `gbraid` or `wbraid` off the URL into the `treg_ad` cookie
  (90 days, Google's click-through window; `SameSite=Lax` so it survives the cross-site navigation an
  ad click is). `gbraid`/`wbraid` are what Google substitutes on iOS traffic; dropping them silently
  loses most mobile conversions. The cookie stores the field name with the value so a braid is never
  uploaded as a GCLID.
- Every public landing surface must load `adtrack.js`; no test sweeps every route, so a new landing
  page adds the tag itself (`interface/seo.md`).
- The script carries the click id across same-origin navigation (link clicks, `history.pushState`/
  `replaceState`) only when it was captured from a URL in that tab, never on a cookie-only return
  visit; `utm_content` carries the original landing. Restoring a cookie never renews its expiry.
- `web/gtag.js` loads Google's tag in advanced Consent Mode v2: every consent signal is set to
  `denied` before the tag script is appended, so Google sets no advertising cookie and receives only
  cookieless pings, which is what lets it model conversions. The dashboard loads it with
  `data-conversion-only` and fires one browser conversion after the first team exists, with
  `transaction_id = treg-web-signup-<org id>`. That action must be configured as secondary in Ads,
  or it double counts the outbox's `signup` row.
- **Short install link.** An agent's embedded browser has its own cookie jar, so `/start` with a
  `treg_ad` cookie mints a one-time code (`signup.mint_ad_link`, `Ephemeral` namespace `ad_link`)
  and shows `curl -fsSL {BASE}/i/<code> | sh && treg login`. `install.sh` stores the code in
  `~/.treg/ad-link`, `treg login` sends it as `/login?...&ad=<code>`, and `/login` redeems it once
  (`signup.redeem_ad_link`) into a `treg_ad` cookie that expires 90 days after minting. A browser that
  already holds `treg_ad` keeps its own. An unknown or spent code installs like `/install.sh`.
- The privacy policy's existing `treg_ad` section is the disclosure for carry and the email
  association below; do not add a separate paragraph without an owner decision.

## Store

`signup._ad_attribution_from` runs at team creation (`POST /orgs`) and writes
`ad_gclid` (historical name, any click id), `ad_click_id_type`, `ad_landing`, `ad_click_at` onto the
new `Org`. Never overwritten: attribution is the first click that led to a signup.

`stash_ad_click` records the cookie against an email only after the email is proven (email-verify,
Google or GitHub callback), never at email-start, where anyone can type any address. `_ad_cookie_for`
falls back to the stash only when the request cookie is empty and the row is under 90
days old. The signup that stores a click deletes that email's stash row in the same transaction, and
`treg-worker admin purge-ad-clicks` (schedule it daily) deletes rows past 90 days. The stash goes
through neither audit nor analytics, and a stash failure never fails sign-in; its log line omits the
email, cookie and exception text.

## Fire

Three sites call `adsconv.queue(db, org, action, ...)`, which writes an `AdConversion` outbox row in
a held-open SAVEPOINT inside the caller's transaction (a plain rollback on a duplicate would undo the
caller's work, for example a Stripe credit on a redelivered webhook):

- `signup._grant_signup_promo` -> `signup`, staged with the grant and committed once. Conversions
  are per team even when no credit is granted; a conversion is not evidence of money.
- `call/settle._record_first_call` -> `first_call`. `Org.first_call_at` is a conditional `UPDATE` on
  its own session, never derived from `CallRecord` (audit sheds rows under exactly the load that
  matters) and never committed on the request's session, which may be mid-settlement.
- `billing._credit` -> `paid`, first credited top-up, with `value_usd_micro`. Not atomic: the credit
  commits first, so a crash between the two commits loses the conversion for good. Accepted; the fix,
  if ever needed, is a read-only sweep for orgs with a credited payment and a click but no `paid` row.

`uq_adconversion_org_action` (one row per org and action), not a check-then-insert, makes every site
safe under retries and webhook redelivery. `queue()` no-ops when disabled, for demo teams, and for an
org with no click id unless Enhanced Conversions for Leads is on.

The outbox is deliberately a durable row and not `audit.py`/`analytics.py`, which are droppable: a
lost conversion silently trains bidding on undercounted data.

## Upload

`adsconv.worker` (started from `lifespan` when enabled) drains every 300 s: up to 100 rows neither
uploaded nor dead-lettered and past `next_attempt_at`, in one POST to Data Manager
`events:ingest` (`adsconv.DATA_MANAGER_URL`, its own `v1`, unrelated to the Google Ads REST version
cycle). All three actions go in one request: `destinations[]` lists each action once and each event
names its destination by `destinationReferences`; `events[i]` maps 1:1 to the outbox row.

Wire constraints Google imposes:

- `transactionId = treg-<row id>`: Data Manager dedupes on it within an action, so a resend after an
  ambiguous outcome folds into the same conversion. A purely numeric id is rejected with a bare 400,
  and `validateOnly` does not catch it.
- `eventTimestamp` is RFC 3339 UTC; stored datetimes are naive UTC and are stamped, not converted.
- Data Manager has no partial success: a 200 with a `requestId` acknowledges every event (per-event
  `fieldWarnings` are kept on `row.error` only); a 200 without one retries the batch. A non-200
  rejects the whole batch. Rows named by a `fieldViolations[].field` path (`events[N]`) dead-letter
  (`failed_at` + `error`) after 8 attempts unless the error is transient; every other row was caught
  in the crossfire and retries regardless. Backoff is exponential from 5 minutes, capped at 24 hours.
- `conversionValue` is a wire double in AUD, converted at upload from the stored USD micro amount at
  a fixed `10/7` (integer math, the float only at the JSON boundary). The fixed rate is deliberate:
  a ROAS change should mean the business moved, not the currency. Never store AUD on the row.

## Enhanced Conversions for Leads

`ads_conv_user_data` (default off). With it on, `queue()` also records rows for teams with no click
id that have a human member (`human_owner_emails`: earliest non-demo member not on `AGENT_DOMAIN`),
and each uploaded event carries `userData.userIdentifiers[].emailAddress` = SHA-256 of the address
normalised by Google's rule (trim, lowercase, drop dots in the local part for gmail.com and
googlemail.com), alongside the click id or instead of it. Google requires:

- top-level `"encoding": "HEX"` whenever any event carries `userData`; it is added only then.
- `consent: {adUserData: CONSENT_GRANTED, adPersonalization: CONSENT_DENIED}` on each such event,
  for EEA users. The basis is the privacy policy; personalisation is always denied because the hash
  is for measurement only.
- the Ads account must accept the Customer Data Terms and enable Enhanced conversions for leads,
  both UI-only switches. Until then every identifier-bearing event is refused with "hasn't agreed to
  the terms for enhanced conversions", which `_RETRYABLE_ROW_DESCRIPTIONS` treats as transient.

The email is resolved at upload time and never stored on the row. A row with neither a click id nor
a human member is dead-lettered as unattributable; a no-click-id row drained while the flag is off is
parked, never dead-lettered, so re-enabling uploads it.

## Authentication

The uploader holds its own platform credential, `ads_conv_refresh_token` (scope
`https://www.googleapis.com/auth/datamanager`, obtained once by an operator), never a customer's
`oauth_providers.GOOGLE_ADS` connection or `Secret`. It is redeemed with the client that issued it,
`google_ads_client_id`/`_secret`. Do not add `datamanager` to the customer-facing Google Ads
provider: every customer would see it on their consent screen. Data Manager takes no developer-token
or login-customer-id header; a manager account goes in the body as `destinations[].loginAccount`
(`google_ads_login_customer_id`, hyphens stripped), unset for direct client auth, and never inferred
from `Secret.resource_ref`. The access token is cached in module state until 60 s before expiry; a
failed exchange raises and the worker retries next pass.

## Conversion configuration

Operators supply their own destination account and action configuration. Hosted account IDs,
bidding goals and verification evidence live in the private
[conversion runbook](https://github.com/superdesigndev/treg-internal/blob/main/docs/marketing/ads-conversions.md).
