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
  - src/treg/domain/catalog/params.py
  - src/treg/domain/catalog/outcome.py
  - src/treg/domain/plugins/__init__.py
  - src/treg/domain/plugins/anyapi.py
  - scripts/catalog_schema.py
  - scripts/catalog_shrink_only.py
  - scripts/catalog_v2.py
  - scripts/catalog_param_types.py
  - tests/test_catalog_schema.py
  - tests/test_catalog_params.py
  - tests/test_catalog_v2_codemod.py
  - tests/plugins/test_anyapi.py
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

## Parameter types

Every declared parameter, a row's query, path, header and body specs and a contract's identity,
filter, output and row fields alike, uses one vocabulary (`catalog/params.py`):

- **`type`** is one of JSON Schema's `string`, `integer`, `number`, `boolean`, `array` and `object`,
  or a list of two or more of them for a parameter that takes either (`[boolean, object]`).
- **`items`** says what each member of an `array` is, as a spec of its own.
- **`format`** annotates a string: `url`, `email` or `binary`. It is never a type; a routed identity
  field of `format: url` is read as a URL (`invalid_identity`), and a row field of it is never cut.
- **Range keys match the type**: `min`/`max` bound a number or an integer, `minLength`/`maxLength` a
  string, `minItems`/`maxItems` an array.
- **`enum`** names whole values: on an array it lists whole arrays; an enum of each item goes in
  `items`.

The validator holds every declaration to it (`params.declaration_errors`): a type word or list,
`items` on an array, range keys of the type, a format on a string, enum entries its type can hold.
The models still parse `minimum`/`maximum` and the contracts parser the earlier contract words
(`str`, `int`, `url`, ...) so a base catalog written before the vocabulary loads in CI's dump
comparison; the validator refuses both. A contract identity variant names each key with a type word
or a spec (`{linkedin_url: {type: string, format: url}}`).

**The check.** `params.refusal(spec, value)` is the one reading of a sent value: its type, enum and
range, and each item of an array. A null or empty value is not sent and meets an optional spec. A
query, path or form value is a string read by the declared type (ASCII digits for an integer, a
decimal for a number, `true`/`false`, JSON for an object or array; a repeated parameter is an array);
a JSON body value keeps its JSON type, except that an integer may be ASCII digits, as the count rule
reads it. A query array that is not JSON (a comma list) is relayed unread. The refusal names the
declared rule, never the value. Where the request path calls it:
[proxy model](proxy-model.md#catalog-request-checks) and [catalog](catalog.md) routing.

**The rewrite into it**: `scripts/catalog_param_types.py` (dry run by default, `--apply`), kept for
re-runs and called by the extended-file ingest so generated rows come out in the vocabulary. It maps
each word, writes a union as a type list, takes the type an `anyOf` declares, gives an untyped spec
object when its example, default, test request or note shows a structure and string otherwise, gives
a bare array its `items` from what the catalog shows of its values (the row's example, default, enum
and test request, the same parameter on the provider's other rows, a JSON sample in its note) and
string otherwise, widens a type its own test request would break, renames range keys and
`format: uri`, moves an array's item enum into `items`, and writes enum entries YAML reads as another
type in the declared one (`off` in a string enum). It prints every inferred type for a person to
correct.

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
  (omitted when a condition is not one equality; its floor `table_min` is served either way),
  `reserve.rows` with its parameter back in
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
cache admission) reads it for a row that declares a `miss` rule, for a 2xx or a 4xx status, beside
a verified adapter, in today's result reasons; routing's `child_outcome` reads it too, keeping its
own checks (declared miss, `not_found`, non-JSON body, empty required output) on top: a 2xx it
judges an error is a miss with reason `provider_error`, a 204 a miss with `adapter_miss`. A v2 row's
adapter only maps fields: `Adapter.is_miss` reads the row's rule, so every reader of an adapter's
miss reads the one declaration. Such an adapter verifies beside any `miss` rule; beside a status
rule alone, a 2xx that found nothing is a routing miss by the empty required output and a hit to
stats, unless the row's `error` rule holds on it, and an example the row judges an error verifies
the adapter as a miss does.

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
equal to its default is left out, one the row lacks is written as `null`, a row without an `expect`
takes its header's (a batch keeps the header's `expect` beside the defaults until the codemod has
run) and with none opts out with `null`, `async: false` becomes
`async: null` beside an `async` default and goes without one when neither the header nor the defaults
declare `async` (beside a header `async` block alone the row is refused), and a `bill` default, the batch's
translation of the Python branch it deletes, is inherited whole, as a `settle` default is by every
row without a settle form of its own. A row priced by its `observed_cost` is rewritten at the price
the catalog serves for it today (`_effective_cost`: the figure as a per-call USD price, `provenance:
{source: observed, confidence: verified}` checked on its `verified` day, and no note: how a price was
measured is evidence, not a caveat); an `observed_cost` beside
a `cost` is dropped, since nothing reads it. A fallback equal to its row's
`value` is dropped, since v2 keeps a fallback only beside a table, and its note joins
`price.note`. An unpriced row opts out of the price and provenance defaults (`price: null` when
there are none), inherits the settle and bill rules, which price nothing without a price, and its
outcome keys move to v2 as a priced row's do. The codemod loads the catalog with a provider's `defaults:` withheld while one
of its rows is still in today's spelling, and with them once its rows are v2. An inert
`miss.status` on a 2xx is dropped and its `means` joins the row's `note`. `--apply` edits the files
in place and keeps every comment outside the replaced keys. The fixture replay is the proof that a
rewrite changes nothing.

The validator checks a v2 row's cost beyond its model: `price.currency`, `settle.reported.unit`
and `settle.usage.unit` resolve to a rate in `fx.yaml` (a price may name a credit or meter rate
declared `usd: null`, which keeps the row off treg's key; a settle may not), usage `terms` are in
USD, and the request
paths of `reserve.rows.param`, `reserve.entities.from` and `price.table` (`when` and `times`) name
declared input fields; an equality-only table also meets today's table checks (enum and range, a
bounded `times`, a fallback at least every row's maximum), except that it may compare an optional
field without a default: an equality on an absent field does not hold, so a later row or the
fallback prices that request. A
`provenance.checked` older than 90 days warns, as `cost.checked` does.

## Provider defaults and the one merge rule

An endpoint file header may carry `defaults:`, any endpoint-level key its provider's rows inherit,
in at most one of the provider's two files. Per top-level key: a row's value wins whole, a row's
`null` opts it out, an unmentioned key takes the default. `defaults` never holds `host`, `auth`,
`credential`, `base_url`, `hosts` or `null`, and never repeats a header key of today's spelling
(`cache`, `expect`, `async`, `ceiling_param`, `response_retention`) in either of the provider's
files.

## Summary and note

A row has two prose fields, and each caveat has one home.

- **`summary`** says what the tool does, in positive terms. It is searched and shown in lists.
- **`note`** holds caveats about the whole tool or its answer that have no more specific home. Only
  the endpoint's own detail shows it; it is not searched.
- **A caveat goes to its most specific home first:** a parameter's `input.*.note`, then the charge's
  `cost.note` (`price.note` on a v2 row), then the row's `note`. Maintainer context is a YAML
  comment; evidence (dates, sample sizes, how a price was measured) is the pull request body.

The writing rules no check can judge:

- **Most callers need it.** A sentence earns its place when most callers of the tool's main use meet
  it and it changes how they call or read the answer. A failure only some callers hit belongs in
  the error they get at that moment, and nothing is written when that error already explains it.
  Correcting a false statement always qualifies.
- **Write what the probes show.** No hedging: a sentence that needs "may" or "often" stays out until
  probes confirm it, and a confirmed behaviour that holds only in some cases names the case ("when
  X, Y").
- **Summary.** Verb + object + key outputs; lead with the essentials: CLI search shows only the
  start of a summary. Positive only: a limit or trap goes to a note. No parameter or output-column
  lists.
- **Note.** Each sentence a fact and its consequence for the caller. Nothing that fits a more
  specific home. No price derivation.
- **Both.** State the present fact; never argue with an earlier version. Do not contradict another
  field. Say each thing once.

A note in that shape: "Answers for a private account are empty: check `is_private` before paging."

The validator warns when a summary runs over about 50 tokens or a note over about 75, and when the
summary, the note or a parameter or charge note contains a word from the reflection list
(`scripts/catalog_text_reflect.txt`); each warning names the word and why, and the author decides.
With `--base` it adds one error: a row respelled between `cost:` and `price:` without the
`catalog-migration` label. Run `uv run python scripts/catalog_validate.py --base origin/main`
locally to see the warnings for the rows you touched.

## The shrink-only violation list

`schema/violations.yaml` lists the violations the catalog still carries: keys only one provider
uses, rows whose parameter specs use JSON Schema words nothing reads (`description`, `examples`,
`anyOf`, `additionalProperties`), data the models or the
validator refuse (each with its reason), and v2 legacy holds. The validator fails on a use the
list does not name and on an entry nothing uses any more; `scripts/catalog_shrink_only.py` fails a
pull request that adds an entry the base branch lacks. Fix the data; never grow the list.

## Manifests, plugins and editors

- `schema.Manifest` models a provider manifest (`src/treg/catalog/providers/<slug>.yaml`). The
  validator checks any that exist; the loader reads none yet.
- A v2 row names a plugin as `<module>.<function>`; `domain/plugins` resolves it by import from the
  row's own provider module (the slug with `-` as `_`) and calls it with read-only views of the
  request and the answer; it may do no I/O ([import-boundaries](import-boundaries.md)). One that
  raises is missing evidence for its hook, and only the exception's type is logged. An adapter
  `miss` that needs an if-then-else, which the codemod refuses, becomes an outcome plugin
  (`miss: {plugin: <module>.<function>}`) when its row moves to v2. Any outcome plugin (`expect`,
  `error`, `miss`, `not_found`) may be called without the request, since stats and routing judge an
  answer alone.
- In the editor JSON Schemas (`scripts/catalog_schema.py`) every field carries `x-kind` (provider
  fact, account term, deployment switch, catalog curation) and credential placement carries
  `x-gated`.
