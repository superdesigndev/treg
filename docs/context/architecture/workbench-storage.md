---
title: Workbench table storage
status: foundational
sources:
  - src/treg/models.py
  - src/treg/alembic/versions/0077_workbench_table_metadata.py
  - src/treg/domain/governance/teams.py
  - tests/test_workbench_table_schema.py
  - src/treg/infra/workbench_store.py
  - tests/test_workbench_store.py
  - tests/test_workbench_store_lifecycle.py
related:
  - architecture/import-boundaries.md
---

# Workbench table storage

Schema and a storage adapter only: nothing provisions team databases or serves tables yet.

## Management metadata (`TeamDatabase`, `TableDoc`)

- **No database foreign keys, on purpose.** Creating these tables must not take foreign-key locks on
  the existing `org` table (`test_workbench_table_schema.py` holds an Org update open during the
  upgrade). Ownership checks are the application's job; an unchecked database id is not
  authorization.
- `connection_ref` is an opaque reference to trusted configuration; never store tokens here.
- `TeamDatabase.database_generation` fences a replaced or restored database; it is not a schema
  revision. `TableDoc.database_generation` is the generation in which the physical table was last
  verified: null until verification, and it must not follow the database's generation on restore. A
  table is usable only when both records are ready and the generations match.
- Physical table names are lowercase `[a-z_][a-z0-9_]*`, never `sqlite_`-prefixed, so uniqueness agrees
  with Turso's case-insensitive identifiers; DDL must still quote them.
- Team deletion removes `TableDoc` before `TeamDatabase` through `ORG_SCOPED_MODELS`; it does not touch
  remote databases, so remote cleanup must exist before provisioning is enabled.

## Storage adapters (`infra.workbench_store`)

`TursoStore` (Turso SQL over HTTP) and `SQLiteStore` (file-backed, for development and contract tests)
share one contract: `read`, and an async `transaction` context that owns BEGIN, COMMIT and ROLLBACK.
The caller supplies trusted SQL with positional parameters and a server-selected database; the module
authorizes nothing. Credentials and adapters stay on the server, never in user scripts. Do not hold a
PostgreSQL connection while awaiting remote storage.

External constraints the adapter is built around:

- **Turso transactions expire after five seconds.** The deadline defaults to four and must be between
  0.5 and five; COMMIT is refused with under 0.5 seconds left (`transaction_expired`, rolled back).
  Keep batches short and do no unrelated I/O inside a transaction.
- **Reads use a separate Turso `--read-only` token** and never fall back to the write token.
  `PRAGMA query_only = ON` is not a remote read boundary: Turso Cloud can reject it, and a pipeline
  keeps executing after an error.
- **Turso Cloud can omit Base64 padding** on BLOB results; both padded and unpadded forms are accepted.
- Remote URLs must be HTTPS, redirects are off, and a response `base_url` must match the configured
  origin, so credentials never go to a server-selected host.

**No automatic retries.** A transport failure or unreadable COMMIT result raises
`StoreError("outcome_unknown")` and sets `tx.outcome_unknown`: the write may be durable, and the caller
must resolve that before replaying. Cancellation during COMMIT sets the same flag but re-raises
`CancelledError`. Errors expose stable codes, never SQL values, tokens or remote bodies.

The contract tests use SQLite and an HTTP fixture; before business integration, verify against a real
Turso Cloud database that the read-only token rejects a mutation through `read`.
