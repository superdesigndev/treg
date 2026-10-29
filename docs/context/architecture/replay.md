---
title: Replay - recorded exchanges re-run through one checkout's billing, outcome and routing code
status: building
sources:
  - src/treg/application/replay/__init__.py
  - src/treg/application/replay/__main__.py
  - src/treg/application/replay/records.py
  - src/treg/application/replay/engine.py
  - src/treg/application/replay/fixtures.py
  - src/treg/application/replay/differ.py
  - src/treg/application/replay/export.py
  - tests/test_replay.py
  - tests/test_replay_export.py
related:
  - architecture/money.md
  - architecture/proxy-model.md
  - architecture/catalog.md
  - architecture/archive.md
  - ops/deploy.md
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

The engine itself only mirrors the call site's arguments (the `provider_failed_<status>` reason,
the observed override of a rejected submission) and composes the existing pure functions below.

## What one replay runs

| Output group | Production function | Fields |
|---|---|---|
| `reserve` | `price_call`, `money.with_margin` | `estimate_micro`, `hold_micro`, `unit_micro`, `reported_charge_unit_micro`, the basis's `when`, kind, reserve and fallback, and a digest of the whole basis |
| `settle` | `submission_state`, `decide_settle` | `deferred`, `billable`, `reason`, `observed_micro`, `actual_micro`, `charged_micro` |
| `outcome` | `results.classify`, `results.verdict` | `state`, `reason`, `hit`, `verdict` |
| `adapter` | `Adapter.from_upstream`, `Adapter.is_miss` | whether the body is JSON, the adapter miss, a digest of the mapped core, the empty required fields |
| `routing` | `child_outcome` (verified, routable adapters only) | `kind`, `why`, `rows` |
| `stats` | `stats.Tally.fold` | whether the row counts, and which counters it moves |
| `async` | `asynctasks.classify_terminal`, `settlement.settle`, `terminal_judgement` | terminal `outcome`, `settled_micro`, `charged_micro`, `hit`, `verdict` |

Every call is replayed as a call on treg's own key (`tier: platform`), with the settings of the
process running it. A step that cannot apply is `null`; a step that raises reports only the
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
                "body_b64": "...", "accept": {},
                "fidelity": "exact|reconstructed|snippet|unavailable" },
  "response": { "status": 200, "media_type": "...", "headers": null, "body_b64": "...",
                "fidelity": "exact|snippet_redacted_truncated", "headers_fidelity": "synthesized" },
  "recorded": { "estimate_micro": 0, "observed_micro": null, "charged_micro": 0, "hit": null,
                "verdict": null, "credential_tier": "platform", "routed_child": false },
  "async":    { "descriptor": {}, "settlement_basis": {}, "status": "...", "settled_micro": 0 } }
```

`query_items` is the caller's query as pricing reads it, path parameters included;
`path_params` names which of them the path consumed. `headers_fidelity` is present only when the
headers were synthesized rather than recorded. `recorded.routed_child` replays the settle as
a routed child. The engine reads nothing else from `recorded`: production's own amounts are carried
for whoever extracted the corpus, not compared. A record holds no team, member, key, call reference
or IP.

## The fixture corpus

`fixtures` writes a corpus from the checkout's own catalog, for every priced endpoint (a positive
price, a request table or a usage meter; routed parents excluded): the committed example answer,
an answer the adapter judges a miss (the example blanked along the adapter's own miss paths), the
declared miss status, a 2xx error envelope, a 400 and a 500, and for an async endpoint terminal
success, failure and billed-failure documents. Requests come from `test_request`, so a record's
request fidelity is `reconstructed` and its response carries no headers. Nothing in it is
customer data, and it is generated, never committed.

## The production corpus

`treg-worker admin export-replay-corpus` (`export.py`) writes the same records from what
production already keeps, so a change can be replayed on real answers as well as on fixtures. It
is the one part of the package that reads a database and the archive.

```bash
treg-worker admin export-replay-corpus --endpoints <id>,<id> --out <dir>/corpus.jsonl \
  --manifest <dir>/manifest.jsonl [--per-class 20] [--days 30] [--statement-timeout-s 20]
treg-worker admin export-replay-corpus --provider <name>,<name> --out ... --manifest ...
```

**Selection.** The named endpoints, or every priced endpoint of the named providers; which
endpoints a change touches is the caller's question (the fingerprints above answer it). An
unknown name exits 2 before anything is read. Only the calls a replay models are read: metered
calls on treg's own key (`credential_tier: platform`, so no own key, overflow child or routed
parent), of kind `call`, never a cached hit or a refusal, inside the window. Per endpoint and
class, up to `--per-class` records (default 20), one per question first (the archive key for an
answer, `params_hash` for a failure, the outcome and verdict for a task), newest first, then
repeats, newest first.

| Class | Rows | Source |
|---|---|---|
| `hit` | 2xx with `hit`, or no verdict and a charge | the archive key's request, the exact snapshot the call received |
| `miss` | 2xx judged a miss, or no verdict and no charge | the same |
| `non2xx` | a provider status outside 2xx with failure evidence inside its 14-day retention, purged or not | `error_request` and `error_response` |
| `async_terminal` | a settled or released task with its terminal answer archived | the task row and that answer |

**Fidelity, with what production keeps today.**

- The archive keeps the vendor-facing URL. The caller's query comes back from it by matching the
  endpoint's path template: the `{placeholder}` values become path parameters, and the template's
  fixed query is removed. Such a request is `reconstructed`; one with neither is `exact`.
- A provider whose charge is a response header (`settle._CREDIT_HEADERS`) gets that header back,
  computed from the recorded `cost_observed_micro` as the exact inverse of the settle's
  conversion (`headers_fidelity: synthesized`). No observed cost means no header, which settles
  as unreported, as the call did.
- A failure is a `snippet` request and a `snippet_redacted_truncated` response: the redacted,
  truncated evidence, with the allowlisted headers it inlines parsed back into `headers`.
- A task's request is `reconstructed` from a table basis, the only basis that keeps it, and
  `unavailable` otherwise; the task settles on its own frozen basis either way.
- A 2xx answer production never kept is counted, never invented: `expect_unavailable` on an
  endpoint that declares `expect` (an answer that failed it is not recorded), `not_archived`
  otherwise, `no_body` or `policy_forbidden` for a key whose bytes are not kept.

**Privacy.**

- A record carries no team, member, key, call reference or IP; `captured_at` is the day. The
  manifest maps each `rid` to its `callrecord.id` (and a task's call id), for whoever debugs the
  run; it never belongs in a report.
- A team that no longer exists, or that is opted out of the archive when the export runs, is
  never read; erasing a team's archive requires that opt-out first. The export reads only
  platform-key answers, which erasure leaves by design (archive.md, "Opting out").
- A record whose request, answer or evidence contains any spelling of treg's own credential for
  the provider (`evidence._secret_renderings`, every account, as is or percent-decoded) is
  dropped, and an endpoint whose credentials cannot be rendered exports nothing.
- Both files are created new, readable by their owner only, and removed when the run fails. The
  command prints counts per endpoint and class, with the reason for every record it dropped; a
  body never reaches its output or a log.

**Load.** One endpoint at a time; one class query at a time through
`ix_callrecord_endpoint_id_created_at`, each a short session reading at most five times
`--per-class` rows under a statement timeout. The sessions come from the worker process's API
pool, as every `treg-worker` command's do: nothing else runs in that process, so the export
competes with no request for a slot. A query that times out is retried on
half its window, three times, after which that class reports `timed_out`. Answers are selected by
the Arena reader's exact-hash selection (`arena_insights._evidence_pointers`) and read through
`archive_bodies.read` (R2 first under the result read switch, the database as fallback) after the
session closes, eight at a time; terminal answers through `archive.load_terminal_snapshots`. How
to run it against a hosted database is in ops/deploy.md, "Worker commands".

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
old and new values and its `where`. A list value matches a list field element for element. The
report names the first matching declaration, but every declaration that matches some change
counts as exercised.

## In CI

`.github/workflows/replay.yml` runs on every pull request: it generates the corpus from the base
(the merge commit's first parent), replays it on the base and on the merge result, and fails on an
undeclared difference. The declarations are the pull request body's fenced `replay-declared`
blocks, so they belong to the change that needs them and never accumulate in the repository;
editing the body re-runs the job. The report goes to the job summary.

A base without the engine skips the comparison. The ordinary test suite also replays the whole
fixture corpus, so a seam that starts raising fails every pull request, not only those that differ.
