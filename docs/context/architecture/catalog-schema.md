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
  - src/treg/domain/plugins/__init__.py
  - scripts/catalog_schema.py
  - scripts/catalog_shrink_only.py
  - tests/test_catalog_schema.py
related:
  - architecture/catalog.md
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
  needs a capability and a cost, a file cites its sources) stay validator rules with their messages.
- **One YAML reading.** Dates stay the strings they were written as, for the loader and the
  validator alike.

## Two spellings, one resolved row

A row is in the **v2 spelling** when it declares `price`, `provenance`, `reserve`, `settle`, `bill`,
`error` or `ceiling_param`, or, carrying no `cost`, writes an outcome key in the v2 shape. Every
other row is in **today's spelling**. Mixing `cost` with a v2 key is refused.

- A row in today's spelling resolves exactly as before: the loaded catalog is byte-identical.
- A row in v2 resolves to typed v2 keys (`price`, `provenance`, `reserve`, `settle`, `bill`,
  `expect`, `error`, `miss`, `not_found`, `ceiling_param`), money as `Decimal`, `bill` always
  present with its defaults, `tier` from the filename, and `cost: None`. `schema.is_v2(endpoint)`
  tells them apart. Nothing in the call runtime reads the v2 keys yet, so the validator refuses
  any v2 row until the v2 settlement order ships; until then the catalog stays in today's spelling.

The v2 settlement order, the outcome classification and the first v2 rows ship next; until then no
v2 row may ship. A resolved v2 row still carries pydantic objects (`Price`, `Bill`, predicate
leaves) where served readers expect plain dicts (`.get` on `expect` and `miss`, `json.dumps` of the
row), so v2 rows need a plain-dict served representation before the validator's guard is lifted.

When a pull request changes loader, schema or validator source (anything under
`src/treg/domain/catalog/`, `scripts/catalog_validate.py`, `scripts/catalog_dump.py` or the Replay
workflow), the Replay job dumps the loaded catalog (`scripts/catalog_dump.py`: every field of the
loaded `Catalog` and each row's cost view, sorted keys) on the base and on the merge result and
fails on any byte difference, naming the first differing path. A pure catalog data pull request
skips the check and owes it nothing.

v2 paths are one syntax (`response.tasks.0.status_code`, `body.limit`, `query.q`, `*`, `[k=v]`)
and one predicate (`{path, op, value}`, a list for all, `{any: [...]}`), parsed by
`schema/paths.py`. A path's location must be one its reader allows: request paths in price tables
and holds, answer paths in outcome rules and settles, both only in `bill.free_when`.

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
  row's own provider module (the slug with `-` as `_`). The package is empty, and an import-linter
  contract keeps it free of I/O.
- `uv run python scripts/catalog_schema.py OUT_DIR` writes the JSON Schemas for editors; every field
  carries `x-kind` (provider fact, account term, deployment switch, catalog curation) and credential
  placement carries `x-gated`.
