---
title: The tool hub — tools a maker publishes, made of other tools
status: in-progress (phase 1 of 8: the manifest and the table; behind TREG_HUB_ENABLED, off)
sources:
  - src/treg/domain/hub/__init__.py
  - src/treg/domain/hub/manifest.py
  - src/treg/application/hub/__init__.py
  - src/treg/routers/hub.py
  - src/treg/alembic/versions/0026_hub_tools.py
  - tests/test_hub.py
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

**Build state: phase 1 of 8.** A tool can be validated and stored; nothing runs. The runner, the
sandbox, the maker's check run, the seller's money and every surface are later phases. Until the
runner exists, a hub tool found on the call road answers **501 `hub_not_runnable`**, never a
silent 404, and no agent-facing file mentions the hub (CLAUDE.md: do not document what is not built).

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
