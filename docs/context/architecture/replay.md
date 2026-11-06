---
title: Replay - recorded exchanges re-run through one checkout's billing, outcome and routing code
status: shipped
sources:
  - src/treg/application/replay/__init__.py
  - src/treg/application/replay/__main__.py
  - src/treg/application/replay/records.py
  - src/treg/application/replay/engine.py
  - src/treg/application/replay/fixtures.py
  - src/treg/application/replay/differ.py
  - tests/test_replay.py
  - scripts/catalog_dump.py
  - tests/test_catalog_dump.py
related:
  - architecture/money.md
  - architecture/proxy-model.md
  - architecture/catalog.md
---

# Replay

A replay answers one question about a change: given the same recorded exchanges, does this
checkout reserve, settle, judge and route them the way the other one did? It is the proof gate for
catalog and billing changes that must not move money or outcomes, and the way to see exactly which
endpoints a change that should move them did move.

A replay is a pure function of one record and one checkout (code plus catalog). It never opens a
network connection, a database session or a ledger transaction, and never calls a provider:
re-asking upstream would spend money and return a different answer from the one that was billed.
`tests/test_replay.py` replays the whole fixture corpus with sockets and session makers refused.

## The seams

The engine calls production code, never a copy of it. Each seam is a pure function lifted out of
the path that still calls it:

| Seam | Lifted from, and still called by | Decides |
|---|---|---|
| `call.resolve.price_call` | `_resolve_marketplace_call` | the estimate, per-row and per-credit units, the frozen settlement basis, the request evidence |
| `call.settle.decide_settle` | `_platform_settle`, everything before the ledger | billable, reason, observed, raw actual (after the repeat price and the routed-child free rule), charged after margin |
| `call.route.child_outcome` | the routed child loop in `_run_routed` | `declared_miss`, `not_found`, `rejected`, `error`, `adapter_error`, `miss` (with its reasons) or `hit` (with its rows) |
| `call.service.submission_state` | the call use case, before settling | whether a `when: terminal` 2xx is a pending task, and why one is not an accepted submission |
| `asynctasks.terminal_judgement` | `_finish_terminal` | the hit and verdict word a terminal outcome records |
| `call.cost.settle_terminal` | the worker's `_finish` | what a `success` or `billed_failure` terminal answer settles at, by the frozen basis or a v2 row's frozen terms |

The engine itself only mirrors the call site's arguments (the `provider_failed_<status>` reason,
the observed override of a rejected submission) and composes the existing pure functions below.

## What one replay runs

One output record per input, grouped as `reserve`, `settle`, `outcome`, `adapter`, `routing`,
`stats` and `async` (`engine.py`), each from the production function that decides it.
Every call is replayed as a call on treg's own key (`tier: platform`), with the settings of the
process running it and a v2 row's frozen cost terms as the call site carries them. A step that
cannot apply is `null`; a step that raises reports only the
exception's type and typed kind, never its message, which may quote the body. An accepted async
submission is `settle.deferred` with no outcome, adapter or routing: it settles, is judged and is
routed on its terminal answer, which an `async_terminal` record carries. That record settles on its
own frozen basis and descriptor when it carries them, else on the ones the checkout derives.

## The corpus record

One JSON object per line, bodies in base64, a fidelity flag on each part:

```
{ "rid": "<opaque>", "endpoint_id": "...", "class": "hit|miss|non2xx|async_terminal",
  "captured_at": "...", "source": "archive|error_evidence|async_terminal|fixture",
  "request":  { "method": "...", "vendor_url": "...", "query_items": [[k, v]], "path_params": {},
                "body_b64": "...", "accept": {}, "fidelity": "exact|reconstructed|snippet" },
  "response": { "status": 200, "media_type": "...", "headers": null, "body_b64": "...",
                "fidelity": "exact|snippet_redacted_truncated" },
  "recorded": { "estimate_micro": 0, "observed_micro": null, "charged_micro": 0, "hit": null,
                "verdict": null, "credential_tier": "platform", "routed_child": false },
  "async":    { "descriptor": {}, "settlement_basis": {}, "status": "...", "settled_micro": 0 } }
```

`query_items` is the caller's query as pricing reads it, path parameters included;
`path_params` names which of them the path consumed. `recorded.routed_child` replays the settle as
a routed child. The engine reads nothing else from `recorded`: production's own amounts are carried
for whoever extracted the corpus, not compared. A record holds no team, member, key, call reference
or IP.

## The fixture corpus

`fixtures` writes a corpus from the checkout's own catalog, for every priced endpoint (a positive
price, a request table or a usage meter; routed parents excluded): the committed example answer,
an answer the adapter judges a miss (the example blanked along the adapter's own miss paths, or a
v2 row's `miss` rule paths), the declared miss status, a 2xx error envelope, a 400 and a 500, and
for an async endpoint terminal success, failure and billed-failure documents. Requests come from `test_request`, so a record's
request fidelity is `reconstructed` and its response carries no headers. Nothing in it is
customer data, and it is generated, never committed.

## The production corpus

Records from real traffic are extracted by a private operator script, run where production's
database and archive are reachable, that writes this record shape to a temporary location deleted
after the replay. It reuses treg's own readers rather than copying them:
`arena_insights._evidence_pointers` (the exact snapshot each call received, with its media type,
under the `replay` read path), `archive_bodies.read`, `archive.load_terminal_snapshots` (a task's
terminal answer and its status), the archive opt-out state, the credential renderings of
`call.evidence`, and `records.line` for each line it writes.

## Running it

```bash
uv run python -m treg.application.replay fixtures corpus.jsonl
uv run python -m treg.application.replay run corpus.jsonl baseline.jsonl --fingerprints baseline.json
# ... in the other checkout, on the SAME corpus file:
uv run python -m treg.application.replay run corpus.jsonl candidate.jsonl --fingerprints candidate.json
uv run python -m treg.application.replay diff baseline.jsonl candidate.jsonl \
  --base-fingerprints baseline.json --candidate-fingerprints candidate.json --declared declared.yaml
```

`diff` exits 1 when any changed field is undeclared or a record is missing from either side.

## The diff report

The differ joins the two outputs by `rid` and prints one row per endpoint and class: records,
records changed, each changed field with `old -> new`, and the declaration that covered it (`NO`
when one did not). A value is printed only when it is a number, a boolean, null or a short code;
any other string is reduced to its field name. A change only in a reason code or a digest is listed
in its own table, apart from changes in money or outcome.

Two more sections follow:

- **Declarations no record exercised**, so a declaration that tested nothing is visible.
- **Touched endpoints**: every endpoint whose effective catalog entry (the loaded row, its cost
  view, its adapter and contract) differs between the two fingerprint files, with its records per
  class; one with none is `untested`.

### Declaring a difference

A declaration is a YAML mapping; a file holds a list of them:

```yaml
- provider: serpstat               # or endpoint: <id or glob>, exactly one of the two
  field: settle.actual_micro       # a field or a glob (`settle.*`); default every field
  from: 4000                       # optional: only this old value
  to: 0                            # optional: only this new value
  where: {outcome.state: error}    # optional: the candidate's other fields must hold these
  reason: serpstat envelope        # required; shown in the report
```

A changed field is declared when some declaration matches its endpoint or provider, its field, its
old and new values and its `where`. A declared value matches only a value of its own kind: a
boolean never matches a number, null matches only null and a string only a string, while an integer
and a float compare by value. A list value matches a list field element for element. The
report names the first matching declaration, but every declaration that matches some change
counts as exercised.

## In CI

`.github/workflows/replay.yml` runs on every pull request: it generates the corpus from the base
(the merge commit's first parent), replays it on the base and on the merge result, and fails on an
undeclared difference. The declarations are the pull request body's fenced `replay-declared`
blocks, so they belong to the change that needs them and never accumulate in the repository;
editing the body re-runs the job. The report goes to the job summary.

When a pull request changes loader, schema or validator source (anything under
`src/treg/domain/catalog/`, `scripts/catalog_validate.py`, `scripts/catalog_dump.py` or the Replay
workflow), the same job also dumps the base checkout's catalog (`scripts/catalog_dump.py`: every
field of the loaded `Catalog` and each row's cost view, sorted keys; a dataclass field at its
default is left out, so adding a field nobody sets yet is not a difference) with the base's code and with
the merge result's code (`--catalog`) and fails on any byte difference, naming the first differing
path ([catalog-schema](catalog-schema.md)). A pull request that also rewrites rows is held to that
on its code alone; the replay judges the rows. A pull request that means to change how existing
rows are served declares the dotted paths it changes in a fenced `dump-declared` block of its body,
one glob per line with an optional `#` reason (`by_id.*.note`); the report names each glob and how
many differences it covered, and any other difference still fails. A pure catalog data pull request
skips it. The replay diff still runs and reports
when the dump check fails.

A base without the engine skips the comparison. The ordinary test suite also replays the whole
fixture corpus, so a seam that starts raising fails every pull request, not only those that differ.
