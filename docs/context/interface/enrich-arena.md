---
title: Enrich Arena — paid comparisons, one-click feedback, and visible waterfalls
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
  - tests/js/arena-bench.test.cjs
  - tests/js/arena-template.test.cjs
  - src/treg/web/enrich-arena/arena.css
  - src/treg/web/agent-setup.js
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
  - tests/js/enrich-arena.test.cjs
related:
  - architecture/catalog.md
  - architecture/money.md
  - architecture/composition.md
  - interface/dashboard.md
---

# Enrich Arena

`/enrich-arena` and `/enrich-arena/leaderboard` share a public standalone Vue app, with
Arena | Leaderboard navigation centered in the account header: icon links share a rounded
white pill container, with a softly tinted sage active pill and dark green text. On narrow screens the control occupies a centered
second header row. Links preserve task and input type; leaving Arena saves its
query draft. Leaderboard does not restore or overwrite drafts, resume runs, fetch history or
request quotes. It reads the same cached database snapshot as Arena. The account credit badge
is a button opening `/app?from=enrich-arena#billing` through `topUp`, preserving the selected team and Arena draft.
The shared header also links to GitHub ("Open source"), Discord and X using the same icons and
destinations as the people-search landing page. The links remain visible across Arena, Leaderboard
and Benchmark, wrapping with account controls on narrow screens.
Its visual system follows the treg redesign reference (`https://treg-design.vercel.app/#start`):
Geist Pixel headings, Google Sans Flex body text, DM Mono for technical values, a cool gray canvas,
white rounded cards with fine borders, black actions and restrained teal status accents. A static
pixel texture echoes the reference background, while the standalone account header stays compact.
The entry screen leads with “Enrich Arena” and a small “Powered by [treg logo] Treg” label beneath the title. The composer keeps its compact, single-column layout and repeatable input rows.
`ArenaTaskTabs` sits above the inputs in a single compact row with icons and readable action
labels such as “Find work email” and “Enrich person”. A subtle filled state marks the active tab.
The row starts with Find work email, Find phone number, Enrich person and Enrich company, followed
by verification, LinkedIn lookup and the discovery tasks. Every option stays in one horizontally
scrollable row, with full labels and icons, on desktop and mobile. The native scrollbar is hidden;
faded edges with small clickable arrows appear only where more content is available. Scroll and
resize updates keep the arrows in sync, and selected tabs are revealed clear of the arrows. Selecting a task or resizing
brings the active option into view without scrolling the page vertically. Arrow keys and Home/End switch
tasks with roving focus; the input panel is labelled by its selected tab. Clicking the active tab
preserves the query. Add entry, the Using input-type selector and Run share the row below the inputs.
A quiet “or paste rows from a spreadsheet” hint sits immediately to the left of Run.
Visible field labels and the Using input-type selector
remain, with Add entry and spreadsheet paste for batches. Multiple entries share one column-label
row above the aligned inputs; each field retains its own hidden label and entry number for
assistive technology. A single entry keeps its visible labels inside the input row. Discovery adds a short result/batch-limit
note. Fresh Arena visits, new queries and task/input-type switches prefill two editable public
examples from `domain.arena.example_inputs`, exposed by `/arena/tasks`. Saved drafts (including
intentionally cleared inputs) and historical results take precedence. No example dispatches
automatically. Vendor selection resets on task switches. Shared column labels stay aligned with
the fields on mobile as well as desktop. Narrow screens keep wider input grids in a local
horizontal scroll region, with 16px editable text and 44px primary touch controls. The mobile
header keeps the brand, community links and signup/credit controls together above the page tabs.
On narrow phones community links use labelled icons; team selection/sign-out use an account row
when signed in. Comparison filters use a two-column grid.
Result tables keep their own horizontal scroll region and stats retain the vendor column while
scrolling. Expanded answers become cards on small screens; selected overview rows and table
headers stop pinning at 600px so they cannot cover those answers. The provider queue contains
its positioned accessibility labels, avoiding page-wide overflow. Dialogs scroll within the
dynamic viewport and prevent the page behind them from scrolling.
Waterfall/Battle controls are centered in the fighter card, with Waterfall first and step/crossed-swords icons, VS connectors
for battles and directional step arrows for waterfalls. Battle retains the internal `compare` mode value. Changing mode updates the query
and invalidates pricing without dispatching; controls are locked during a run. Old result cards
retain their recorded mode when a new query is being configured. Waterfall is the default; explicit choices in a restored draft or
URL remain respected. The header has no bottom rule and the hero uses tighter spacing.
There is no Options section or editable estimate limit. Vendor avatars act as native toggle
buttons: enabled fighters have a check, excluded fighters remain dimmed and clickable. Selection
updates pricing and the saved login draft without dispatching. All services start enabled; task
or input-type changes reset the selection. No enabled services blocks submission. Avatars lock
during execution, and historical sessions restore their recorded vendor set. The existing API
$10 admission ceiling remains; the page no longer carries a hidden user budget from old drafts.
Selecting a task or input type immediately
shows a compact vendor/pricing table, including before login or input completion. `/arena/tasks`
provides catalog estimates per input variant using verified adapters, canonical derivations and
request pricing (including adapter constants and margin), with no team reads, stored quotes or
upstream calls. A ready team quote replaces these public estimates and supplies the actual
waterfall order and own-key pricing. Matching results replace the preview after dispatch; editing
the query restores its pricing preview while preserving the previous result below it. Results appear below the composer, which stays
visible with its inputs and controls disabled while a run is in progress. After completion, users
can edit the inputs and submit another run. A compact table shows each vendor's bundled logo,
key result fields, status, cost, timing and one-click thumbs up/down actions. Full fields and original
responses expand beneath a row by clicking it or pressing Enter/Space while focused. Row action
buttons act independently without toggling details. Waterfall uses the same table with numbered steps and timing bars;
its stop reason appears as a short footer. Neither mode has a results heading or aggregate-charge
bar. Either thumb records a per-result rating immediately; there is no
selection/submit step, reveal gate, none-useful button or skip button. Work-email rows flag a
returned email domain that differs from the requested company domain (subdomains are accepted),
while preserving the vendor’s answer. This signals a possible mismatch, not automatic invalidity.
Vendor icons are bundled locally from official sites, with source URLs in asset comments.
Original PNG/ICO icons are embedded in SVG wrappers to preserve the shared logo paths; existing
vector marks remain vectors. Brand.dev now redirects to Context.dev and uses its current icon.
Initials appear only as an image-load fallback.
A compact pixel-fighter lineup sits above pricing and results, using those same logos as heads.
It is idle before dispatch and follows each real attempt: running punches, hits celebrate,
misses fall, errors/timeouts show an alert, and uncalled services remain on the bench. Compare
animates concurrent active attempts; waterfall animates only its active step. A current thumbs-up winner receives a crown. A thumbs-down rating immediately overrides the celebration/crown with the
defeated pose and “Thumbs down” label; changing to thumbs up restores the recorded outcome pose.
One thumbs-up makes that result the winner, with a crown and Winner label, in either mode.
Fastest and Cheapest belts still compare successful, non-downvoted results when there is only
one thumbs-up, so those performance badges may belong to a different vendor than the winner. With multiple
thumbs-up results, Fastest and Cheapest compare only those results, and their metric winners get
crowns. No unrated vendor can outrank a thumbs-up vendor. Changing a rating recomputes the winner
and belts immediately; saved historical comparison evaluations remain intact but do not drive
these current winner indicators. Without thumbs-up votes, completed Battles with at least two
successful, non-downvoted results show Fastest and Cheapest
badges as pixel-edged belts across the fighters’ waists and as compact labels in result rows.
The belts move with the sprite and counter-mirror their text for facing opponents. They compare recorded duration and actual settled charge,
respectively; ties share badges, genuine zero charges qualify, and an unknown metric withholds
that metric's award. Running sessions never show provisional awards. Hits indicate returned data, not a ranking or verified correctness. Terminal
runs stop all looping motion; reduced-motion preferences disable animation. The lineup scrolls
horizontally for larger cohorts and has per-vendor accessible status labels.
Results start directly with the fighter card, with no query-summary row, aggregate progress count
or New query link. Inputs remain editable after completion; Stop sits in the fighter toolbar
only during an active run.
Visitors can select a task, input variant, execution mode, and services before signing in.
Submission checks the existing browser session. Signed-out visitors see Sign up. Successful email
or Google/GitHub authentication opens the existing agent setup modal next, without dispatching a run.
A ten-minute, single-use session-storage marker resumes setup after OAuth; ordinary page loads do not
reopen it. OAuth returns allow only Arena, Leaderboard and Benchmark paths with task/run/team query
parameters, validated both before redirect and at callback. The return cookie holds a Fernet-encrypted
target; callbacks authenticate and decrypt it before repeating the allowlist check. Missing or tampered
cookies fall back to the dashboard. A ten-minute session-storage draft
preserves the query through signup. The top navigation also links to `https://treg.to` via Treg.
New users can create a team here. Valid inputs for signed-in
users fetch a quote after an 800 ms pause. The compact Run button displays the estimated cost
(waterfall: “Run from” the cheapest selected vendor’s quoted price); per-service prices appear below the input. The page
has no price-confirmation modal. Clicking a current affordable quote dispatches immediately.
An absent or expired quote is first refreshed on the button; login never automatically spends.
Changes to identity, mode, team, budget or services invalidate the displayed quote and discard
late responses. A completed run consumes its quote; a fresh run gets a new quote.
Insufficient-credit buttons open `/app#billing` with the Arena team selected via the dashboard's
`treg-active` preference, preserving an unsubmitted draft. A 402 at start uses the same path.
The navigation shows a labeled Team switcher only for users with multiple teams; a single team
is selected automatically without an extra dropdown.

Tasks are work email, person enrichment, company enrichment, phone lookup, email and phone verification,
email-to-LinkedIn, people search, people at a company, and similar companies. The catalog's existing contracts and adapters define supported inputs and
normalized result fields. Name-based comparisons require both first and last name, validated
before quote creation or upstream dispatch; use a LinkedIn URL when that input is unavailable.
Email inputs require a nonempty mailbox and dotted domain. Malformed domain/LinkedIn URLs,
invalid ports, embedded credentials and non-web schemes return validation errors before pricing
or charging, including malformed bracketed hosts that URL parsing would otherwise reject with an exception.
Each provider contributes one eligible synchronous endpoint. Bulk jobs,
asynchronous submissions and personal-email finders are excluded from the work-email task.
`?capability=people.email.find&mode=waterfall` opens a task/mode directly.

## Historical vendor insights

Arena shows historical stats only in the existing vendor pricing table, with four columns:
Vendor, Hit rate (Verdict rate for verification), Response time and Price. There is no separate
insights section or highlight card. `insightRows` matches the snapshot by task, input shape and exact endpoint ID;
`previewProviders` attaches that evidence without changing quoted order, avatar selection or
team-specific pricing. Missing endpoints show a dash rather than zero coverage. Billing terms
appear beneath the price. A compact About these stats disclosure includes dates, exclusions and
the aggregate download.

The Arena table includes horizontal bars for rate and response time beside their values.
Rate uses a fixed zero-to-100% scale; durations share a zero-based scale up to the largest known
median, so shorter is faster. Missing evidence has a dashed track rather than a zero bar. The
vendor/price columns remain available. Leaderboard has one centered use-case heading using the same pixel font, weight and responsive
sizing as the Arena heading, with Setup independently aligned to its right
(stacked on small screens), a centered content-width horizontal use-case tab container using the same white rounded shell and sage active pills as People Search Bench, a left filter rail for metric and input type, and one chart canvas, inspired by
Design Arena. The rail stacks above the chart on mobile. It offers hit-rate bars (default),
response-time bars, horizontal price rows (cheapest first) and price versus hit-rate scatter. Price bars
use current per-entry catalogue estimates and include priced vendors without historical samples;
missing or invalid prices are omitted and genuine zero prices remain visible. Each price row
shows the vendor logo, proportional bar and exact USD value. All vendors appear in the vertical
list so expensive vendors cannot be hidden off-screen while determining the scale. Only enrichment tasks covered by the
aggregate pipeline appear there; discovery tasks remain in Arena.

`chartRows` uses the selected Arena vendor cohort or the full public catalogue cohort on
Leaderboard, with the same sample thresholds. Rate bars sort
descending on a fixed zero-to-100% axis; response-time bars sort ascending with a zero-based axis.
Bar columns constrain their inner grid width independently of label length. Bars share a width
cap, vendor names wrap at word boundaries, and large cohorts scroll horizontally rather than
squeezing labels into adjacent columns.
Bar charts omit vendors without usable evidence for the selected metric; genuine zero results remain visible. The table retains every selected vendor. `chartPoints` includes only vendors
with both a usable rate and a current estimated price (including a true zero price). Leaderboard
uses public per-entry catalogue estimates independent of team quotes, batch size and vendor
selection. Charts follow task and input selections without dispatching calls. Vendor buttons and the scatter legend support pointer, keyboard and touch selection, with a
shared rate/time/price detail line and no lookup counts. The scatter uses provider logos in white markers (a provider initial if its logo fails),
retains accessible labels and selection details, and omits unavailable pairs. The chart explains that these are observational results, not a controlled benchmark;
verification rates measure returned verdicts, not accuracy. All geometry is derived in the browser
from the already-loaded aggregate, with no extra requests, chart dependency or embedded metrics.

`insightRate` uses only unique-request coverage; lookup counts are omitted from the table.
Fewer than 20 unique lookups withhold a rate; small-sample labels are hidden.
Verification shows the share returning a verdict, not an accuracy percentage. Response time
is the median of successful calls (including repeats), displayed only with at least 20 hits and
a known timing aggregate. Current prices remain estimates for the selected entries, and own-key
prices retain their no-treg-charge label. Historical stats do not alter dispatch or result awards.

`GET /arena/insights` returns the persisted database aggregate via one primary-key read, with
`updated_at`, source window, actual first/last observation dates, collection status and only public aggregate fields. The disclosure shows recorded dates when observations exist, so a sparse or historical sample does not appear to span the whole rolling window. No production
metrics ship in assets or fixtures. A fresh database returns `warming` with no rows. The UI polls
every two minutes while visible, preserves the last successful values after a refresh error, and
shows the last update time. Prices still come from the catalog and team quote.

`application.arena_insights.worker` runs on control/all roles and uses the background database pool.
It reads 100 audit records per transaction, follows their exact archive key/content and optional body
carrier, reclassifies stored responses with current Arena required-field rules, and upserts anonymous
`ArenaObservation` facts. It never calls vendors or trusts `CallRecord.hit`. No money writes or proxy
changes are involved. Evidence lookup deduplicates key/content pairs and finds each pair's newest
matching snapshot through the existing `(key_id, version)` index. This avoids repeatedly scanning
the global content index for identical responses shared by many requests. Missing exact content
remains unresolved; newer different answers never substitute for historical evidence. The values
CTE requires SQLAlchemy 2.0.42 or newer, reflected in the server dependency floor.
`ArenaInsightState` serializes the cursor across workers and stores the aggregate;
initial history is withheld until the first pass completes. Steady state refreshes about every two
minutes (plus collection time), allowing one minute for audit/archive writes and revisiting ten minutes
of recent evidence. The first backfill may take longer. Evidence that arrives later than this revisit
window remains unresolved until a version-triggered rebuild; lossy audit cannot establish complete traffic.

The source window is 30 days, with old facts pruned. Rule, adapter or contract changes produce a new
version/cursor and rebuild from available evidence. PostgreSQL computes medians and unique-request
counts in SQL; local SQLite uses an exact Python median. The collector owns writes to the two new
tables, created by Alembic revision `0028`. Run `python -m treg upgrade` before restarting the app.
A refresh failure retains the last aggregate and its original timestamp; partial collection is never
presented as a complete window. The API does not expose identities, hashes, organization IDs or bodies.

Coverage is structurally usable results divided by usable results plus genuine misses, using the
latest decided observation per endpoint/input/request hash. Wrong requests, rate limits, balances,
access/service failures, refusals and pending results are excluded. Ambiguous errors, unavailable
bodies and missing request hashes remain unresolved. Own-key, overflow and cached scopes are omitted.
Only inputs attributable to the selected shape appear in that row. Response time includes repeats
but requires 20 successful calls with recorded timing. Different cohorts and waterfall positions
prevent controlled rankings, and returned fields are not independently verified.

## Multiple entries in the same composer

The heading's “Setup treg in” button shows Claude Code, Codex, OpenClaw and Hermes logos plus
the count of other choices. It opens a native dialog using the same `AgentPicker` and
`SetupInstructions` components as the dashboard welcome modal (`agent-setup.js`). The instruction
label sits inside the prompt card alongside Copy, above the setup command. The remembered
`treg-agent` choice, expanded agent list and Grok Bot plugin step are shared. The setup text points
to this deployment's `/llms.txt`. Continuing as a signed-in team member fetches `/auth/cli-token`
for the active team; the token is masked by default, copied only on click, never persisted by the
modal, and cleared when it closes. Stale responses cannot restore a closed or wrong-team token.
Next opens the shared third “Try it out” step: four copyable example prompts, the waiting-for-agent
message, grouped OAuth provider links and Skip/Browse all catalog actions. Copying an example
only writes its prompt to the clipboard; provider links open the main app’s provider page without
starting OAuth. The example definitions and `TryItOut` component are shared with dashboard onboarding.
Signed-in visitors without a team first name and create their team inside the setup modal, matching
the dashboard welcome flow. The agent step then fetches a token for that team; authenticated users
never silently fall back to guest instructions. Anonymous visitors still receive the setup line and
agent-guided login instructions. Opening setup does not start an Arena run or change its inputs/history.

The composer starts with two example rows. Add entry and per-row remove controls expand that same input
to at most 50 enrichment/verification entries or 10 discovery queries, sharing the task, input variant, mode and vendor selection. Spreadsheet
paste accepts TSV/CSV, quoted fields and matching column headers; it replaces the focused row
and inserts the remaining pasted rows beside it, preserving other entries. Overflow and malformed
column shapes leave the input unchanged. Blank/incomplete rows and duplicate identities block
submission; the server repeats validation after normalizing domains, names and LinkedIn URLs.
Only three input rows appear initially, with Show all/Show fewer for longer lists. Login drafts
and history restore all entries. Editing any entry invalidates the quote, including late responses.

`POST /arena/plans` accepts either the original `identity` or an `identities` list, never both.
The server requires one input shape and a common provider cohort across the list. Every entry is
planned through the existing routing planner, with its own frozen adapter requests and estimates.
The encrypted payload carries `identities` plus the first-entry `identity` compatibility field;
each attempt includes an `entry_index`. Existing single-entry records default to index zero.
Raw response snapshots share a 2 MB per-run allowance, split equally across attempts and capped
at the existing 256 KB per response. A response exceeding its raw-storage share still undergoes
normal classification and field extraction; `raw_omitted` explains its absent source snapshot
in expanded details. This bounds repeated encrypted writes and polling payloads for large batches.
The quote aggregates estimates per provider and reports `entry_count`; history includes the count,
while full run reads/export include all identities and individual results. No schema change is needed.

Batch Battle calls each selected vendor for every entry with a shared four-leg concurrency limit.
Batch Waterfall walks each entry's ascending-price steps, completing the first step across the
list before advancing unresolved entries to their next step. It remains serial at dispatch;
each entry has independent hit/error-fallback state, while the run spending ceiling is shared.
Waterfall admission and the Run-from button use the sum of the cheapest first steps across all
entries. Battle admission uses the sum of all attempted estimates. There is no implicit partial
purchase when the batch is unaffordable. Runtime balance/policy refusals stop the waterfall and
leave untouched attempts available for the explicit, priced per-cell Try action. Manual plans
use the identity belonging to that cell, retaining all other results and feedback.

Batch runs have a deadline of 240 seconds per entry, capped at one hour; each leg retains its
90-second limit. One batch occupies one active-run slot. State persists after every attempt, so
refresh/history resumes observation without spending. Cancellation and process loss retain
completed work and never retry dispatched calls. Uncalled cells can be tried individually, including while another additional call is running; there is no automatic batch resume or automatic retry after an ambiguous outcome.

For multiple entries, fighters summarize each vendor. Vendor scoreboard rows with attempts expand in place into that vendor's
attempted entries (including misses/errors, excluding queued, skipped and uncalled entries).
The shared result table labels these rows by entry and keeps per-result details and feedback.
Only one vendor or entry expansion is open at a time, avoiding duplicate feedback forms and IDs.
An entry-by-vendor matrix replaces the single-entry table. Selecting a matrix row/result expands a nested vendor table immediately beneath
that entry; selecting it again collapses it. The shared `ArenaResultTable` component renders both
single-entry and nested details. Overview cells show only attempted results; queued, skipped and
uncalled cells remain empty. Thumbs, Try and optional report forms appear only inside the detailed
table, avoiding duplicate controls. The overview entry column stays pinned during horizontal scrolling.
Results grow to full height in the page, without an internal vertical scrollbar. The `stickyHeader`
directive keeps real table headers aligned at the viewport top during page scrolling. The expanded
entry row stays beneath the overview header until its detail section ends; the nested vendor header
sits below both, preserving the entry name while reviewing a long vendor list. The directive observes
table resizing, responds to entry changes, and removes its listeners on unmount.
The matrix keeps readable vendor column widths and scrolls horizontally for larger vendor sets.
The nested detail panel stays aligned to the visible scroll container with a container-relative
width and sticky left offset; its columns do not scroll with the overview. Nested details use fixed
column proportions with stacked vendor rows in narrow containers. Found uses a green check badge;
No match uses a red cross badge so outcomes differ by both color and symbol.
Filters show all, found, unresolved, or differing answers. Immediate per-result thumbs and
optional rejection details use the same durable rating/report endpoints. Raw hit status and found
percentages remain unchanged by ratings; **kept** counts hits without a thumbs-down rejection.
Batch thumbs up approve their cell; one upvote does not crown a vendor across the whole list.

Completed Battles with a complete vendor cohort award Coverage to the largest kept count,
Cheapest to total settled charges (including misses) divided by kept count, and Fastest to the
median recorded duration of kept results. Ties share awards; only Coverage receives crowns.
Vendors with zero kept results cannot win. Missing charges/timings withhold the respective metric;
uncalled/interrupted/cancelled outcomes withhold batch awards. Running batches show progress without
provisional awards. Waterfall displays found/attempted counts and contributions, with no comparative
crowns or belts because later vendors receive only the unresolved subset. Found is explicitly not
verified accuracy, and speed excludes queue time. Single-entry winner rules remain unchanged.

## Discovery queries

Discover contains Find people (`people.search`), People at a company (`people.company.search`,
an Arena alias of the catalog's `people.search` capability), and Find similar companies
(`companies.similar`). Find people accepts a search description or job title plus a recognized
ISO country code; company people search accepts a company domain with an optional job title.
Similar-company discovery accepts a seed domain. These are direct catalog queries, with no
agent harness, model-written plan, automatic pagination or implicit follow-up enrichment.

Discovery shares the existing quote, admission, cancellation, feedback and batch execution paths.
Each batch query returns its own list per vendor, with at most ten normalized matches displayed.
People search uses the contract's bounded default limit; CompanyEnrich similar search fixes
pageSize to ten, while Tomba's similar endpoint has a fixed-price response without a limit input.
Preview prices are computed from those exact requests. Both public previews and private plans
exclude adapters that drop requested identity constraints or contract filters, including the
result limit. Unknown country codes are rejected before planning rather than silently omitted.

Returned lists have bounded scalar names, roles, company, location, contact and company fields.
URLs pass the existing safe-field normalizer; raw responses retain the existing separate storage
budget. The displayed count describes the normalized list, not an upstream total across pages.
Expanded vendor rows display the individual matches; batch disagreements compare the returned
lists rather than counts alone. Batch coverage and cost-per-kept use queries as their denominator,
not the number or relevance of contacts. Waterfall still stops on the first structural hit, which
for discovery means a usable nonempty list, not a relevance judgment.

Discovery previews show vendor prices without historical enrichment hit-rate charts. Discovery
endpoints are excluded from the enrichment insights aggregation until an appropriate discovery
metric exists; this also preserves the existing enrichment metric snapshot and fingerprint.

## Execution and costs

`safe_output` normalizes the Arena fields before structural classification and again on presentation
for saved sessions. Vendor masking booleans cannot become text fields such as a name or location;
only explicit verification fields retain booleans. Numeric metrics and flexible employee/founding
values retain their types. Domains become hostnames, website values gain a web scheme when absent,
and LinkedIn handles/schemeless URLs become canonical person or company URLs. Malformed or non-web
addresses are unavailable. Original response snapshots, recorded charges, and historical outcomes
remain intact. Masked required fields in new responses cannot qualify as structural hits.

Person and email-to-LinkedIn results display an identity notice when the returned name/profile differs
from the supplied identity or vendors disagree within the same entry. The notice does not choose a
correct vendor, change votes, or rewrite the answer. A vendor HTTP 402 displays an exhausted-credit
or lookup-allowance message recommending another vendor; it is distinct from a team-balance refusal
and does not direct the user to top up team credits. `upstream_status` preserves this distinction in
both live reads and saved sessions.

`POST /arena/plans` validates input, applies the normal team's routing eligibility and freezes
the exact adapter requests, endpoint/adapter hashes, identity, ordering, estimates and budget in
an encrypted `ArenaRun`. A quote expires after five minutes. Creating it does not reserve credits.
Battle requires credit for the sum of estimates. Waterfall sorts selected vendors by their exact
quoted price, starts with credit for only the cheapest vendor (including an exact balance match),
and displays “Run from” that price. Own-key calls have a zero treg price. Each subsequent attempt
still passes the normal runtime balance check and the remaining run-budget check. These are admission limits at quoted prices, not a guarantee
against a higher provider-reported final charge. The UI discloses that difference.

`POST /arena/runs/{id}/start` rechecks catalog hashes and available credits. A conditional database
update claims the run once, including across web workers. Duplicate starts never dispatch again.
Execution is owned by an in-process task: compare has four concurrent legs per run; waterfall
executes serially. Each leg captures current membership/policy and calls the existing
`application.call.service.execute_call` against the direct endpoint. The ordinary call runtime
owns pricing, credential priority, authorization, reserves, settlement and cancellation cleanup.
Arena never writes balances or holds. Own keys remain unmetered by treg. Aggregator overflow is
disabled for these comparisons, and archive lookup is bypassed so runs measure fresh calls.

Database sessions are short and closed before upstream requests. Attempt state and call references
persist before dispatch. Each leg has a 90-second deadline and the run has a 240-second deadline.
Cancellation is polled between writes and interrupts in-flight tasks through the normal call
cleanup. Shutdown drains Arena owners before closing the shared HTTP client. A process-lost run
becomes interrupted after its persisted deadline; it is never automatically retried. Unknown
charges remain unknown until a durable ledger settlement/release can resolve them on a later read.
Actual run starts are limited to 100 per user/hour, with an admission check for three active runs.
Automatic price previews do not count toward that limit. Quote creation retires the oldest unused
quotes to retain at most 100 per user, preserving completed/running sessions. Start admission
serializes on the user row across drafts and teams; hitting the execution limit still allows pricing.

Waterfall uses ascending quoted prices, retaining planner order for ties, and the bounded error fallback policy. It stops
at the first structural hit: the adapter supplies the contract's required fields. Found work email
does not mean verified deliverability; phone found does not mean a live line. A negative mailbox
verification verdict is a successful answer. Each step shows queued/running, found/no match,
error/timeout, skipped/not attempted, timing, charge, and the reason for stopping or skipping.

## Additional vendor calls and issue reports

Each uncalled (`not_attempted` or `skipped`) result has a priced Try action. Completed attempts
never expose a rerun action, including errors, timeouts and interrupted manual attempts.
`POST /arena/runs/{id}/attempts/{attempt_id}/plan` rechecks current routing access for that exact
endpoint and computes the real adapter-request estimate. It stores a five-minute quote in the
run's encrypted payload without reserving credits or calling a vendor. The UI dispatches only
at or below the displayed estimate; an increase requires another click at the new price.
Known insufficient credit changes the action to Top up without dispatching. A fresh quote,
start admission and ordinary runtime reservation independently check paid-call funds; zero-cost
and own-key calls remain available even with a nonpositive balance. Insufficient balance uses
the existing team top-up flow.

The matching `/start` locks the owned run, checks the quote, catalog hashes, credit admission and
active-run cap, then claims the uncalled attempt once. It reopens a completed session or adds
an independent worker to a running session using the ordinary call runtime. Up to four manual
attempts may run at once. Only the clicked Try action is pending during its plan/start requests;
other uncalled vendors remain available. Queued automatic steps and dispatched calls cannot
be claimed again. Cancellation blocks new claims until the run has stopped. This explicit extra call is admitted separately from the
original waterfall budget. Original identity, earlier results, reports, and call references stay
intact. The result carries `manual: true`; timing continues after the previous attempts. Duplicate
starts, expired quotes and already-attempted vendors cannot dispatch. Every worker stays registered for shutdown. Persistence locks the run and merges only the
worker’s own attempts, preserving concurrent quotes, results and feedback; a run becomes terminal
only after all active attempts finish. Manual cancellation and process loss use the same
settlement and interruption handling as initial runs.

Thumbs up and thumbs down save immediately through
`POST /arena/runs/{id}/attempts/{attempt_id}/rating`, for returned results in either mode.
The selected thumb updates optimistically; failed saves restore the prior selection and show an error.
Thumbs up has no follow-up form. Thumbs down opens optional reason and note fields after beginning
the independent rating save; closing them keeps the rating. Either reason or note may be supplied,
and a later click on thumbs down reopens the details. The first submitted details are idempotent
through `/report`; they never overwrite an upvote from another tab. Historical reports remain intact.

Ratings store the current up/down value, creation/update timestamps and attributed context in the
attempt's encrypted payload. Repeating the same rating is idempotent; choosing the other thumb
updates it. Ratings and optional reports are private to the session owner/team, survive additional
calls, and travel with history/export under the same 30-day retention. Terminal payload reads,
ratings, reports and manual claims serialize on the run row so writes cannot erase one another.
Feedback never changes the vendor's output, hit classification or charges.

## Visible vendors and durable feedback

`GET /arena/runs/{id}` returns private, attributed snapshots, including existing runs. The UI polls
every 1.5 seconds while running and shows each vendor's queued, running or completed result as it
arrives. Comparison rows retain their frozen randomized display order; waterfall steps retain
execution order. Provider, endpoint, raw response, cost and timing are visible without voting.
Viewing a result never creates an evaluation or makes a paid call.

The page collects per-result thumbs in both modes, including individual batch cells. The previous
`POST /arena/runs/{id}/evaluations` API remains available for compatibility, including winner,
tie/none/cannot-judge/skip kinds and `POST .../reveal`. Its first evaluation remains immutable under
a run-row lock and unique run-id constraint. New thumbs do not create or rewrite those historical
comparison votes.
Version 2 evaluations carry `feedback_context: attributed`, the frozen cohort fingerprint,
exposed candidate ids/order, outcome and adapter versions. This distinguishes vendor-visible
preferences from historical blind votes. Existing evaluation rows remain immutable. The legacy
`revealed_at` column records the evaluation event; it no longer controls result visibility or
eligibility to submit the first vote. No schema change is required for this presentation change.

History appears as a left-hand session list anchored near the viewport edge, loaded for the
active team and hidden when empty. The Arena column is centered independently in the remaining
space, with a capped width on larger screens. The history list remains scrollable with its native
scrollbar hidden, including the horizontal list on mobile.
The list starts with 30 runs. Load more appends older runs in groups of 30 and disappears at the
end of the retained history. `GET /arena/runs` keeps its array response and default limit of 30;
optional `limit` (1–100) and `before` (an owned, retained run ID) paginate by descending creation
time and ID. The UI requests one extra row to detect the next page. New runs cannot shift older
page boundaries, repeated clicks share the in-flight request, and stale team/refresh responses
cannot append to the current list. Reading additional history makes no vendor calls.
Selecting a session restores its inputs, mode and results with a read; New query clears the
current inputs/results while keeping saved sessions. The list refreshes after dispatch and
completion, ignores responses from a previously selected team, and clears on logout/team change.
Session switching is disabled during an active run. On narrow screens it becomes a horizontally
scrollable strip above the composer. There is no history modal. History/export do not invoke services. The public leaderboard displays aggregate call stats, not feedback rankings. There is no
feedback-reporting dashboard; durable evaluations are the source for later feedback analysis.

## Privacy and operation

Runs and evaluations belong to both their creator and active team. Another team member cannot
read the creator's runs. Inputs, response snapshots and comments are encrypted with the existing
Fernet key. Results are bounded to 256 KB per provider. Access expires after 30 days; a bounded
lazy sweep deletes expired runs and evaluations on history/quote requests. Team deletion removes
evaluations before runs. Normal ledger retention remains unchanged.

The page opts out of PostHog autocapture and session recording through `sitetrack.js`'s
`data-private-page` flag. Inputs stay out of URLs. Mutating endpoints use the existing same-origin
guard. All Arena routes belong to the control role (and default all role), including interactive
paid execution; the dataplane's `/call/` contract is unchanged.

Alembic revision `0027` creates `arenarun` and `arenaevaluation`. Run `python -m treg upgrade`
before serving the new release. Tests cover auth/private access, aggregate admission, direct billing,
own keys, cancellation, duplicate start/vote, attributed progress/results and pre-charge name validation, waterfall progression and OAuth return.

Frontend billing-flow checks: `node --test tests/js/enrich-arena.test.cjs` exercises inline pricing,
price invalidation, login gating, duplicate clicks, quote expiry and the correct-team top-up link.

### Conversion tracking

`TregTracking` in `sitetrack.js` connects anonymous pageviews to the authenticated email and
active `team` group after `loadIdentity`, including OAuth returns and team creation. Team switches
clear the previous group; account changes clear the previous identified analytics session. The
page still disables autocapture and recording. Explicit events contain product metadata only:

- `arena_page_viewed` (immediately at mount, before data requests, once per page, `surface=arena|leaderboard|benchmark`) and
  `arena_signup_opened` are browser events. `entry_surface` is the first observed product surface
  from the 90-day first-party cookie, distinct from UTMs/referrer; it is not historical attribution
  for visits before this tracking shipped.
- `signup_completed` is emitted by email OTP and GitHub/Google provisioning **after commit and
  only for a new User**. Returning sign-ins and failed proofs do not count. `signup_method` and
  the allowlisted entry surface are included. The event uses the same email identity as calls.
- `arena_run_started` is emitted after a successful database claim, not for quotes or duplicate
  start requests. `arena_run_completed` is emitted after saving the initial run's terminal state.
  Both include run ID, capability, mode, entry count and team; completion includes `successful_call`
  (at least one hit or evaluated miss) and `returned_data` (at least one hit). Manual Try and
  verification continue to emit ordinary `tool_called` events with `client=enrich-arena` without
  creating another logical-run event. Completion after a process crash is not reconstructed.
- `arena_topup_clicked` records the link click. The app carries `checkout_source=arena` to both
  top-up entry points even if SPA navigation removes the query. `topup_started` and the credited
  `topup_completed` include both first entry and checkout source. Stripe Session and PaymentIntent
  metadata carry both so either webhook order works; the top-up ledger metadata persists them.

For an Arena visitor funnel, count unique people with `arena_page_viewed` **filtered to
`surface=arena`**, then `signup_completed`, then `tool_called` filtered to `client=enrich-arena`
(and `outcome=ok` for successful calls). Use an ordered conversion window, e.g. 30 days; report
returning-user activation separately from new-signup activation. Do not count child calls as users
or batches as multiple runs. To include a teammate paying, join an activated person's run/call
`team` to subsequent `topup_completed` events and deduplicate people/teams at the intended grain;
a person-only payment funnel misses those conversions. Count the first paid top-up per team for
new-payer conversion, not repeat purchases or checkout redirects. Acquisition and checkout-source
breakdowns answer different questions and should stay separate.

Events remain best-effort PostHog analytics (disabled without a configured key); the existing
ledger is the durable payment source. No schema migration or historical backfill is part of this
tracking change. Live dashboard configuration and production event delivery must be checked after
deployment. Tests exercise fresh-vs-returning auth, batch deduplication, both webhook orders,
identity/group switching and exclusion of enrichment inputs from event properties.

### Optional verification after contact lookup

Find work email and Find phone number enable verification by default for new queries. The switch choice is persisted in the draft and bound into the server quote.
Restoring an explicit draft opt-out or historical run preserves its recorded setting. New email checks,
automatic and table-triggered, call `treg.people.email.verify` through the ordinary routed runtime.
The quoted ceiling sums accessible verifier estimates (own keys cost zero), with candidates preferred
in price order. The frozen provider set, catalog hashes and route max-cost header bound execution;
newly added providers cannot silently enter an existing quote. The router falls back on unavailable
providers and missing verdicts, subject to its existing error-fallback limit, and stops on completed
valid, invalid or risky verdicts. Every child uses normal credit reservation and settlement.
Phone checks remain a direct Tomba call because there is only one integrated phone verifier.
Battle quotes include the verification ceiling for every vendor/entry; lookup waterfall quotes
include one ceiling per entry. Lookup misses incur no verification call. The original found contact
is retained alongside the actual serving verifier, tried-provider trace and total verification charge.
Saved quotes retain their original policy; obtain a fresh quote to use the email waterfall.
Invalid and catch-all verdicts are completed checks, not failed lookups. Waterfall still stops
at the first structural lookup hit; verification does not silently start another lookup.

An unchecked found result has a priced Verify email/phone action in its table row. Verification
plan/start routes are owned, same-origin, five-minute quotes; they read the contact from the
encrypted stored result. They check current access, catalog hashes and team credit before
claiming a nested attempt exactly once. Per-row UI state allows independent checks without
blocking other providers. Automatic checks, explicit checks and extra Try lookups use the normal
reserve/settle/release call runtime. Try admission and its displayed total include automatic
verification when enabled. Nested charges are included in row/run totals; cancellation, deadline
and process-loss recovery preserve the lookup and never retry paid work automatically.

Verify phone number is also a standalone batch-capable task using Tomba's existing phone-validator
endpoint. Input requires an international number with + and country code; spacing and punctuation
are normalized. It returns numbering-plan validity, country, line type and carrier when supplied.
It does **not** establish that the line is live or belongs to the intended person. Arena can plan
a single-provider task through the existing candidate planner; public synthetic routes still
require two verified adapters. No public single-provider routed endpoint is added.

An explicit invalid/undeliverable email verdict automatically creates an `incorrect_data` report
on the original lookup, for both automatic and table-triggered verification. This happens in
the locked backend result merge, so it works without an open browser and is idempotent across
worker saves. Reports record `automated_verification` provenance and the verification attempt ID;
the UI labels them “Invalid email · automatically reported”. Existing reports and human ratings
are preserved. Catch-all, unknown, risky, failed/cancelled checks and phone verdicts never trigger
this email issue rule. The original contact, lookup outcome and billing remain unchanged.

Verification cost annotations sit below the total in the Cost column, with a wrapping label and
a separate amount line. Cost/Time column sizing keeps the annotation inside its own cell,
including nested result tables and the existing mobile card layout.

Arena migrations are ordered after main’s 0026 (call reviews): 0027 (runs and evaluations), 0028 (rolling insights), and 0029
(published verification aggregates). Public snapshot reads use the API pool; the incremental worker
is listed explicitly in the background pool budget. Email verification adapters join the existing
task through catalog-driven discovery.

Email-verification result rows use `emailVerdict` and `outcomeClass` to show “Verdict: Valid”
(green), “Verdict: Invalid” (red), or “Verdict: Risky” (amber), instead of a green returned-answer
badge. Catch-all/accept-all and explicit risky statuses take precedence over the adapter's
`valid` boolean. Unknown, unverified and unrecognized statuses remain neutral “Unknown”; they
are not displayed as invalid simply because the adapter projects `valid: false`. Original
provider status is retained in the result and expanded data. Nested email verification uses
the same verdict colors and keeps the original status in its tooltip. This display grouping
does not change call outcomes, billing, waterfall behavior or automated issue-report rules.

The optional verification control is an inline “Email verification” / “Phone verification”
switch beside the input selector, with a green enabled track and no surrounding pill. The
footer wraps its controls on narrow screens. Extra per-found-result pricing lives
in a hover/focus tooltip, using the current quote when available and the catalog minimum otherwise.
The tooltip explains that verification is included in the run estimate; Escape dismisses it.
The native checkbox retains switch semantics, keyboard operation and the existing disabled rules.

### People Search Bench

`/enrich-arena/people-search-bench` is the third top navigation destination, labelled **Benchmark**. It shares the Arena
shell and account/setup controls. Its read-only startup loads the existing same-origin
`/people-search` landing page once and uses `ArenaBench.parseDocument` to extract its published
category scores. Scores are not copied into a second dataset. Incomplete categories, unknown
systems or nonnumeric/out-of-range scores show a retry/source-link error instead of partial charts.
It never restores query drafts, resumes runs, requests quotes or polls live call insights.

A selected-category horizontal chart sorts complete search systems by published score. Four
small charts compare Recruiting, B2B prospecting, Deterministic and Influencer on a common 0–100
scale, with system logos and text values. Source links attribute the landing snapshot and the
original LessieAI benchmark. The original repository's current figures differ from the landing;
the page discloses that difference and does not manufacture an overall score or describe these
numbers as individual vendor hit rates or email-verification accuracy. The new charts update when
the existing landing source changes. No paid benchmark execution is triggered by viewing them.

`tests/js/arena-template.test.cjs` compiles the shared page and component templates using the
bundled Vue runtime. This catches malformed template expressions that method-only tests miss,
including the nested footer interpolation that previously prevented all Arena views from mounting.

## Published verification pilot

`application.arena_verification_insights` validates and publishes aggregate-only pilot data in
`ArenaVerificationSnapshot` (revision `0029`). `GET /arena/insights` attaches the latest publication
as `verification` alongside rolling call stats. Both reads are bounded snapshot queries; rendering
the table or Leaderboard never scans evidence or calls a verifier. The rolling worker cannot overwrite
a publication. Reimporting an identical run is a no-op; different contents under the same run ID fail.

The Arena table adds **Email validity rate** for email lookup. The Leaderboard
uses the same label. Rows match task, endpoint and input type. Validity is the share of sampled
distinct returned emails labelled valid by both verifiers, divided by completed checks. It does not
multiply by lookup hit rate or require a historical baseline. This is observed verifier agreement
within the sample, not guaranteed delivery or ownership. The entire column header opens an explicit tooltip explaining the sampled denominator on hover;
its text button also supports focus, tap and Escape. The tooltip is rendered outside the scrolling
table to avoid clipping. No sample-status sublabels are shown.
Row hover details identify the verifier providers and check date; the chart source shows the sample period. Fewer than 20 completed checks
are withheld; small-sample labels are hidden. Risky, unknown and conflicting verdicts stay
in the denominator but do not count as valid; unfinished checks are excluded. A genuine zero is
displayed and missing values have no chart bars. Phone format checks never produce a verified rate:
reachability and ownership remain unverified. The publication preserves the legacy `rate` projection
for older consumers and adds explicit `checked_n` and `validity_rate` fields; the UI only consumes
`validity_rate`, so older snapshots cannot accidentally display the lookup projection as validity.

Publication data stays outside the checkout. To populate a migrated deployment, pass the private
aggregate JSON to `scripts/import_arena_verification.py` with its configured `TREG_DATABASE_URL`;
`--check` validates without writing. The importer accepts only schema-whitelisted aggregates,
source dates, endpoint/input identifiers, verifier names and counts; raw contact fields are rejected.
It does not import private evidence or run paid verification. The same aggregate file can seed local
and production databases after their migrations. Publishing a new run updates the public snapshot
on subsequent reads; the normal frontend refresh picks it up.

Missing or insufficient historical samples display a dash in the vendor table without a
“No sample” or “Insufficient sample” label; small-sample labels are also hidden.

## Request a vendor

Both draft and result fighter lineups end with a small, muted **+** button labelled Request a vendor.
`ArenaFighters` emits `request-vendor`; the Arena opens a native dialog using the dashboard's
existing Request a tool form fields and submission flow: what's missing, optional details,
and optional contact when signed out. `submitVendorRequest` posts to the existing open,
rate-limited `/tool-requests` endpoint with `source: web` and a public task identifier as query
context. It never copies enrichment inputs, requires credits or starts a run. Pending submissions
disable repeat sends; errors preserve the draft for retry and success shows a compact Request sent
confirmation. Before and after submission, a vendor self-serve section offers the published
`https://treg.to/vendor-listing.md` agent prompt and a copy button, inviting API owners to open a PR.
Clipboard failures keep the prompt selectable and show an explicit fallback message. Native dialog focus handling supports Escape and returns focus to the trigger on close.

## Task navigation and saved-result URLs

Task and input-type changes push the selected capability/variant into the current page URL,
clear the previous result and save the draft. Switching between Arena and Leaderboard carries
the current selection. Browser Back/Forward reloads that URL, so its state is reapplied consistently.
Bare Arena visits can restore a recent draft, but never silently reopen a session from the legacy
active-run cache. Explicit run links also bypass pending drafts and never dispatch lookups.

Selecting history or starting a run sets `/enrich-arena?run=<id>&team=<slug>`. That URL loads
the saved result through the existing authenticated, owner-scoped `GET /arena/runs/{id}`.
The selected team must be in the signed-in user's memberships. Signed-out visitors are prompted
to sign in; email and social sign-in preserve the run destination. Missing, expired or inaccessible
runs show an error instead of falling back to another result. These are private bookmarks with
the existing retention limits, not public share links. New queries remove the run parameter.
