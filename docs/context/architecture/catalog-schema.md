---
title: Catalog schema - the strict models every catalog reader shares, in today's spelling and v2
status: building
sources:
  - src/treg/domain/catalog/schema/__init__.py
  - src/treg/domain/catalog/schema/base.py
  - src/treg/domain/catalog/schema/paths.py
  - src/treg/domain/catalog/schema/common.py
  - src/treg/domain/catalog/schema/legacy.py
  - src/treg/domain/catalog/schema/v2.py
  - src/treg/domain/catalog/schema/manifest.py
  - src/treg/domain/catalog/schema/files.py
  - src/treg/domain/catalog/schema/violations.py
  - src/treg/domain/catalog/schema/violations.yaml
  - src/treg/domain/catalog/schema/export.py
  - src/treg/domain/catalog/outcome.py
  - src/treg/domain/plugins/__init__.py
  - scripts/catalog_schema.py
  - scripts/catalog_shrink_only.py
  - scripts/catalog_v2.py
  - tests/test_catalog_schema.py
  - tests/test_catalog_v2_codemod.py
related:
  - architecture/catalog.md
  - architecture/money.md
  - architecture/replay.md
---

# Catalog schema

One set of strict pydantic models defines what a catalog file may say. The loader
(`domain/catalog/store.py`) and the validator (`scripts/catalog_validate.py`) both read every
endpoint file through them, so the two can no longer disagree about a key, a type or a date.

## Strictness

- **Unknown keys are refused** at every modelled level: the file header, the endpoint row, `input`,
  `test_request`, each parameter spec, `cost` and its sub-keys, and every v2 key.
- **A file or row the models refuse fails the load**, never an empty provider; so does a duplicate
  endpoint id, and a `contracts.yaml` or `adapters.yaml` with an unknown key or an expression that
  does not parse. A missing catalog directory is still an empty catalog.
- **The models own keys, types and shapes.** Completeness rules that depend on the tier (a core row
  needs a capability and a cost, or a price in v2; a file cites its sources) stay validator rules
  with their messages.
- **One YAML reading.** Dates stay the strings they were written as, for the loader and the
  validator alike.

## Two spellings, one resolved row

A row is in the **v2 spelling** when it declares `price`, `provenance`, `reserve`, `settle`, `bill`,
`error` or `ceiling_param`, or, carrying no `cost`, writes an outcome key in the v2 shape. Every
other row is in **today's spelling**. Mixing `cost` with a v2 key is refused.

- A row in today's spelling resolves exactly as before: the loaded catalog is byte-identical.
- A row in v2 resolves to its v2 keys as plain JSON values (`price`, `provenance`, `reserve`,
  `settle`, `bill`, `expect`, `error`, `miss`, `not_found`, `ceiling_param`): every key present,
  `None` where absent, `bill` with its defaults filled in, money as the quoted decimal strings it
  was written as, predicates as written, and `tier` from the filename. `schema.is_v2(endpoint)`
  tells them apart. The validator refuses a row's own `ceiling_param` until non-negotiable 4 names
  that key; the file header's applies to v2 rows as to today's.
- A v2 row also serves `cost` in today's shape, derived from its price, provenance, reserve and
  settle: a `type` label (free; per_result when a reserve, count or plugin decides the bill;
  per_call when a miss is charged; else per_success), a price table as today's equality maps
  (omitted when a condition is not one equality), `reserve.rows` with its parameter back in
  `queryParams`/`pathParams`, `reserve.entities` as `{entities: {}}`, `settle.count` and a
  `settle.reported` path (`reported_charge`) with answer-relative paths, and `settle: usage` with its
  meters; `round` is not shown. Every view, eligibility check and price display that reads `cost`
  reads a v2 row unchanged; billing never reads it
  ([money](money.md#rows-in-the-v2-spelling)).

The byte-identical claim is checked by the catalog dump in [replay](replay.md).

v2 paths are one syntax (`response.tasks.0.status_code`, `body.limit`, `query.q`, `*`, `[k=v]`)
and one predicate (`{path, op, value}`, a list for all, `{any: [...]}`), parsed and evaluated by
`schema/paths.py`. A path's location must be one its reader allows: request paths in price tables
and holds, answer paths in outcome rules and settles, both only in `bill.free_when`. A path with
`*` or a filter selects many values and a single-value reader takes the first; a missing path makes
every op false except `absent`; answer equality is exact, request equality compares after input
defaults and type coercion. A usage meter is one value: no `*` and no root filter.

## The outcome classification

`domain/catalog/outcome.py` judges one answer to a v2 row from its one declaration: a non-2xx is a
miss when a `miss` rule lists its status and its `when` or `plugin` holds, else an error; a 2xx is
an error when `expect` does not hold or `error` holds, a miss when it is a 204 or a status-less
`miss` rule holds, else a hit. Billing reads it in the settlement order; `results.classify` (stats,
cache admission) reads it for a row that declares a 2xx miss beside a verified adapter, in today's
result reasons; routing's `child_outcome` reads it too, keeping its own checks (declared miss,
`not_found`, non-JSON body, empty required output) on top: a 2xx it judges an error is a miss with
reason `provider_error`, a 204 a miss with `adapter_miss`. A v2 row's adapter only maps fields:
`Adapter.is_miss` reads the row's rule, so every reader of an adapter's miss reads the one declaration.

## Rewriting rows: `scripts/catalog_v2.py`

The codemod rewrites rows in today's spelling by the section 4.4 table and writes today's
behavior, not a wished one: a per_result or per_call row bills misses and errors, a result-aware
row gets today's error envelope as an `error` rule, an adapter's `miss` moves to the row. Two
adapter spellings are read as today's evaluator reads them: `x == []` is `x` present and empty (a
null list stays a hit), and `coalesce(a, b, []) == []`, whose coalesce skips null, `''` and `[]`,
is one absent-or-empty rule per path. A per_success row with an `expect` keeps its error envelope
out of the `error` rule: today its failed `expect` is free while an envelope is charged unless it
misses, which one `bill.error` cannot say, so billing and routing read the envelope by the miss rule
and stats and archive admission stop calling it an error. Dry-run (the default) prints each rewrite, warns where it reads
an answer shape differently, and lists the rows it cannot rewrite with the reason (a provider
decided in Python, a single-provider cost key, a guessed hold, a fallback beside a different
`value`, a replaced key that defines a YAML anchor another row aliases); every rewritten row it
prints parses whole. A provider whose header declares `defaults:` is rewritten against them: a key
equal to its default is left out, one the row lacks is written as `null`, an `expect` the row does
not declare is inherited (the batch moves the header's `expect` there), `async: false` becomes
`async: null` beside an `async` default and goes without one when neither the header nor the defaults
declare `async` (beside a header `async` block alone the row is refused), and a `bill` default, the batch's
translation of the Python branch it deletes, is inherited whole. A fallback equal to its row's
`value` is dropped, since v2 keeps a fallback only beside a table, and its note joins
`price.note`. An unpriced row only opts out of the price defaults, and its outcome keys move to v2
as a priced row's do. The codemod loads the catalog with a provider's `defaults:` withheld while one
of its rows is still in today's spelling, and with them once its rows are v2. An inert
`miss.status` on a 2xx is dropped and its `means` joins the row's `note`. `--apply` edits the files
in place and keeps every comment outside the replaced keys. The fixture replay is the proof that a
rewrite changes nothing.

The validator checks a v2 row's cost beyond its model: `price.currency`, `settle.reported.unit`
and `settle.usage.unit` resolve to a rate in `fx.yaml` (a price may name a credit or meter rate
declared `usd: null`, which keeps the row off treg's key; a settle may not), usage `terms` are in
USD, and the request
paths of `reserve.rows.param`, `reserve.entities.from` and `price.table` (`when` and `times`) name
declared input fields; an equality-only table also meets today's table checks (required or
defaulted fields, enum and range, a bounded `times`, a fallback at least every row's maximum). A
`provenance.checked` older than 90 days warns, as `cost.checked` does.

## Provider defaults and the one merge rule

An endpoint file header may carry `defaults:`, any endpoint-level key its provider's rows inherit,
in at most one of the provider's two files. Per top-level key: a row's value wins whole, a row's
`null` opts it out, an unmentioned key takes the default. `defaults` never holds `host`, `auth`,
`credential`, `base_url`, `hosts` or `null`, and never repeats a header key of today's spelling
(`cache`, `expect`, `async`, `ceiling_param`, `response_retention`) in either of the provider's
files.

## The shrink-only violation list

`schema/violations.yaml` lists the violations the catalog still carries: keys only one provider
uses, rows whose parameter specs use JSON Schema words nothing reads, data the models or the
validator refuse (each with its reason), and v2 legacy holds. The validator fails on a use the
list does not name and on an entry nothing uses any more; `scripts/catalog_shrink_only.py` fails a
pull request that adds an entry the base branch lacks. Fix the data; never grow the list.

## Manifests, plugins and editors

- `schema.Manifest` models a provider manifest (`src/treg/catalog/providers/<slug>.yaml`). The
  validator checks any that exist; the loader reads none yet.
- A v2 row names a plugin as `<module>.<function>`; `domain/plugins` resolves it by import from the
  row's own provider module (the slug with `-` as `_`) and calls it with read-only views of the
  request and the answer; it may do no I/O ([import-boundaries](import-boundaries.md)). One that
  raises is missing evidence for its hook, and only the exception's type is logged.
- In the editor JSON Schemas (`scripts/catalog_schema.py`) every field carries `x-kind` (provider
  fact, account term, deployment switch, catalog curation) and credential placement carries
  `x-gated`.
