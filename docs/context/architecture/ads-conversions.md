---
title: Google Ads conversion tracking — capture, outbox, upload
status: shipped
sources:
  - src/treg/adsconv.py
  - src/treg/application/signup.py
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

> Scope note: this fragment is the **Google click-id** path (`treg_ad` → `Org.ad_*` → conversion
> upload). Generic traffic-source attribution — `utm_*` and referrer for sponsor links, newsletters,
> directories — is the separate `treg_utm` → `Org.utm_*` path in `web/sitetrack.js`, documented in
> [data-model](data-model.md) and [api](../interface/api.md). The two are independent; a Google ad
> click with utm tags populates both.

# Google Ads conversion tracking

Off unless `google_ads_customer_id` and `ads_conv_refresh_token` are both set (`adsconv.enabled()`) —
keeps the test suite and self-hosted instances from starting machinery that cannot upload. When off,
`/adtrack.js` is an empty no-cache response and attribution cookies are ignored, so nothing is
captured, queued or uploaded; the whole feature is additive. `google_ads_developer_token` is NOT part
of this gate: Data Manager has no developer-token header, so that setting only matters for the
read-side Ads catalog calls (`oauth_providers.GOOGLE_ADS`), a separate credential entirely (below).

## The chain: capture → store → fire → upload

1. **Capture** (`web/adtrack.js`, a first-party script). It reads `gclid`/`gbraid`/
   `wbraid` off the URL — `gbraid`/`wbraid` are what Google substitutes on iOS traffic, and omitting
   them silently drops a large share of mobile conversions — and writes them into a `treg_ad` cookie
   (90 days, the length of Google's click-through attribution window; `SameSite=Lax` so it survives
   the cross-site top-level navigation an ad click is). The cookie records the mutually-exclusive
   field name as well as its value (`gclid|…`, `gbraid|…`, or `wbraid|…`); the old `CLICK_ID|landing`
   shape remains readable as a legacy GCLID. Every page, the signed-in dashboard included, also loads
   `web/gtag.js` — Google's tag in **advanced Consent Mode v2**: every consent signal is queued as
   `denied` before the tag script is appended, so Google sets and reads no advertising cookie and
   receives only cookieless pings, which is what lets it MODEL conversions for visitors who never
   granted anything (a tag that is not loaded sends nothing and models nothing). The dashboard
   includes it with `data-conversion-only` (`send_page_view: false`; Google's own cookieless
   `ccm/collect` consent ping still goes out on every load regardless) and fires exactly one event,
   `treg Signup (web)` (`AW-18392771132/0usqCIeQrO0cELzUrcJE`, action `7745505287`, SECONDARY
   and excluded from the Conversions column, so it never double counts against the outbox below),
   right after the first team exists: from `autoTeam()` (`frontend/src/state/onboarding.js`)
   after `POST /onboard/team` made it, with `transaction_id = treg-web-signup-<org id>` so a retry
   cannot count a team twice. A future consent UI grants
   cookies via `window.tregAdsConsent('granted')`; nothing else depends on it.
   Every public landing surface must load `adtrack.js`; no test sweeps every route, so a new
   landing page adds the tag itself. See `interface/seo.md` for the page-by-page scope.
   `adtrack.js` also restores an existing cookie when the URL has no click id, preserves the id
   on the current URL with `history.replaceState`, and carries it on delegated same-origin link
   clicks (including new links and middle clicks). During that visit it also wraps
   `history.pushState`/`replaceState`, so the dashboard rewriting its own address (to `/app#start`,
   for instance) keeps the id for an in-app browser's "open in browser". External links, mail links, fragment-only links
   and links already carrying any click-id parameter are untouched. Carry runs only for a click
   on the current URL or a matching `sessionStorage` marker from an earlier URL capture in that
   tab; a cookie-only return visit does not rewrite URLs. Blocked session storage does not prevent
   direct URL capture or carry. The existing `utm_content`
   convention carries the original landing value, taking precedence over the destination's
   `data-page`; it is only added or replaced when needed to preserve that value. A new URL click
   wins over the cookie. A `|` inside the landing is kept, on both the script and the server
   parser. Restoring a cookie alone does not renew its expiry. `/login` preserves its
   query on the dashboard redirect, and the CLI login shell loads the capture script too.
   **The short install link** covers the sign-in that happens in a browser the ad never touched
   (an agent's embedded browser holds its own cookie jar). When `/start` sees the
   `treg_ad` cookie it asks `POST /i` for a six-character code (`signup.mint_ad_link`: the cookie's
   normalised value under the hashed code in the `Ephemeral` store, namespace `ad_link`, 30 days,
   30 codes per address per 10 minutes) and swaps the copied command to
   `curl -fsSL {BASE}/i/<code> | sh && treg login`. `GET /i/<code>` serves `install.sh` with the
   code templated in, and the script leaves it in `~/.treg/ad-link`; `treg login` sends it as
   `/login?cli=…&ad=<code>` and deletes the file once the login succeeds; `/login` redeems it
   (`signup.redeem_ad_link`) and sets `treg_ad` in the response, the shape the script writes, so
   carry, stash and both signup doors run unchanged. A code redeems once: the first `/login` deletes
   it, so a command pasted into a README or a prompt carries the click to nobody else. The restored
   cookie lives until 90 days after the code was minted, never a fresh 90 days, and a browser that
   already holds `treg_ad` keeps its own. An unknown, expired or redeemed code installs exactly like
   `/install.sh` and restores nothing. No click, or capture off, means the
   plain command. The CLI never sends a value that is not a well-formed code.
2. **Store** (`application.signup._ad_attribution_from`, read at both signup doors: `register_user` (`POST /users`)
   and `create_org` (`POST /orgs`), since a browser visitor who clicked an ad can land on either).
   The cookie is decoded and persisted onto the new `Org`: `ad_gclid` (the historical column name,
   now holding any supported click id), `ad_click_id_type`, `ad_landing` (the use-case page slug or
   `ref`/`utm_content`), `ad_click_at`. Once set, never overwritten — attribution is "first click that
   led to a signup," not "most recent."
   `stash_ad_click` observes valid cookies only once the email is proven: after a successful
   email-verify, Google or GitHub callback. Email-start does not stash, since anyone can type any
   address there and tie their own click to it. `AdClickStash` uses trimmed, lowercased email as its primary key
   (dots and aliases remain distinct), capped and percent-encoded cookie fields, and naive UTC
   creation/update timestamps. An atomic upsert replaces the prior observation. Capture and lookup
   are disabled with `adsconv.enabled()`. `_ad_cookie_for` falls back to this first-party stash
   at both signup doors only when the request cookie is empty, and ignores observations older than
   90 days. A signup that stores a click on its team deletes that email's row in the same
   transaction, and `treg-worker admin purge-ad-clicks` (schedule it daily) deletes rows whose
   `updated_at` is past the 90-day window, so expiry limits retention as well as lookup. This
   association goes through neither audit nor analytics. Stash failures do not fail sign-in;
   the warning omits the email, cookie, and exception text (which could contain SQL parameters). Revision `0075` adds the table without a
   backfill or startup schema mutation. The privacy policy deliberately does not add a separate
   paragraph for the URL carry or the email association (an owner decision); its existing
   `treg_ad` section is the disclosure.
3. **Fire** — three chokepoints call `adsconv.queue(db, org, action, ...)`, which writes an
   `AdConversion` outbox row inside a `SAVEPOINT` (a nested transaction, not a bare flush or a
   `db.rollback()` — this runs inside the CALLER's transaction, and a plain rollback on a duplicate
   would roll back their work too, e.g. undoing a Stripe credit on a redelivered webhook):
   - `application.signup._grant_signup_promo` calls `ACTION_SIGNUP` before `ledger.grant()`.
   - `api._record_first_call` → `ACTION_FIRST_CALL`, on the org's first successful `/call/`.
   - `billing._credit` → `ACTION_PAID`, on the org's first credited top-up, carrying `value_usd_micro`.
   `queue()` no-ops (returns `False`) when tracking is disabled, for demo teams, and — unless
   Enhanced Conversions for Leads is on (below) — for an org with no `ad_gclid`, so by default the
   conversion side stays ad-attributed-only while the product metric it rides alongside
   (`first_call_at`) is set for every org. With the flag on, every team with a human creator is
   queued and identified by hashed email instead.
4. **Upload** (`adsconv.worker`, started from `lifespan` when `adsconv.enabled()`, drains every 300s).
   `drain_once` selects due rows — neither uploaded nor terminal, older than a 6-hour delay, and past
   `next_attempt_at` — and POSTs one batch to the Google Ads API `uploadClickConversions` with
   `partialFailure`. The payload uses the stored click-id field; a braid is never mislabeled as a
   GCLID. Results are acknowledged per operation index: a successful sibling is marked uploaded, a
   failed sibling is not. `CLICK_CONVERSION_ALREADY_EXISTS` is also acknowledged because Google has
   already stored it. HTTP failures, batch/unparseable responses and explicitly transient row errors
   retry indefinitely with exponential backoff capped at 24 hours. Other indexed per-row errors retry
   through eight attempts, then retain the row as a visible dead letter (`failed_at` + `error`). The
   six-hour delay exists because a click id may not be accepted immediately after the click; uploading
   too early can be rejected.

## Enhanced Conversions for Leads: the hashed-email identifier (2026-09-21)

Without URL carry or an email-to-click association, a click id only connects a conversion to a click in the same browser. The YouTube
in-stream campaign (24217105891) is 99.9% mobile clicks against a product whose first step needs a
terminal, so the journey that matters — a pre-roll watched or tapped on a phone, the team created on
a laptop hours later — has no click id at either end. Fourteen days and A$2,191 of that campaign
produced zero orgs carrying a Demand Gen click id; that was a real zero for click-through and a
blind zero for everything else.

`ads_conv_user_data` (default OFF) turns on Google's answer to that: **Enhanced Conversions for
Leads**. With it on, `queue()` also records signup / first-call / paid rows for teams with no click
id, provided the team has a **human creator** (`human_owner_emails`: the earliest non-demo member by
membership id whose address is not on `AGENT_DOMAIN` — agent identities are unroutable and can match
nobody, and 15% of paying teams are agent-owned), and every uploaded event carries
`userData.userIdentifiers[].emailAddress` = SHA-256 of the **normalised** address (`normalize_email`:
trim, lowercase, and for gmail.com / googlemail.com drop the dots in the local part — Google's rule,
because Gmail ignores them) next to the click id when there is one and instead of it when there is
not. Google matches the hash against its own signed-in accounts, which covers a signed-in YouTube
viewer's engaged view or click on any device, ad blockers or not. Each such event also carries
`consent: {adUserData: CONSENT_GRANTED, adPersonalization: CONSENT_DENIED}` — required for Google to
use an EEA user's identifier; treg's basis is the privacy policy every signup accepts (§07 and the
Google Ads processor row name this exact disclosure), and personalisation is always denied because the
hash exists for measurement, never for audiences.

The email is resolved at **upload** time (`drain_once` → `human_owner_emails`), never stored on the
row: the outbox holds no address, hashed or not. If the creator has left by upload time, the team's
earliest remaining human member is uploaded instead; a known approximation, since orgs record no
creator id. A row with neither a click id nor a human member is dead-lettered with
`error = "outbox row has no attributable org: no click id and no human creator email"`. A row with
no click id drained while the flag is OFF is parked a day at a time, never dead-lettered, so turning
the flag back on uploads it.

**Two account-side switches gate the flag**, both UI-only (the API reads them back but cannot set
them): the Ads account must accept the *Customer Data Terms* and enable *Enhanced conversions for
leads* (Goals → Conversions → Settings). Until then Google refuses every event that carries an
identifier — verified live with `validateOnly` on 2026-09-21: the click-only request returns 200, and
the same request plus `userData` returns 400 `events.events[0].destination_references[0]: The
destination account hasn't agreed to the terms for enhanced conversions.` That description is in
`_RETRYABLE_ROW_DESCRIPTIONS`, so rows queued before the click keep retrying (24-hour backoff cap)
instead of dead-lettering after eight attempts. The same probe found that any request carrying
`userData` must also carry a top-level `"encoding": "HEX"` (`events.encoding: Required field is
missing`); `_payload_and_rows` adds it only then, so the click-only body is byte-for-byte unchanged.

## Authentication: a platform credential, not a customer's OAuth connection

Two different purposes were once conflated here and no longer are. A **customer** connecting their
Google Ads account (`oauth_providers.GOOGLE_ADS`) so their agent can read campaign data is one thing;
**treg** uploading its own marketing conversions to its own ad account is another. The uploader does
not borrow the customer-facing provider or any per-org `Secret` — it holds its own PLATFORM
credential, `settings.ads_conv_refresh_token`, obtained once out of band by an operator via the OAuth
playground with scope `https://www.googleapis.com/auth/datamanager`. `_auth_headers` exchanges it
directly against `https://oauth2.googleapis.com/token` (`grant_type=refresh_token`), redeemed with
the SAME client the refresh token was issued against — `google_ads_client_id`/`_secret`, reused from
the read-side Ads OAuth client rather than a new one — never the shared `google_client_id` and never
a customer's connection. `google_ads_customer_id` is the target Ads account. Data Manager takes
neither a developer-token header nor a login-customer-id header at all: `google_ads_developer_token`
plays no part in this path, and the manager (MCC) account moves into the request BODY as
`destinations[].loginAccount` (`google_ads_login_customer_id`, hyphens stripped) instead — set only
for manager-account auth; direct client auth leaves it unset. (`Secret.resource_ref`, where it
exists on an unrelated per-org connection, is never the source for this — it names a discovered
target client, not a manager.)

The exchanged access token is cached in `adsconv` **module state** (`_cached_access_token`,
`_token_expires_at`) and reused until within 60 seconds of its ~3599s expiry. `worker` drains every
300s, so most drains hit the cache; re-exchanging the refresh token every pass would be wasteful and
risks Google's refresh rate limit. `test_access_token_is_cached_and_not_re_exchanged_within_its_lifetime`
proves a second same-window drain makes zero additional token calls. A failed exchange raises
`RuntimeError`; `drain_once` runs inside `worker`'s try/except, so that just retries next pass rather
than crashing the loop.

Before this rework, `oauth_providers.GOOGLE_ADS` carried `datamanager` alongside `adwords` so the
uploader could borrow a customer's OAuth connection — but `listing()` shows every provider to every
customer, so that put a marketing-only permission on every consent screen for no reason (`adwords`
alone was confirmed, via a `validateOnly` `userLists:mutate` call, to already cover audience/
customer-match writes). The provider is back to exactly `adwords`; `datamanager`'s `SCOPE_LABELS`
copy stays (harmless, and correct if the scope is ever genuinely requested).

## Idempotency: the outbox's unique constraint, not a check-then-insert

`AdConversion` has one row per `(org_id, action)` (`uq_adconversion_org_action`) — this, not an
application-level check, is what makes every fire site safe under a retried signup or a redelivered
Stripe webhook: `queue()` tries the insert and treats the resulting `IntegrityError` as "already
recorded," rather than racing a SELECT against a concurrent insert.

## Durable outbox, deliberately NOT audit.py or analytics.py

`audit.py` and `analytics.py` are both deliberately **droppable** — `audit.py` sheds rows past its
queue bound, `analytics.py` is lossy by design — because losing a `CallRecord` or a PostHog event
costs nothing but a metric. A lost `AdConversion` is different: it is a conversion Google never learns
about, which trains the campaign's bidding on undercounted data, silently, in the direction that makes
the campaign look worse than it is. So the write is durable (a row, in the firing code's transaction)
and only the upload is best-effort/retried. Nothing in `adsconv.py` routes through `audit.py`.

## `first_call_at` is NOT derived from `CallRecord`

`Org.first_call_at` is set by a conditional `UPDATE` in `api._record_first_call`, not read off the
audit table. `audit.py` sheds rows under exactly the load that makes "first call" data most valuable —
a busy launch — so deriving the flag from `CallRecord` would undercount precisely when traffic is
highest. `_record_first_call` runs on its **own session**, opened fresh via `session_maker()`, and
never raises into the caller's response. This is deliberate: an earlier version committed on the
request's own `db` session mid-settlement (the proxy call was still being billed) and broke 8 billing
tests, because that reaches into the money transaction from outside `domain/money` — the same rule
`money.md` states for ledger writes applies here by extension, even though `adsconv.py` itself never
touches balance.

## Atomicity: two of three fire sites are atomic with their event, one is not

- **`signup`** — atomic. `adsconv.queue()` and `ledger.grant()` both stage in `_grant_signup_promo`,
  and its one commit lands the user claim and credit together with the conversion when eligible.
  Conversions remain per-team even when a user is unverified, already claimed, legacy-ineligible,
  or the grant amount is zero; a conversion is not evidence of credited money.
- **`first_call`** — atomic. `_record_first_call` queues the conversion and commits once, on its own
  session.
- **`paid`** — **not atomic**. `billing._credit` commits the credit first, then queues the `paid`
  conversion and commits that separately. A crash between the two commits loses the conversion
  permanently: a Stripe webhook redelivery finds the payment already credited (`fresh` is `False`),
  and the fire site that would have queued the conversion never runs again.

  This gap was found in review and the decision (2026-08-17) was to **accept it and document it
  honestly** rather than restructure `domain/money` to make the credit and the conversion one
  transaction. The cheap fix, if the gap ever matters in practice: a reconciliation sweep over orgs
  that have a credited payment (a `CreditBlock`) and a `gclid` but no `paid` `AdConversion` row, in
  the shape of `reconcile.py`'s other read-only reports. Not built.

## Fixed FX: 1 AUD = 0.70 USD, set 2026-08-17

`usd_micro_to_aud_micro` converts at a **constant** rate (`AUD_PER_USD_NUM=10, AUD_PER_USD_DEN=7`),
deliberately not a live rate, so a change in reported ROAS means the business moved, not that the
currency market did. Integer arithmetic throughout (`usd_micro * 10 // 7`) — the one permitted float
is at the JSON boundary in `build_payload`, because the Ads API's `conversionValue` field is a wire
double; the value that reaches it is computed from the already-integral micro amount, never the other
way around. The outbox stores the original USD amount, never AUD, so a future FX correction doesn't
need to rewrite history — conversion happens once, at upload time.

## Conversion configuration

The uploader records `signup`, `first_call` and `paid`. Operators supply their own destination
account and action configuration. Hosted account IDs, bidding goals and verification evidence live
in the private [conversion runbook](https://github.com/superdesigndev/treg-internal/blob/main/docs/marketing/ads-conversions.md).

## API version

The uploader pins Google Ads API **v25** (`adsconv.API_VERSION`). v21 was sunset 2026-08-05; the pin
moved to v25 on 2026-08-17 across every place a version is hard-coded (`oauth_providers.GOOGLE_ADS`,
`.agents/skills/google-ads/SKILL.md`, this repo's catalog yaml) — see `architecture/auth-secrets.md`
for the full four-places-at-once list and the two-failure-modes note (a dead version returns a typed
`UNSUPPORTED_VERSION`, not the HTML 404 a never-existent version returns).

## Testing: one database per test process

The suite's default SQLite files live under `$TMPDIR/treg-tests/`, one per test process (named by
pid, removed at exit), out of the repo tree so file watchers stay quiet. Concurrent runs in one
checkout therefore never share a file. Before this, two runs against one file wiped each other's
tables mid-test and surfaced as a misleading `no such table` error that looked like a flake.

## Not built

- **No router / auto-failover** — not applicable here (there is one destination, Google Ads), but
  stated for consistency with the rest of the catalog: treg does not model automatic choosing.
- **The reconciliation sweep for the `paid` atomicity gap** (above) — named as the cheap fix, not
  implemented.
- **The live-click verification** — needs a real ad click, cannot be automated, and runs after this
  documentation lands, before any real spend.
