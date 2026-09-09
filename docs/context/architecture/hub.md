---
title: The tool hub — tools a maker publishes, made of other tools
status: in-progress (phase 2 of 8: the JSON road runs; behind TREG_HUB_ENABLED, off)
sources:
  - src/treg/domain/hub/__init__.py
  - src/treg/domain/hub/manifest.py
  - src/treg/domain/hub/refs.py
  - src/treg/domain/hub/graph.py
  - src/treg/application/hub/__init__.py
  - src/treg/application/hub/runner.py
  - src/treg/routers/hub.py
  - src/treg/alembic/versions/0026_hub_tools.py
  - src/treg/alembic/versions/0027_hub_runs.py
  - tests/test_hub.py
  - tests/callmatrix/test_hub_run.py
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

**Build state: phase 2 of 8.** A steps recipe runs end to end on the call road; a script recipe
still answers **501 `hub_not_runnable`** (the sandbox is phase 3). The maker's check run, the
seller's money and every surface are later phases, and no agent-facing file mentions the hub
(CLAUDE.md: do not document what is not built).

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

## The routes (`routers/hub.py`)

Flag off ⇒ every route 404. `POST /hub/tools` (member+) takes the four files as fields —
`manifest`, `script`, `check`, `readme` — validates them against the team's world (catalog ids,
the team's own tools, existing hub ids), stores the next version, and answers 201 with
`{tool_id, version, status: "unchecked", kind}`; a refusal is 422 `{error: manifest_invalid,
field, rule}`. `GET /hub/tools/mine` lists the team's versions; `GET /hub/tools/{tool_id}[@N]`
returns one (the maker also gets script, check and readme; another team sees only live versions).

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
