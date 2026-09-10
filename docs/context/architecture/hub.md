---
title: The tool hub — tools a maker publishes, made of other tools
status: in-progress (phase 7 of 8: the surfaces; behind TREG_HUB_ENABLED, off)
sources:
  - src/treg/domain/hub/__init__.py
  - src/treg/domain/hub/manifest.py
  - src/treg/domain/hub/refs.py
  - src/treg/domain/hub/graph.py
  - src/treg/application/hub/__init__.py
  - src/treg/application/hub/runner.py
  - src/treg/application/hub/sandbox.py
  - src/treg/application/hub/limits.py
  - src/treg/domain/money/__init__.py
  - src/treg/routers/catalog.py
  - src/treg/application/hub/health.py
  - src/treg/worker.py
  - src/treg/routers/web.py
  - src/treg/web/skill.md
  - src/treg/web/llms.txt
  - src/treg/alembic/versions/0028_hubtool_check_result.py
  - src/treg/mcp.py
  - src/treg/cli.py
  - src/treg/hub_sandbox.py
  - src/treg/routers/hub.py
  - src/treg/alembic/versions/0026_hub_tools.py
  - src/treg/alembic/versions/0027_hub_runs.py
  - tests/test_hub.py
  - tests/callmatrix/test_hub_run.py
  - tests/test_hub_sandbox.py
related:
  - architecture/proxy-model.md
  - architecture/money.md
  - architecture/catalog.md
---

# The tool hub

A **hub tool** is a tool a maker publishes on treg, made of other tools: either a JSON list of
steps, or a script that runs in a sandbox. Every step is an ordinary treg call, so identity, the
access rules, key injection and money already exist once per call. The decisions are in
`docs/HUB-DECISIONS.md` (50 questions, settled 2026-09-09); the plan is eight phases, one pull
request each into `dev/hub`, behind `hub_enabled` (`TREG_HUB_ENABLED`, default off) so no merge
along the way changes what users see.

**Build state: phase 4 of 8.** Both roads run; a maker (or their agent over MCP, or the CLI)
publishes a tool, the check runs once for real, and a passing version is live at once. The
seller's money and every surface are later phases, and no agent-facing file mentions the hub
(CLAUDE.md: do not document what is not built) — the MCP verbs exist on the server but the flag
keeps every hub route 404 until launch.

## The manifest (`domain/hub/manifest.py`)

Pure rules over one `recipe.json`; every refusal is a `ManifestError(field, rule)` — a dotted path
into the file and the rule it broke — because the maker is usually an agent that must fix the
file without a person reading a trace. Fields: `name`, `summary` (≤200), `writes`, `inputs`,
`uses`, `limits`, `price_usd`, exactly one of `steps` | `script: "run.js"`, `output`.

Rules carried from Crawl4AI's recipes (the same idea, live for months): an input is required by
having NO `default` (a `required` key is refused); every non-secret input carries an `example` or
a `default`; an `int` input names a `max`; a secret input is a string with no default. `uses` is
the security boundary: each entry must be a catalog id or one of the maker's own tool names, and
a hub id is refused there (depth one — a hub tool may not use a hub tool). Every step's `call`
must be in `uses`. Limits: `steps` ≤ 20, `wall_s` ≤ 120, `price_usd` 0–100 with at most six
decimals (stored as `price_micro`). `validate_check` checks `check.json` (sample inputs, required
fields, `min_rows`) against the manifest, so a check that can never pass is refused before anyone
pays for it.

## The table (`HubTool`, migration 0026)

One row per (tool_id, version), `tool_id = <team slug>.<name>`; status `unchecked` (phase 1
writes only this) | `live` | `failed` | `retired`. Stores the four files a maker ships: manifest
(normalized, with the assigned version), script, check, readme, plus `kind`, `summary`, `writes`,
`price_micro`, `created_by`. `HubTool` is in `ORG_SCOPED_MODELS`, so a team holding hub tools can
still be deleted.

## The maker's road (`routers/hub.py`, `application/hub`)

Flag off ⇒ every route 404. `POST /hub/tools` (member+) takes the four files as fields —
`manifest`, `script`, `check`, `readme` — validates them against the team's world (catalog ids,
the team's own tools, existing hub ids; 422 `{error: manifest_invalid, field, rule}`), stores
the next version as `checking`, then **runs `check.json` once for real**: an in-process request to
`POST /call/<id>@<version>` carrying the maker's own identity headers, so the steps are charged to
the MAKER's balance at the normal prices (seller price not charged) through the real call road.
Pass (every `check.fields` present and non-empty, `min_rows` met) ⇒ `live`, and the 201 carries
`call: "POST /call/<id>"`; fail ⇒ the version is kept as `failed` with the reason in
`check_result` (migration 0028) — a 402 there says the maker could not afford the run. `PUT
/hub/tools/{id}` publishes a new version of a tool the team owns (same body; the name must match
the id). `POST /hub/run` is the dry run behind `treg hub run .`: the four files plus `inputs`,
run for real as the maker, nothing stored, version 0 on every trace. `GET /hub/tools/mine`,
`GET /hub/tools/{id}[@N]` read back (the maker also gets script, check, readme and the verdict;
another team sees only live versions).

Versions: the newest `live` serves `/call/<id>`; `<id>@N` pins one, and a pinned old version stays
callable for 30 days after a newer live one exists (`application/hub.tool_for`). Four runs at a
time per team (`application/hub/limits.py`, in-process, exact for the one-process production
deploy): the fifth answers 429 `hub_busy` with `retry_after_s`.

Over MCP (`/mcp/`): `hub_create` (the four files; returns tool_id, version, status, the call line,
the check verdict, or the manifest's field and rule), `hub_update` (a new version), `hub_mine`.
Calling stays the existing `call`. The CLI mirrors it: `treg hub init <name> [--script]` writes the
four files, `treg hub run <dir> --input k=v` dry-runs the folder, `treg hub publish <dir>`
publishes, `treg hub ls` lists. `hub_create`'s description carries the owner's rule: a credential
the team does not hold is never hard-coded into a script — register it first, then name the tool.

## The call road

Resolution order for `/call/<rest>` is unchanged for everything that exists today and gains a
third, last step: an own tool wins, then a catalog id, then — only when both missed with a 404
and the flag is on — `application/hub.tool_for` looks up the newest `live` version (or `@N`).
Phase 1 raises `ResolutionFailed("hub_not_runnable", 501)` with the tool id; the runner replaces
that in phase 2. An unchecked version is never on the call road.

## The reference language (`domain/hub/refs.py`)

Complete and deliberately small: `$input.<field>`, `$<step>.<path>` (any depth, `[0]` for one
item), `$<step>[]` (every answer of a repeated step), `$<step>.length`, `$0.<path>` (the position
alias), `$<as>.<field>` inside a repeat. A string that is exactly one reference resolves to the
value itself; a string with references among text resolves to text. A missing field reads as
`None` (a missing answer is data); an unknown root is refused at publish. No arithmetic, no
condition, no function: a recipe that needs those is a script.

## The graph (`domain/hub/graph.py`)

Edges come from the references, never from a declaration: a step that reads `$company.x` waits
for `company`. Built at publish (so a cycle or an unknown step is refused before anyone pays) and
again at run. `wave` is a step's depth, kept on the trace; the runner schedules dynamically.

## The runner, JSON road (`application/hub/runner.py`)

`POST /call/<team>.<name>` with the inputs as the JSON body (or as query params on GET). The
runner coerces the inputs once (defaults, types, ints clamped to `min..max`, unknown or missing
required inputs → 422 `hub_input_invalid` naming the field), reads the ceiling from
`X-Treg-Run-Max-Cost` (default $1.00), builds the graph, then starts every ready step, four at a
time, waiting on whichever finishes first. Each step is one `execute_call` under the child hold
`{run}:s{n}` (`{run}:s{n}.{i}` for an item of a repeat): the same gates, the same key injection,
the same reserve-and-settle as a direct call. **Two teams meet in one run:** a catalog step runs
as the caller (their money, their rules, their key if they hold one, else treg's); a step on one
of the maker's own tools (`call: "<tool>/<path>"`, optional `method`) runs as the maker — their
registered key, unmetered — which is how a shared recipe uses a key the caller never holds.

`for_each` fans a step into N units, one per item, all counted against `limits.steps`;
`skip_if_empty` marks a unit `skipped` without a call; `allow_fail: true` lets a failed step
read as `None` instead of stopping the run. The ceiling check counts what is already in flight.
When a step fails (a non-2xx or a refusal), nothing new starts, in-flight steps finish, and the
run answers **424 `hub_run_failed`** with `{error: hub_step_failed, step, status, trace,
charged_micro}`; a global refusal (balance, caps) keeps its own kind and status; passing the
ceiling is 402 `hub_run_max_cost`, the step cap 424 `hub_step_cap`. Money already spent on
completed steps stays spent (the ledger has no undo); a failed step's own hold releases by the
endpoint's normal billing rule.

The reply: `{run_id, recipe: "<id>@<v>", output, usage: {cost_micro, steps, ms}, trace, log}`
with `X-Treg-Run-Id` (= the parent call id), `X-Treg-Steps`, `X-Treg-Cost-Micro`. The trace has
one entry per unit: `wave, name, call, outcome (ok|failed|failed_allowed|skipped), status, ms,
cost_micro, key (treg|team), item`. `Idempotency-Key` covers the whole run through the parent's
store, exactly like a routed endpoint. `HubRun` (migration 0027) keeps one row per run — status,
steps, cost, duration, masked inputs, trace, error — for 30 days; in the org cascade by
`caller_org_id`.

## The script road: the sandbox (`application/hub/sandbox.py`, `treg/hub_sandbox.py`)

A script recipe's `run.js` runs in a separate short-lived process per run: `python -m
treg.hub_sandbox`, spawned with `runner.py`'s discipline — a scrubbed environment (never the
server's), a private temporary HOME, its own process group, POSIX rlimits (CPU, file size, no
core, RLIMIT_AS on Linux), a wall-clock kill, the whole group killed on every exit. Inside, QuickJS
(the `quickjs` package, server extra; Python 3.12 wheels in production, sdist locally) has no
network, no file system, no `require`, no `process`, no timers: `typeof fetch` is `undefined`.
The engine's own heap is capped at 64 MB and its clock at the manifest's `wall_s`; a memory bomb
ends as `memory`, an endless loop as `timeout`, a thrown error as `script`, each one line the
maker reads in the run log.

The whole surface a script gets: `ctx.inputs` (coerced by the same rules as the JSON road),
`ctx.call(target, {method, query, body})` → `{status, headers, json, text}`, and `ctx.log(text)`
(50 lines × 2 KB). `ctx.call` crosses to the parent as one JSON line over stdin/stdout and is
run through the same child call the JSON road makes: `uses` enforced per call (a call outside the
list, a URL, or an unknown catalog id is refused and the run stops), the 20-call cap, the run
ceiling, `{run}:s{n}` holds, a catalog call as the caller, an own-tool call as the maker. The
returned object must be JSON under 2 MB and carry every field in `output.fields`, else 424
`hub_output_invalid`. A failed run is 424 `hub_run_failed` with `{error: hub_script_failed,
kind, message, trace, log, charged_micro}`; money spent on completed calls stays spent.

Known limit of version one: `ctx.call` is synchronous underneath the engine, so two calls inside
one `Promise.all` run one after the other; real parallelism is the JSON road's.

## The case study (phase 5, 2026-09-09) and what it taught

The owner and the assistant walked the maker's road by hand against a real Supabase table
(2,592 SEO rank-tracking rows in schema `hubdemo` of the owner's `krew-saas` project), on a
local server with the flag on: `treg hub init` (neutral skeleton) → the four files written
together → a run with no tool registered (refused: `uses[0]`, the rule, the two fix commands) →
`treg secret add` + `treg tool add supabase` with two bindings (`Authorization: Bearer` and
`apikey`) → `treg hub run .` → `treg hub publish .` (check passed, 0 µ$: the only step ran on the
maker's own key) → a second team called `unclecode-superdesign-dev.keyword-rankings` and got rows,
the maker's key served the step, and neither the Supabase URL nor any key material appeared in
the reply or headers.

Two runner rules came out of it:

- **A step is never answered compressed.** The runner reads every step's bytes itself (a script
  gets `json` and `text`), so the caller's `Accept-Encoding` is dropped from every child call and
  `identity` is asked instead. Found live: a 20-row Supabase answer came back gzip (Cloudflare
  compresses bigger bodies) and the script saw an empty list, while a 2-row answer was fine.
- **A script may set headers on `ctx.call`** (`Accept-Profile` for a PostgREST schema, a vendor's
  `Accept`), minus the ones that carry identity or framing: `authorization`, `cookie`, `apikey`,
  `host`, `content-length`, `x-treg-*` are treg's and are dropped; the tool's binding always wins.

Also from the walk: `init` writes a vendor-neutral skeleton (`my-api`, a `query` input, comments
that say what to replace); the hub commands print in the house style (numbered section bars,
ticks, a trace table, refusals as a "✗ Refused" block with field, rule and the fix command);
`treg hub init --from <template>` is backlog (templates for data providers); `treg hub retire`
is phase 7.

## The seller's money (phase 6, docs/HUB-DECISIONS.md round 3)

A maker writes `price_usd` in the manifest (stored as `price_micro`). A caller's run then pays
two things: every metered step as before, plus the price. The price rides the same money
primitives as a step: one extra hold `{run}:price` opened on the CALLER at run start
(`reserve_in_transaction`; 402 `hub_price_unaffordable` with the amount when the balance is not
there, before any step runs), settled on success, released on any failure or stop. The settle is
the one cross-team money movement in treg: `money.settle_to_in_transaction(db, call_id,
payee_org_id)` closes the caller's hold at its full amount, consumes the caller's blocks, and in
the same transaction credits the maker's team with an `earned` block of the same amount, writing
a `settle` entry on the payer (meta names the payee) and a `grant` entry on the payee (block kind
`earned`, meta names the payer's run). The invariant holds on both teams at every instant. No
margin: the seller's price is the seller's, whole (no platform share in the MVP).

Not charged when the caller IS the maker: their own runs, and the publish check run, cost the
maker only the steps. The ceiling (`X-Treg-Run-Max-Cost`) covers price plus steps: a price alone
above it is 402 `hub_run_max_cost` before any step, with the hold released. The reply's `usage`
carries `cost_micro` (the total), `steps_micro`, `price_micro`; `X-Treg-Cost-Micro` is the
total; `HubRun.price_micro` is what the maker earned on that run.

`earned` sits in the spend order between the free kinds and purchased money
(`_KIND_ORDER`: promotional/referral/bonus 0, earned 1, purchased 2), so a maker's own dollars
are used last. Withdrawal is backlog.

The seller's view: `GET /hub/tools/{id}/earnings?days=90[&format=csv]` and `treg hub earnings
<id> [--days N] [--csv]`: per day, runs, successes, failures and what was earned, for a tool the
team owns. Sales only (the maker's own runs are excluded), counts and amounts only, never who
called.

## The front door for agents (phase 7.1)

`skill.md` and `llms.txt` carry a hub section (the four files, `ctx.call`, the check, publish,
price, the share page, and the owner's rule: never paste a credential into a script, register it
first) inside `<!--hub-->…<!--/hub-->` blocks. `routers/web.py` strips those blocks when
`hub_enabled` is off, exactly as it strips the routed-discovery blocks, so a deployment never
documents what it has not switched on. The generated plugin SKILL.md files are regenerated only
at the final merge, when the flag flips.

`GET /catalog/endpoints/<id>` (behind `catalog_get` and `treg catalog get`) answers for a hub id
too: `endpoint.kind == "hub"`, with summary, inputs, output, the price line, health, version,
`call_template`, the page URL and the readme; never the script, the maker's tools or a key.
Search never returns a hub tool (unlisted by design); an unknown hub-shaped id stays a 404 with
the usual near-miss hints.

## The public share page (phase 7.2)

`GET /hub/<id>` (and `.md` for agents; `<id>@N` for a pinned older version) in `routers/web.py`
renders one live hub tool through the shared `_page` shell: the id line, summary, the price line
("seller $0.01 + steps", per 1,000), health from the last check, version, the exact call line
(CLI and curl), inputs, output fields, the README (a small escaping renderer `_md_lite`:
headings, lists, code, bold; no raw HTML survives), the publish check's trace, "made of N tools
of the maker's own (names and keys hidden)", reliability over 30 days (runs by others, success
share, median), and the older versions still callable. Readable without sign-in; `noindex` and
absent from the sitemap, because a hub tool is shared by its id, not found by search. The
publish reply (`POST /hub/tools`) and `treg hub publish` name the page.

## Health, the scheduled check, retire, price (phase 7.3)

Health is derived, never stored (`application/hub/health.py`): a version is `failing` when its
last three runs, callers' runs and scheduled checks alike, all failed; `ok` otherwise; `unknown`
before any run. A failing tool stays callable; the public page, `catalog_get` and the maker's
`GET /hub/tools/{id}/health` say the state, and the next passing run clears it. `treg-worker hub
check` (cron it every 6 hours) runs every live tool's newest `check.json` once as its maker: the
identity is rebuilt from the database (the publisher's membership, else an owner's), the run goes
through the runner as a normal run charged to the maker at step prices and never the seller
price, the row is a `HubRun` with `caller_email = "hub-check"`, and the verdict lands on the
version's `check_result` with `scheduled: true`. A failing check never retires a tool by itself.

`DELETE /hub/tools/{id}` (`treg hub retire <id>`) retires every version: off the call road at
once, rows kept so earnings and history stay readable. `PATCH /hub/tools/{id}` `{price_usd}`
(`treg hub price <id> <usd>`) changes the newest live version's price for later runs, no version
bump; every trace stamps the price it paid.
