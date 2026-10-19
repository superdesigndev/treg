---
title: The Workbench — tables, runs and triggers in the dashboard
status: shell behind TREG_WORKBENCH_ENABLED; the tabs fill in over the next changes
sources:
  - contracts/workbench/openapi.yaml
  - frontend/src/workbench/WorkbenchPage.vue
  - frontend/src/workbench/routes.ts
  - frontend/src/workbench/api/client.ts
  - frontend/src/workbench/api/types.gen.ts
  - frontend/src/state/workbench.js
  - frontend/scripts/fixture-plugin.mjs
related:
  - interface/dashboard.md
  - architecture/workbench-storage.md
  - architecture/hub.md
---

# The Workbench

One navigation item, three tabs: the team's tables, the runs that fill them, and the triggers that
start those runs. `/meta.workbench` (`workbench_enabled`) decides whether the entry exists; with it
off the dashboard is unchanged.

## The contract comes first

`contracts/workbench/openapi.yaml` is the shared Workbench transport draft, written before the
routes exist so the dashboard, the CLI and the MCP tools are built against one description. It sits
at the repository root, not under `frontend/`, because the server's tests validate their responses
against the same file. `contracts/workbench/fixtures/` holds one JSON answer per operation and case,
named `<operationId>.<case>.json`; they are the only invented data and carry example names and
amounts only.

The TypeScript types are generated from the contract (`npm --prefix frontend run contract:types`)
and committed; `frontend/src/workbench/api/client.ts` is the only module that imports them, and
pages read view models from it. Changing the API means changing the yaml, regenerating, and
adjusting fixtures in the same change.

Defaults remain optional in generated request types. Explicit request variants retain required
row revisions and update identities in TypeScript as well as JSON Schema. Mutation methods accept
an explicit key from their caller and preserve the payload; the client never generates a replacement
key or retries an uncertain operation on its own. Success types include every declared 2xx response.

## Fixtures reach the page two ways, from one directory

`TREG_FIXTURES=1 npm --prefix frontend run dev` mounts `scripts/fixture-plugin.mjs`, which answers
`/api/workbench/*` from the fixtures instead of proxying to the server. Browser tests will
intercept the same paths with Playwright and read the same files. There is no second copy of the
data anywhere.

The middleware returns the declared status for each fixture and paginates its finite arrays. It
does not implement database transactions, authorization, Run admission or idempotency. The
development-only `X-Treg-Fixture-Case` header selects ready/replay/terminal cases explicitly; it is
not a production API feature. A long response wait never selects a terminal fixture implicitly.

## Addresses

`#workbench/<tab>` with the table, run or cell carried the way `routes.ts` parses and prints it.
The view is in both hash whitelists, like every other view ([dashboard](dashboard.md)), and
`syncTabUrl` keeps the address current as the tab or the open object changes.

## Table contract boundaries

The contract distinguishes the verified database generation and schema version from the metadata
revision and each row's revision. Ready tables require verified versions; creating tables can
return null. `columns` is the API projection of `TableDoc.column_metadata`; column IDs survive SQL
renames. `Row.cells` contains business values, while write requests use explicit `values` envelopes.

Presentation edits address stable column IDs. Physical changes use separate maintenance operations,
never business Runs. Creation defines structure; imports use bounded writes after readiness.
Conditional patch never recreates a deleted row. A matching committed write receipt precedes new
schema/row revision checks, after authorization, maintenance, generation and expiry checks. Expired
or unknown outcomes cannot be retried with an automatically replaced identity. The write-key wire
encoding and recovery window must be agreed before SDK writes ship.

## Run admission and retry boundaries

Run admission and selective retry require a caller-supplied idempotency key. A response wait is
an observation preference; it cannot create another execution or cancel accepted work. Dry-run
previews create no Run. Pending execution returns 202 and a handle; terminal outcomes and previews
return 200. Stop is naturally idempotent and preserves an already terminal outcome.

Retry selects retained failed item identities, not the table's current error cells. It requires a
terminal source, rejects blocked selections as a whole, and preserves source code and bindings
while reading current selected rows. It creates a new Run and may incur charges again. Interrupted
and unstarted work are separate progress categories. These are contract requirements for future
routes and runtime implementation, not guarantees implemented by the fixture server.

## Install and version boundaries

Stable Install and Trigger IDs accompany human-readable names and keys. Proposed save requests
require a compare-and-set revision; updates also carry stable identity to prevent a reused name
from accepting an old edit. Revision zero means create, not unconditional overwrite. The API prefix
and aggregate Install PUT are still integration decisions: do not implement omission-as-archive
until replacement has a concurrency guard covering omitted and concurrently added triggers.

A saved Tool version can still be building or awaiting review. Saving, publishing and Hub Listing
approval are distinct; `latest_version` can be ahead of `serving_version`, and `published_at` can
be null. None of these contracts requires automatically upgrading an existing Install.
