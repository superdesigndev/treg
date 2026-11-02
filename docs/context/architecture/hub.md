---
title: The tool hub - tools a maker publishes, made of other tools
status: shipped
sources:
  - src/treg/domain/hub/__init__.py
  - src/treg/domain/hub/manifest.py
  - src/treg/domain/hub/refs.py
  - src/treg/domain/hub/graph.py
  - src/treg/application/hub/__init__.py
  - src/treg/application/hub/runner.py
  - src/treg/application/hub/sandbox.py
  - src/treg/application/hub/limits.py
  - src/treg/application/hub/health.py
  - src/treg/hub_sandbox.py
  - src/treg/routers/hub.py
  - src/treg/routers/catalog.py
  - src/treg/routers/web.py
  - src/treg/routers/hub_gate.py
  - src/treg/application/call/service.py
  - src/treg/domain/money/__init__.py
  - src/treg/mcp.py
  - frontend/src/state/hub.js
  - frontend/src/pages/HubPage.vue
  - frontend/src/pages/HubRunPage.vue
  - src/treg/cli.py
  - src/treg/worker.py
  - src/treg/models.py
  - src/treg/web/index.html
  - src/treg/web/skill.md
  - src/treg/web/llms.txt
  - src/treg/alembic/versions/0044_hub_tools.py
  - src/treg/alembic/versions/0045_hub_runs.py
  - src/treg/alembic/versions/0046_hubtool_check_result.py
  - src/treg/alembic/versions/0047_hubrun_output.py
  - src/treg/alembic/versions/0048_hubtool_data.py
  - src/treg/alembic/versions/0049_hubtool_listed_public_log.py
  - src/treg/alembic/versions/0050_hub_listing.py
  - docs/hub-recipes/data-sheets/run.js
  - docs/hub-recipes/data-csv/run.js
  - docs/hub-recipes/engineering-team-size/run.js
  - tests/test_hub.py
  - tests/test_hub_sandbox.py
  - tests/callmatrix/test_hub_run.py
related:
  - architecture/proxy-model.md
  - architecture/money.md
  - architecture/catalog.md
  - architecture/import-boundaries.md
---

# The tool hub

A **hub tool** is a tool a maker publishes on treg, made of other tools: either a JSON list of
steps, or a script that runs in a sandbox. Every step is an ordinary treg call, so identity, the
access rules, key injection and money exist once per call; the hub adds the graph, the sandbox,
the maker's road, the seller's price and the surfaces.

## Visibility gates

Everything sits behind `hub_enabled` (`TREG_HUB_ENABLED`, default off): with the flag off every
hub route answers 404, the call road never asks the hub, the agent files carry no hub text, and
the dashboard shows no Hub entry. With the flag on, `TREG_HUB_TEAMS` (team slugs) and
`TREG_HUB_USERS` (sign-in emails) narrow it; both empty means every team. A listed person sees the
hub in every team they act for; unlisted teammates do not.

- Gates that have a caller (the hub router, `/call/` of a hub id) use `hub_app.enabled_for(slug,
  email)` and answer 404 outside the lists, exactly as with the flag off.
- Open surfaces follow the reader: catalog search and get, the share page, the hub sections of
  `/skill.md` and `/llms.txt`, and the MCP tools `hub_create`, `hub_update`, `hub_mine`
  (`mcp._HubToolsGate`) resolve the key or session the request carries
  (`routers/hub_gate.reader`) and ask `hub_app.visible_to`. A reader with no team sees the hub only
  when no list is set. `treg skill bootstrap` sends the key so a listed reader's agents get the
  hub sections; the static plugin files never carry them (`build_plugin.py`).
- `hub_gate.remember` caches who a credential is for a short TTL, keyed by a hash of key, session
  and team header. Only visibility may ride on that cache, never authorization or money.

## The files

`recipe.json` (the manifest), `run.js` (script recipes), `check.json`, `README.md`, and optionally
`data.csv`. `treg hub init <name> [--script]` writes a vendor-neutral skeleton; `docs/hub-recipes/`
holds worked recipes. A JSON step's `input` is an object, so an upstream with an array body needs
a script.

**The manifest** (`domain/hub/manifest.py`) is validated by pure rules; every refusal is
`ManifestError(field, rule)`, a dotted path into the file plus the rule it broke, because the maker
is usually an agent fixing a file. `name` becomes the id `<team-slug>.<name>`. An input is required
by having no `default` (a `required` key is refused). `uses` is the security boundary: each entry is
a catalog id or one of the maker's own tool names; a hub id is refused (depth one), and an own tool
named with a dot cannot be in `uses`. Every step's `call` must be in `uses`. References are parsed
and the graph built at publish, so a cycle or an unknown step is refused before anyone pays.

**Pricing** covers only the maker's part; the steps' provider fees are billed to the caller on top.

- A steps recipe has one fixed price per successful run, `"pricing": {"price_usd": N}` (a
  top-level `price_usd` means the same).
- A script declares `"pricing": {"max_price_usd": N}` and prices itself in `run.js` with
  `ctx.charge(usd, note)`, one line at a time. A script with no `pricing` is free and `ctx.charge`
  is refused; `price_usd` on a script is refused.

`ctx.charge` exists because only the script knows which branch ran: a fee, a per-result charge, a
margin on a catalog call (`r.cost_usd`), or the maker's cost of an own-key step at a vendor treg
cannot see (that step costs the caller nothing, non-negotiable 1, and the maker real money).

## The reference language and the graph (JSON road)

`domain/hub/refs.py` is deliberately small: no arithmetic, no condition, no function. A recipe that
needs those is a script; do not grow the language. `domain/hub/graph.py` derives the edges from the
references (never declared).

## The runner (`application/hub/runner.py`)

`POST /call/<team>.<name>` with the inputs as the JSON body. The ceiling is `X-Treg-Run-Max-Cost`,
else the tool's own `limits.cost_usd`, else the runner default; it counts price plus steps,
including what is in flight. The seller-price hold goes through `reserve_admission` like a metered
catalog reserve (429 `money_admission_timeout` before any step).

Each unit is one `execute_call` under its own child hold: the same gates, key injection and
reserve-and-settle as a direct call. **Two teams meet in one run:** a catalog step runs as the
caller (their money, their rules, their key if they hold one); a step on one of the maker's own
tools runs as the maker (their registered key, unmetered), which is how a shared recipe uses a key
the caller never holds. That step runs on a snapshot with the email `hub-caller:<org>` and no key
(`_maker_snapshot`), so the maker's log never names the caller.

Rules that look removable and are not:

- Every child asks `Accept-Encoding: identity`; the runner reads the bytes itself.
- A script may set headers on `ctx.call` except identity and framing headers
  (`_SCRIPT_HEADER_DENY`, plus `x-treg-*`); the tool's binding always wins.
- A step's answer is read into memory up to `MAX_BODY_BYTES` and the stream closed beyond it.
- A child's cost comes from its call record, never from an upstream `X-Treg-Cost-Micro`.
- A child call never resolves to a hub tool (`CallInput.child_of`): no nested runs.
- The price hold closes on every path: a refusal or disconnect releases it, a crash becomes 424
  `hub_run_crashed`, never a 500 with an open hold.
- Publish and the scheduled check hold no database connection during the run.
- A caller's trace, error and idempotent replay never carry an upstream error body; server
  tracebacks never reach the maker's log.
- A team tool's `base_url` may not be treg itself (`points_at_treg`), refused at `/tools` and again
  in the runner for older tools, since it would let a hub tool call other hub tools.

Failure: when a step fails, nothing new starts, in-flight steps finish, and the run answers 424
`hub_run_failed`; passing the ceiling is 402 `hub_run_max_cost`. Money spent on completed steps
stays spent and the price is released. Runs per team are capped in-process
(`application/hub/limits.py`, 429 `hub_busy`); the cap is per process, not global.
`Idempotency-Key` covers the whole run through the parent's store. `HubRun` keeps one row per run
and is deleted with the calling team.

## The sandbox (`application/hub/sandbox.py`, `treg/hub_sandbox.py`)

`run.js` runs in a separate short-lived process per run, `python -m treg.hub_sandbox`: a scrubbed
environment, a private temporary HOME, its own process group, POSIX rlimits, a wall-clock kill, the
whole group killed on every exit. Inside, QuickJS has no network, no file system, no `require`, no
`process`, no timers. There is no `ctx.secret`: a key reaches an upstream only inside `ctx.call`,
through a registered tool's binding, so a script never holds one.

`ctx.call` crosses to the parent as one JSON line over stdin/stdout; the parent enforces `uses` per
call, the step cap, the ceiling and the output contract. `target` is a catalog id, `<tool>/<path>`
for an own tool, or a full URL allowed only when it starts with the base URL of an own tool named in
`uses`; the sandbox never opens a socket. `ctx.call` returns a promise and does not block the
engine, so concurrent calls run in the parent up to the JSON road's width. An endless loop ends by
the parent's kill: the engine cannot call into Python with its own time limit set. `timeout_s` on
one call answers `{status: 0, timed_out: true}` instead of ending the run, and the cancelled child
releases its hold.

## The maker's road (`routers/hub.py`, `application/hub/__init__.py`)

`POST /hub/tools` validates the files against the team's world, stores the next version as
`checking`, then **runs `check.json` once for real** as an in-process `POST /call/<id>@<version>`
with the maker's identity: steps are charged to the maker at normal prices, never the seller's
price. Pass makes the version `live`; fail keeps it `failed` with the reason in `check_result`. A
version under check is visible only to its maker. `POST /hub/run` is the dry run (nothing stored).
`DELETE` retires every version (off the call road at once; rows kept).

Versions: the newest `live` serves `/call/<id>`; `<id>@N` pins one, and a pinned old version stays
callable for `OLD_VERSION_DAYS` after a newer live one exists (`tool_for`). The call road resolves
an own tool first, then a catalog id, then the hub (`application/call/service.py`).

`hub_create`'s MCP description carries the rule that a credential the team does not hold is never
hard-coded into a script; register it first, then name the tool.

## Health and the scheduled check (`application/hub/health.py`)

Health is derived, never stored: `failing` when the last three runs (callers' and checks alike) all
failed. A failing tool stays callable; a failing check never retires a tool. `treg-worker hub check`
runs each live tool's newest `check.json` as its maker (identity rebuilt from the publisher's
membership, else an owner's), recorded as a `HubRun` with `caller_email = "hub-check"`.

## The seller's money (`domain/money/__init__.py`)

One extra hold `{run}:price` on the caller at run start holds the most the maker can earn on this
run (`price_usd` or `max_price_usd`; 402 `hub_price_unaffordable` before any step). On success
`settle_to_in_transaction` settles a recipe's price in full or a script's sum of `ctx.charge`
lines, refunds the rest to the caller, and in the same transaction credits the maker an `earned`
block (a `settle` on the payer naming the payee, a `grant` on the payee naming the payer's run).
This is the only cross-team money movement in treg. Any failure releases the whole hold. An answer
whose every declared output field is empty settles the seller price at 0 (`_empty_answer`). No
seller price when the caller is the maker. `earned` spends after the free kinds and before
purchased money (`money._KIND_ORDER`). The earnings view excludes the maker's own runs and never
names who called. A price change (`treg hub price`) applies to later runs with no version bump; on
a reviewed tool it waits in `HubListing.pending_pricing`.

## Listing and review

A hub tool is out of catalog search until its maker asks (`PATCH /hub/tools/{id} {"listed": true}`)
and a superadmin approves. The request is a `HubListing` row, one per tool, not per version (the
per-version `hubtool.listed` column is unread, expand-only).

- Approved tools merge into search by score with the catalog's own scoring and no boost
  (`score_extra`, `merge_by_score`); their `added` is the approval day.
- An optional manifest `capability` is only a proposal; the approval sets
  `HubListing.capability`. An approved job makes the tool a sibling of that job's providers and
  seeds its `observed.ok_rate` (`seeded_observed`). It is never a routed child: an agent compares
  and picks (non-negotiable 4).
- `HubListing.reviewed` is set by the first approval and never cleared. On a reviewed tool a
  passing new version becomes `review`, not `live` (`_hold_for_review`), until an admin approves
  it; unlisting only moves the row to `unlisted`, and a rejection keeps it reviewed, so neither is
  a way round the review. A never-approved request is deleted by unlisting.
- `tool_for` serves a `review` or `checking` version only to a call by the maker's team, and a
  `rejected` tool only to its maker (`is_rejected`); others get 404 (the share page 410).
- The reviewer sees the code, the own tools' base URLs, a diff against the approved version, and
  every output field the approved check filled that the new one leaves empty (`_fields_lost`),
  because a maker writes its own `check.json`. A maker cannot review its own tool's runs.
- Team names that read as treg, `official`/`verified` or a catalog provider are refused
  (`teams.reserved_reason`) unless a superadmin acts, since a hub id starts with the team slug.

## The surfaces

- `skill.md` and `llms.txt` carry the hub section inside `<!--hub-->...<!--/hub-->` blocks that
  `routers/web.py` strips for readers who cannot see the hub; `scripts/build_plugin.py` drops them
  unless `--with-hub`.
- `GET /catalog/endpoints/<id>` answers for a hub id with the public contract (`kind: "hub"`,
  inputs, output, price line, health, version, `call_template`, the hosts of the maker's own tools
  that receive the caller's inputs). Never the script, the maker's tools or a key.
- The public price (`price_ranges`) counts only runs of the priced version, by other teams once
  there are any, else the maker's own marked `price_from_tests`.
- The share page `GET /hub/<id>` (and `.md`) is readable without sign-in, `noindex` and out of the
  sitemap. Its run log (when the maker leaves `public_log` on) and check trace show outcomes and
  shape only, never the caller, inputs, output, what each step called, or the check's sample inputs.
- The dashboard (`frontend/src/state/hub.js`, `pages/HubPage.vue`, `pages/HubRunPage.vue`) asks no
  hub route when `/meta.hub` is false. Files are read-only there: a new version comes from the
  terminal or the agent.
