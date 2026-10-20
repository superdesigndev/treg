---
title: Call events — one pseudonymous event per call, to an Iceberg table outside Postgres
status: building
sources:
  - src/treg/call_events.py
  - src/treg/application/call_events.py
  - src/treg/catalog/contracts.yaml
  - tests/test_call_events.py
---

# Call events

Benchmarks of catalog endpoints (how often a finder finds, whether what it found later verifies,
how two providers compare on the same question) are batch reads over months of calls. They do not
belong in the production database: `callrecord` is large, a routed parent has no outcome columns,
`params_hash` is per endpoint, and joining a find's answer to a later verify meant re-parsing
archived requests and reading bodies from object storage. Call events carry, per call, exactly
what those reads need, to an HTTP stream that writes an Apache Iceberg table (a Cloudflare
Pipelines stream with an R2 Data Catalog sink). Postgres is not touched: no table, no column, no
query on the call path.

## Guarantees

- **Off unless configured.** `TREG_CALL_EVENTS_URL` and `TREG_CALL_EVENTS_HMAC_SECRET` must both
  be set; otherwise nothing is built or queued. `TREG_CALL_EVENTS_TOKEN` is sent as a bearer when
  the stream requires authentication.
- **Never in a call's way.** `emit` is synchronous, takes no lock, opens no session and never
  raises. It queues a small dict of columns already in hand plus a builder; the builder (which
  parses request and answer bodies and hashes identifiers) runs in the one flusher task per
  process. Same shape as `audit.py` and `analytics.py`: bounded queue (`_MAX_PENDING`), a bound on
  what waiting builders keep alive (`_MAX_RETAINED_BYTES`: bodies, query strings, and a routed
  parent's caller body and assembled answer by their known sizes; past it the event goes out
  without input/answer columns), batched POSTs, drop-and-count on overflow, failure or a raising
  builder (`call_events.counters`, logged at ERROR on the first drop and every thousandth).
  Losing an event costs a sample, never money, a response or a hold.
- **Retried, then dropped.** A transient POST failure (timeout, transport error, 429, 5xx) is
  retried twice with backoff; a refusal (other 4xx) is not. Once a batch exhausts its retries,
  later batches get one attempt each until one lands, so a stream outage never stalls the
  flusher on backoffs. Shutdown drains with one attempt per batch.
- **Duplicates are possible; deduplicate on `event_id`.** A timed-out attempt may still have been
  accepted by the stream, so its retry can land the same batch twice. Every event carries a unique
  `event_id`; readers count distinct ids.
- **Loss is measurable.** Each process logs its cumulative counters every five minutes
  (`call events stats: pid=… pending=… queued=… sent=… retried=… dropped_failed=…`), and once at
  shutdown. The loss rate per process is `dropped_* / queued` from its latest line.
- **Pseudonymous.** No raw email, name, domain, phone or URL is emitted. Identifiers are
  HMAC-SHA256 under the server-side secret (32 hex characters), so a reader of the table cannot
  test a guessed address without the secret. A team that opted out of the archive
  (`Org.archive_opt_out_at`) gets no answer columns; its input keys remain, since they are needed
  to count its calls per question and carry nothing readable.
- **Exactly one terminal event per async task** (one `event_id`; a retried POST may store it twice). See below.

`archive_key_hash` and `archive_content_hash` are the archive's own identities (plain SHA-256,
not keyed), carried so a sample can be traced to its stored body.

## Where events are emitted

| Site | Kind | Notes |
|---|---|---|
| `service._audit` (every resolved catalog call: direct, routed child, check, poll) | `call` / `async_submit` | input from the vendor request read back through the adapter; answer from the buffered body |
| `service` marketplace refusal (unresolved catalog call) | `call` | generic columns only |
| `service._routed_event` (once per routed parent, after `run_routed` returns or raises) | `call` | input from the caller's fields; answer from the router's core output; `served_by`, `tried_count`, `check_verdict` from `CallContext.route_summary` |
| `asynctasks._finish_terminal`, and the worker's timed-out / retired-account releases | `async_terminal` | answer from the terminal poll body |

Own tools and hub runs emit nothing of their own (a hub step is an ordinary catalog call and
emits as one). Overflow children are not yet emitted. An own-key call whose answer streams
through unbuffered has no answer columns, as it has no `hit` in `callrecord`.

## Linking

`call_ref` is the call's `X-Treg-Call-Id`. A child's ref is its parent's plus a step
(`:rN` waterfall attempt N, `:v` the check, `:pN` a routed poll, `:sN` a hub step); events carry
`parent_ref`, `step` and `waterfall_position` parsed from it. An async task's submission
(`async_submit`) and its outcome (`async_terminal`) share the submission's `call_ref`; the terminal
event carries `hit`, `verdict`, `cost_charged_micro` (what the task settled at; the submission's
is null), `async_outcome` (`success`, `failure`, `billed_failure`,
`timed_out`, `account_retired`, `closed_hold`) and the answer columns. Its input lives on the
submission event. `_finish` moves a task out of `pending` once, under the task row's lock,
whichever of the caller's poll (`observe_owned_poll`) and the worker sees the answer first, and
only that transition emits, so a late reread emits nothing.

## Columns

One table for every capability. Generic columns: `schema_version`, `event_id`, `event_time`,
`event_kind`, `org_id`, `client`, `capability`, `endpoint_id`, `provider`, `credential_tier`,
`call_ref`, `parent_ref`, `step`, `waterfall_position`, `is_eval` (team listed in
`TREG_CALL_EVENTS_EVAL_ORGS`), `status_code`, `refused_by`, `hit`, `verdict`, `cached`,
`cost_estimated_micro`, `cost_charged_micro`, `upstream_ms`, `duration_ms`, `served_by`,
`tried_count`, `check_verdict`, `async_outcome`, `archive_key_hash`, `archive_content_hash`,
`adapter_fingerprint` (12 hex of the adapter definition, so a change of mapping shows in the data).
A column with no value is omitted from the event rather than sent as null, so it reads as SQL
NULL (a JSON null in a `json` column would be stored as the string `'null'`).

Capability-specific data rides in string-keyed maps, so a new capability needs no table change.
The stream has no map type; they travel as JSON objects (`json` columns):

- `input_type` names the contract identity variant the question is expressed in, `input_id` is
  the HMAC of that variant's normalized values. It is computed from the contract identity after
  derivation (`canonical_identity`), never from the vendor request's shape. A direct call is read
  back through its adapter's `in` map plus any request parameter named exactly like an identity
  field (a finder whose adapter sends `full_name` may be called with `first_name` +
  `last_name`). A caller's
  `first_name`+`last_name` derives `full_name`, so `{full_name, domain}` and
  `{first_name, last_name, domain}` asked of a direct finder, a routed child or the routed parent
  give one `input_id`.
- `input_keys` — `{field: hmac}` for every identity field present after derivation.
- `input_meta` — non-sensitive segments: `domain_class` (`company`, `webmail`, `edu`, `gov`) and
  `tld` of the input domain (or the email's), plus any filter declared `analytics: value`.
- `answer_keys` — `{field: hmac}` for output fields declared `analytics: key`.
- `answer_values` — `{field: value}` for output fields declared `analytics: value`, as strings.
- `answer_flags` — derived from email-shaped keys: `is_role`, `free_mail`, and `domain_match`
  when the input named a domain (prefixed `<field>_` for a field other than `email`).

Where an output field goes is declared on the contract (`contracts.yaml`), default `skip`:

```yaml
output:
  email: {type: str, required: true, analytics: key}
  verified: {type: bool, analytics: value}
```

The parser refuses any other word, and a filter can never be a `key`. The public catalog contract
does not show the annotation.

Normalization before hashing: emails lower-cased; domains without scheme, path or `www.`;
LinkedIn URLs and handles reduced to the lower-cased handle; other text case-folded with spaces
collapsed. So a find's `answer_keys['email']` equals a later verify's `input_keys['email']`.

## Not built

Per-capability benchmark tables are derived from this one later, in batch. Nothing here reads the
table back.
