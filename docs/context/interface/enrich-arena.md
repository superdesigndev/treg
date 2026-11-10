---
title: Enrich Arena - paid comparisons, one-click feedback, and visible waterfalls
status: shipped
sources:
  - src/treg/domain/arena.py
  - src/treg/application/arena.py
  - src/treg/routers/arena.py
  - src/treg/models.py
  - src/treg/alembic/versions/0027_enrich_arena.py
  - src/treg/domain/governance/teams.py
  - src/treg/routers/auth.py
  - src/treg/bootstrap.py
  - src/treg/web/enrich-arena.html
  - src/treg/web/enrich-arena/arena.js
  - src/treg/web/enrich-arena/bench.js
  - src/treg/web/enrich-arena/arena.css
  - frontend/src/agent-setup/index.ts
  - src/treg/application/arena_verification_insights.py
  - src/treg/alembic/versions/0029_arena_verification_snapshot.py
  - scripts/import_arena_verification.py
  - tests/test_arena_verification_insights.py
  - src/treg/application/arena_insights.py
  - src/treg/domain/arena_insights.py
  - src/treg/alembic/versions/0028_arena_insights.py
  - tests/test_arena_insights.py
  - src/treg/web/logos/apollo.svg
  - src/treg/web/logos/branddev.svg
  - src/treg/web/logos/companyenrich.svg
  - src/treg/web/logos/findymail.svg
  - src/treg/web/logos/hunter.svg
  - src/treg/web/logos/icypeas.svg
  - src/treg/web/logos/leadmagic.svg
  - src/treg/web/logos/leadsforge.svg
  - src/treg/web/logos/lusha.svg
  - src/treg/web/logos/pdl.svg
  - src/treg/web/logos/predictleads.svg
  - src/treg/web/logos/thecompaniesapi.svg
  - src/treg/web/logos/tomba.svg
  - src/treg/web/sitetrack.js
  - tests/test_enrich_arena.py
related:
  - architecture/catalog.md
  - architecture/money.md
  - architecture/composition.md
  - interface/dashboard.md
---

# Enrich Arena

`/enrich-arena` (Arena), `/enrich-arena/leaderboard` and `/enrich-arena/people-search-bench`
(Benchmark) are one public standalone Vue app (`web/enrich-arena.html`, `arena.js`, `bench.js`).
Visitors pick a task, input shape, mode (Waterfall or Battle; Battle is the internal `compare`
value) and vendors before signing in; running is authenticated and metered against team credits.
`?capability=<id>&mode=waterfall` opens a task directly.

## Page rules a change must keep

- **Nothing spends without an explicit click.** Examples (`domain.arena.example_inputs`, exposed by
  `/arena/tasks`) prefill but never dispatch; login never dispatches; there is no automatic retry.
  Valid inputs fetch a quote after a pause; any change to identity, mode, team, budget or vendors
  invalidates it and late responses are discarded. An absent or expired quote is refreshed on the
  Run button before it spends. A completed run consumes its quote.
- **Public estimates** (`/arena/tasks`) come from verified adapters, canonical derivations and
  request pricing with margin: no team reads, stored quotes or upstream calls. A team quote replaces
  them with the real waterfall order and own-key prices.
- **Leaderboard and Benchmark are read-only**: no draft restore, run resume, history fetch or quote.
- **Insufficient credit** opens `/app#billing` with the Arena team selected (dashboard
  `treg-active` preference) and the draft preserved; a 402 at start does the same.
- **Support chat** boots only after `/auth/me` establishes a user and `/meta` supplies
  `intercom_app_id`; email and company are sent only with `intercom_user_hash` (identity
  verification), otherwise chat stays anonymous. Logout, expiry or an account change shuts the
  session down. Widget failure never blocks Arena.
- **Sign-up** opens the dashboard's agent setup modal next (`frontend/src/agent-setup/`, compiled to
  `/agent-setup.js`, shared with dashboard onboarding: a markup change is checked on both surfaces).
  Signed-in visitors without a team create one there; authenticated users never fall back to guest
  instructions. The CLI token is fetched for the active team, masked, copied only on click, never
  persisted, and cleared on close; stale responses cannot restore a closed or wrong-team token.
- **OAuth return paths** are allowlisted (Arena, Leaderboard, Benchmark and Web Arena paths with
  task/run/team query parameters), checked before redirect and again at callback after decrypting
  the Fernet-encrypted return cookie; missing or tampered cookies fall back to the dashboard.
  Company SSO carries the same validated path in its encrypted state cookie.
- **URLs.** Task and input changes push capability/variant into the URL. A bare visit may restore a
  recent draft but never silently reopens a session. `/enrich-arena?run=<id>&team=<slug>` loads a
  saved run through owner-scoped `GET /arena/runs/{id}` for a team the user belongs to; these are
  private bookmarks, not share links, and a missing run shows an error instead of another result.
  Inputs never go into URLs.
- **Result honesty.** A hit means returned data, not verified correctness: found work email is not
  deliverability, found phone is not a live line. Work-email rows flag a returned domain different
  from the requested company domain (subdomains accepted) without altering the answer. The identity
  notice on person results flags disagreement but never picks a vendor or rewrites an answer. A
  vendor's HTTP 402 (`upstream_status`) is shown as that vendor's exhausted allowance, never as a
  team top-up prompt. Email verdict display: catch-all/accept-all and explicit risky statuses win
  over the adapter's `valid` boolean; unknown and unrecognized statuses stay neutral, never invalid.
- **Awards** never rank unrated vendors above a thumbs-up, never appear while running, and are
  withheld when the metric (duration, settled charge) is unknown; ties share. Batch Battle awards
  need a complete cohort and count only kept results (hits without a thumbs-down); Waterfall gets
  no comparative awards because later vendors see only the unresolved subset.
- **Request a vendor** posts to the open, rate-limited `/tool-requests` with `source: web` and the
  task id; it never copies enrichment inputs or starts a run.

## Tasks and inputs

Tasks and their catalog capabilities live in `domain.arena.TASKS`; the catalog's contracts and
adapters define inputs and normalized fields. Each provider contributes one eligible endpoint whose
adapter verifies the task contract; providers join through adapter categories, never through
provider branches in Arena. Bulk endpoints and `domain.arena.EXCLUDED` (personal-email finders)
stay out. Asynchronous submissions participate through the same planner (Arena submits, polls and
normalizes internally).

Validation happens before pricing or charging: name-based tasks need first and last name; emails
need a mailbox and dotted domain; malformed domains or LinkedIn URLs, invalid ports, embedded
credentials, non-web schemes and malformed bracketed hosts are rejected. The server repeats
validation after normalizing; blank, incomplete or duplicate entries block submission.

Discovery (`people.search`, `companies.similar`) is a direct catalog query: no agent harness,
model-written plan, pagination or follow-up enrichment. The retired `people.company.search` task
still resolves through `domain.arena.catalog_capability` for saved runs. Previews and plans exclude
adapters that drop requested identity constraints or filters (including the result limit); unknown
country codes are rejected rather than omitted. The displayed count is the normalized list, not an
upstream total; batch coverage and cost-per-kept use queries as the denominator; Waterfall stops on
a usable nonempty list, which is not a relevance judgment. Discovery is excluded from the
enrichment insights aggregate.

## Plans, runs and money

`POST /arena/plans` validates input, applies the team's routing eligibility and freezes the exact
adapter requests, endpoint/adapter hashes, identities, ordering, estimates and budget in an
encrypted `ArenaRun`. It takes `identity` or `identities`, never both; every entry shares one input
shape and provider cohort, each attempt carries `entry_index`, and the payload keeps `identity` for
compatibility. A quote expires quickly and reserves nothing. Battle admits on the sum of estimates;
Waterfall sorts by quoted price and admits on the cheapest first step per entry. These are admission
limits at quoted prices, not a guarantee against a higher provider-reported charge, and the UI says
so. An unaffordable batch is refused whole, never partially bought.

`POST /arena/runs/{id}/start` rechecks catalog hashes and credit, then a conditional update claims
the run once across workers. An in-process owner task runs it: Battle in bounded concurrent legs,
Waterfall serially (a batch Waterfall completes each step across the list before advancing
unresolved entries). Each leg captures current membership and policy (`_fresh_caller`, which also
rechecks that an initiating managed key is still active and in the same membership before every
paid step) and calls `application.call.service.execute_call` on the direct endpoint. The ordinary
call runtime owns pricing, credential priority, authorization, reserve, settle and cancellation;
**Arena never writes balances or holds.** Own keys stay unmetered. Overflow is disabled
(`platform_overflow_disabled` on the snapshot) and the archive is bypassed (`cache-control:
no-cache`) so runs measure fresh calls.

- DB sessions are short and closed before upstream I/O and polling waits; attempt state and call
  references persist before dispatch and after every attempt, so refresh resumes observation
  without spending.
- Legs and runs have deadlines (`RUN_SECONDS` per entry); a process-lost run becomes interrupted
  after its persisted deadline and is never retried. Unknown charges stay unknown until a durable
  ledger settle/release resolves them.
- Run starts are rate-limited per user with an active-run cap; start admission serializes on the
  user row across drafts and teams; pricing stays available at the limit. Old unused quotes are
  retired per user.
- Run completion signals the cancellation poll and waits for its read to close before the final
  save; never cancel that poll inside DB I/O, because under SQLite's rollback journal a cancelled
  cursor can keep a read lock that blocks the final commit
  (`test_run_finishes_while_cancel_poll_is_reading`).
- Waterfall order is ascending quoted price (planner order on ties) with the bounded error-fallback
  policy, stopping at the first structural hit (the contract's required fields). A negative
  mailbox verdict is a successful answer. An async attempt not provably terminal is saved as pending
  with its reservation, task id and call reference; pending stops only that entry's Waterfall.
  Internal polls continue an admitted submission without consuming another endpoint allowance or
  depending on quota Redis, but still pass ordinary authorization and ownership checks. Terminal
  attempts show the settled charge, not the reservation.
- Raw response snapshots share a per-run allowance (`MAX_BATCH_RAW_BYTES`) split across attempts and
  capped per response (`MAX_RESULT_BYTES`); an oversized response is still classified and
  extracted, with `raw_omitted` explaining the missing snapshot.
- `domain.arena.safe_output` normalizes fields before classification and on presentation: masking
  booleans never become text fields, domains become hostnames, LinkedIn handles become canonical
  URLs, and malformed or non-web addresses are dropped. Masked required fields cannot qualify as
  hits. Stored snapshots, charges and outcomes stay intact.
- The run cost summary uses the server's settled run total (verification and misses included);
  the per-result average divides by distinct entries with a found, non-rejected result.

**Additional calls.** Only uncalled (`not_attempted`/`skipped`) results get a priced Try; completed
attempts never get a rerun. `.../attempts/{attempt_id}/plan` rechecks access and stores a short
quote without reserving; the UI dispatches only at or below the displayed estimate. The matching
start locks the run, rechecks quote, hashes, credit and the active-run cap, and claims the attempt
once, reopening a completed session or joining a running one. It is admitted separately from the
original Waterfall budget and marked `manual: true`. Persistence locks the run and merges only that
worker's attempts; a run becomes terminal only after all active attempts finish.

**Ratings and feedback.** Thumbs save immediately (`.../rating`) into the attempt's encrypted
payload, idempotently; thumbs-down optionally opens reason/note through `/report`, whose first
submission is idempotent and never overwrites an upvote from another tab. Payload reads, ratings,
reports and manual claims serialize on the run row. Feedback never changes the vendor output, hit
classification or charges. The legacy `POST /arena/runs/{id}/evaluations` (and `/reveal`) API
remains for compatibility; its first evaluation is immutable (run-row lock and unique run id).
Version 2 evaluations carry `feedback_context: attributed` with the cohort fingerprint, exposed
candidates, outcome and adapter versions, distinguishing visible-vendor preferences from historical
blind votes; `revealed_at` no longer gates visibility.

**History.** `GET /arena/runs` returns an array, default 30, with optional `limit` and `before` (an
owned, retained run id) paginating by descending creation time and id. History and export never
call vendors.

## Verification after lookup

Find work email and Find phone number verify found contacts by default; the choice is bound into
the server quote, and restored drafts and historical runs keep their recorded setting. Email checks
call `treg.people.email.verify` through the ordinary routed runtime under a quoted ceiling (own keys
cost zero), with the frozen provider set, catalog hashes and route max-cost header bounding
execution, so newly added providers cannot enter an existing quote. Phone checks are a direct call
to the single integrated phone verifier. Lookup misses incur no check, and verification never
starts another lookup. Battle quotes include a ceiling per vendor and entry, Waterfall one per
entry.

The explicit Verify action is a short, owned, same-origin quote reading the contact from the
encrypted stored result, claiming a nested attempt once. All checks use the normal
reserve/settle/release runtime, nested charges count in totals, and recovery never retries paid
work.

For phone verification, `verification_identity` carries a lookup's reported country into the check;
an explicit international calling code wins. A national number with no usable country context is
not checked or charged and is marked `not_started` ("Verification not run"); no default country is
guessed. Phone validation establishes numbering-plan validity, never that the line is live or
belongs to the person.

An explicit invalid/undeliverable **email** verdict automatically files an `incorrect_data` report on
the original lookup inside the locked backend merge (works without a browser, idempotent across
saves), with `automated_verification` provenance. Catch-all, unknown, risky, failed checks and phone
verdicts never trigger it; the original contact, outcome and billing are unchanged.

## Historical vendor insights

The vendor pricing table and Leaderboard show observed hit rate (verdict rate for verification),
median response time and price, matched by task, input shape and exact endpoint id; this evidence
never changes quoted order, selection, dispatch or awards. They are observational results, not a
controlled benchmark, and the page must say so; verification rates measure returned verdicts, not
accuracy. Rates below the sample threshold are withheld (a dash, no label); genuine zeros show.
Leaderboard prices are public per-entry catalog estimates; all chart geometry is derived in the
browser from the loaded aggregate.

`GET /arena/insights` reads the current version's persisted aggregate by primary key with its
window, actual first/last observation dates and status, exposing only public aggregate fields. If
that version has not published, one bounded query serves the latest completed publication of a
previous version, ordered by the payload's publication timestamp (the worker clears the cursor's
`updated_at` during refresh); the current version wins as soon as it publishes, even empty. A fresh
database returns `warming`. No production metrics ship in assets or fixtures.

`application.arena_insights.drain` is the collector, run by the `treg-worker arena insights` cron.
It must not run in web processes: every web process would contend for the cursor row and walk
`callrecord` on the money-path database. Once caught up it re-aggregates only every
`REFRESH_SECONDS`, because each aggregate scans the whole window. The rewind that revisits recent
evidence is constrained to Arena endpoints so it rides `ix_callrecord_endpoint_id_created_at`.

- It reads a bounded batch of audit records and archive pointers, closes the session, resolves
  bodies through `archive_bodies.read` (path `arena`), then reacquires the cursor lock and
  revalidates before publishing; no cursor lock or DB connection is held across object-store I/O,
  and evidence from a superseded batch is discarded.
- It reclassifies with current rules and upserts anonymous `ArenaObservation` facts. It never calls
  vendors or trusts `CallRecord.hit`. Missing exact content stays unresolved; a newer different
  answer never substitutes. Invalid compressed bodies stay unresolved per record.
- The values CTE needs SQLAlchemy 2.0.42+ (the server dependency floor).
- The version is a hash of `domain.arena_insights.RULES_VERSION` and `domain.arena.VERSION`: bump one
  when classification changes and the collector rebuilds. Catalog changes apply at aggregation time,
  not by moving the cursor. `prune` (every pass) drops current-version facts outside the window and
  every superseded version's facts, and deletes superseded state rows once the current version has
  a ready payload; a superseded row's cursor is reset with its facts so a rolled-back collector
  rebuilds before publishing. Partial initial history is withheld; a refresh failure keeps the last
  aggregate and its timestamp.

Coverage is structurally usable results over usable results plus genuine misses, using the latest
decided observation per endpoint/input/request hash. Wrong requests, rate limits, balances,
access/service failures, refusals and pending results are excluded; ambiguous errors, unavailable
bodies and missing request hashes are unresolved. Own-key, overflow and cached scopes are omitted.
Async endpoints are omitted (their first answer is a task id) except rows declaring
`terminal_on_submission`, whose finished first answers count and whose "still working" answers are
`unresolved_pending`, never a miss. Late evidence beyond the revisit window waits for a rebuild;
lossy audit cannot establish complete traffic.

### Published verification pilot

`application.arena_verification_insights` publishes aggregate-only data in
`ArenaVerificationSnapshot`; `GET /arena/insights` attaches the latest as `verification`. The
rolling worker cannot overwrite it; reimporting an identical run is a no-op and different contents
under the same run id fail. **Email validity rate** is the share of sampled distinct returned emails
labelled valid by both verifiers over completed checks (risky, unknown and conflicting stay in the
denominator; unfinished checks are excluded); it is verifier agreement, not delivery or ownership.
Phone gets a separate `format_validity_rate` for `phone_format` rows, never promoted into email
validity or hit rate. The legacy `rate` projection stays for older consumers, but the UI reads only
`validity_rate` and `format_validity_rate`, so old snapshots cannot display a lookup rate as
validity.

The data stays outside the checkout: `scripts/import_arena_verification.py` (`--check` validates
only) accepts schema-whitelisted aggregates, dates, endpoint/input ids, verifier names and counts,
and rejects raw contact fields.

### People Search Bench

The Benchmark page loads the same-origin `/people-search` page once and extracts its published
scores with `ArenaBench.parseDocument`; scores are never copied into a second dataset. Incomplete or
invalid scores show an error, never partial charts. The original benchmark repository's figures
differ from the landing's; the page discloses that, attributes both, and never manufactures an
overall score or presents the numbers as vendor hit rates or verification accuracy.

## Privacy and operation

Runs and evaluations belong to their creator and active team; another team member cannot read the
creator's runs. Inputs, snapshots and comments are encrypted with the Fernet key. Access expires
after `RETENTION_DAYS`; a bounded lazy sweep on history/quote requests deletes expired runs and
evaluations. Team deletion removes evaluations before runs. The API never exposes identities,
hashes, org ids or bodies in aggregates.

The page loads `sitetrack.js` with `data-private-page` (no autocapture, no session recording).
Mutating endpoints use the same-origin guard. Arena routes belong to the control role; `/call/` is
unchanged.

### Event contract

`TregTracking` (`sitetrack.js`) identifies the email and active `team` group after `loadIdentity`;
team switches clear the group and account changes the identity. Events carry product metadata only,
never enrichment inputs:

- `arena_page_viewed` (once per page at mount, `surface=arena|leaderboard|benchmark`) and
  `arena_signup_opened`; `entry_surface` is the first observed surface from a first-party cookie,
  distinct from UTMs and referrer.
- `signup_completed` is emitted by email OTP and OAuth provisioning only after commit and only for a
  new User. `client` is `cli` for the terminal's email door, `cli_browser` for a browser sign-in
  that completes a `treg login` (the pairing state rides the email code or the social callback),
  else `browser`.
- `arena_run_started` after a successful claim (never for quotes or duplicate starts);
  `arena_run_completed` after saving the initial run's terminal state, with `successful_call` and
  `returned_data`. Manual Try and verification emit only ordinary `tool_called` with
  `client=enrich-arena`. Completion after a crash is not reconstructed.
- `checkout_source=arena` survives SPA navigation to both top-up entry points and rides Stripe
  Session and PaymentIntent metadata so either webhook order works; the ledger is the durable payment
  source, PostHog is best-effort.
