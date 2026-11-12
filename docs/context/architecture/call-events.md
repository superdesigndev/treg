---
title: Call events - one pseudonymous event per call, to an Iceberg table outside Postgres
status: shipped
sources:
  - src/treg/call_events.py
  - src/treg/application/call_events.py
  - src/treg/domain/catalog/routing/segments.py
  - src/treg/catalog/contracts.yaml
  - tests/test_call_events.py
  - tests/test_request_segments.py
---

# Call events

Benchmarks of catalog endpoints (how often a finder finds, whether what it found later verifies,
how two providers compare on the same question) are batch reads over months of calls, so they stay
out of the production database. Call events carry, per call, exactly what those reads need, to an
HTTP stream that writes an Apache Iceberg table (a Cloudflare Pipelines stream with an R2 Data
Catalog sink). Postgres is not touched: no table, no column, no query on the call path. Nothing in
treg reads the table back.

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
  retried with backoff (`_RETRY_DELAYS_S`); a refusal (other 4xx) is not. Once a batch exhausts its retries,
  later batches get one attempt each until one lands, so a stream outage never stalls the
  flusher on backoffs. Shutdown drains with one attempt per batch.
- **Duplicates are possible; deduplicate on `event_id`.** A timed-out attempt may still have been
  accepted by the stream, so its retry can land the same batch twice. Every event carries a unique
  `event_id`; readers count distinct ids.
- **Loss is measurable.** Each process periodically logs its cumulative counters, and once at
  shutdown; its loss rate is `dropped_* / queued` from its latest line.
- **Pseudonymous.** No raw email, name, domain, phone or URL is emitted. Identifiers are
  HMAC-SHA256 under the server-side secret, so a reader of the table cannot
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
emits as one). Overflow children are not emitted. An own-key call whose answer streams
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

One table for every capability; the generic columns are built in `application/call_events.py`.
A column with no value is omitted from the event rather than sent as null, so it reads as SQL
NULL (a JSON null in a `json` column would be stored as the string `'null'`).
`adapter_fingerprint` hashes the adapter definition so a change of mapping shows in the data.

Capability-specific data rides in string-keyed maps, so a new capability needs no table change.
The stream has no map type; they travel as JSON objects (`json` columns):

- `input_type` names the contract identity variant the question is expressed in, `input_id` is
  the HMAC of that variant's normalized values. It is computed from the contract identity after
  derivation (`canonical_identity`), never from the vendor request's shape. A direct call is read
  back through its adapter's `in` map, its `in_read` rules (a field the provider takes reshaped:
  HLR Lookup's digits without `+`, TrestleIQ's national US numbers) plus any request parameter
  named exactly like an identity field (a finder whose adapter sends `full_name` may be called with `first_name` +
  `last_name`). A caller's
  `first_name`+`last_name` derives `full_name`, so `{full_name, domain}` and
  `{first_name, last_name, domain}` asked of a direct finder, a routed child or the routed parent
  give one `input_id`.
- `input_keys`: `{field: hmac}` for every identity field present after derivation.
- `input_meta`: non-sensitive segments: `domain_class` (`company`, `webmail`, `edu`, `gov`) and
  `tld` of the input domain (or the email's), `calling_code` of an international input phone
  (`44`, `1`), plus any filter declared `analytics: value`, plus the categories of the contract's
  segment set (`segments:` on the contract, below). A routed
  parent also carries `route_order` and `route_retry_order` (what decided its order) and, with published rules on,
  `route_rules_version` and `route_rule` ([route-rules.md](route-rules.md)).
- `answer_keys`: `{field: hmac}` for output fields declared `analytics: key`.
- `answer_values`: `{field: value}` for output fields declared `analytics: value`, as strings
  (capped length), exactly as the adapter produced them, plus `<field>_calling_code` beside every
  keyed phone (`phone_calling_code`), so a number's country is known when its provider names none.
  A direct call's answer is read as the Arena reads it (`safe_output`), except that a recorded
  field the contract types `bool`, `int` or `float` keeps its value (`name_match`, `ported`).
  A contract with a segment set that judges answers adds its row counts here (`rows`,
  `rows_company_match`, below) with `segments_version`.
- `answer_flags`: derived from email-shaped keys: `is_role`, `free_mail`, and `domain_match`
  when the input named a domain (prefixed `<field>_` for a field other than `email`). The same
  `domain_match` comparison is disclosed to callers on a routed email-find hit as
  `_treg.domain_match` / `X-Treg-Domain-Match` (see [catalog.md](catalog.md)); this event flag
  stays the analytics form.

## Request segments

A contract may name a segment set (`segments: people_search`), defined once in
`domain/catalog/routing/segments.py`. Its function reads the identity and filters after derivation
and returns categories, each a word from the set's fixed vocabulary; `segment` adds them to
`input_meta` with `segments_version`, the set's version, which moves whenever a rule or a
vocabulary changes. No title, query, name, place or domain is recorded, only the category it falls
in. A filter already recorded as a value (`country`) keeps its own spelling; the set only adds
keys. The same function serves the event and the route rule's match, so a measured segment and
a routed segment cannot disagree; the patterns are module constants, so an offline analysis can
import them instead of keeping its own copy.

`people_search` (version 1), each key always present unless noted:

| Key | Vocabulary | From |
| --- | --- | --- |
| `title_class` | `none`, `leader`, `function`, `multi`, `other` | `multi`: several titles in one string (`,` `/` `\|` `;` ` or `); `leader`: function `executive` (CEO, founder, owner, president, managing director, partner, Geschäftsführer, directeur général, amministratore delegato, director general) |
| `title_function` | `sales`, `marketing`, `product`, `data`, `engineering`, `it_security`, `hr_talent`, `finance`, `operations`, `customer`, `legal`, `design`, `education`, `healthcare`, `real_estate`, `executive`, `other`, `none` | first matching pattern in `TITLE_FUNCTIONS` |
| `title_seniority` | `c_level_founder`, `vp`, `head_director`, `manager_lead`, `individual`, `none` | first matching pattern in `TITLE_SENIORITIES` |
| `title_lang`, `q_lang` | `en`, `non_en`, `none` | a non-ASCII letter or a listed non-English role word |
| `title_words` | `1-2`, `3-4`, `5+`, `none` | words in the title |
| `has_country` | `true`, `false` | |
| `country_iso2` | an upper-case ISO 3166 alpha-2 code; absent when the value names none | `us`, `US`, `United States`, `uk` -> `US`, `GB` |
| `country_raw_case` | `upper`, `lower`, `name`, `other`, `none` | how the caller spelled it (`US`, `us`, `Germany`, `Us`) |
| `has_location` | `true`, `false` | |
| `location_kind` | `city`, `region`, `country_name`, `unknown`, `none` | short place lists and region words (`Greater`, `area`, `county`) |
| `limit_bucket` | `<=5`, `6-10`, `11-25`, `>25`, `none` | the effective limit, the contract default when none is sent |
| `q_words` | `1-3`, `4-8`, `9+`, `none` | |
| `q_has_place` | `true`, `false`, `none` | a country, region or major city named in `q` |
| `name_given` | `true`, `false` | `full_name` present after derivation |
| `has_keywords` | `true`, `false` | |

A set may also count how an answer's rows fit the question; `people_search` writes these to
`answer_values` as decimal strings, beside `segments_version`. Nothing from a row leaves, only the
counts. A direct call is counted over every row its adapter mapped, in the contract's row shape,
not the Arena's shorter reading; a routed parent over its assembled answer. A check the question
gives nothing to judge by is absent, not zero.

| Key | Counts | Present when |
| --- | --- | --- |
| `rows` | rows in `people` | the answer has a `people` list |
| `rows_with_profile_url` | rows with an `http(s)` profile URL on a dotted host | always with `rows` |
| `rows_dup` | rows repeating an earlier row's person (same LinkedIn handle or URL, else same name and company) | always with `rows` |
| `rows_company_match` | rows whose company domain is the asked `company_domain` (or a subdomain), or whose company name is its first label | `company_domain` was asked |
| `rows_title_match` | rows whose title has the asked title's `title_function` (a leader asked: an `executive` row) | the asked title has a function other than `other` |
| `rows_country_match` | rows whose location names the asked country (by name, a US state, never a two-letter part) | the asked country is an ISO code or name |

Where an output field goes is declared on the contract (`contracts.yaml`), default `skip`:

```yaml
output:
  email: {type: str, required: true, analytics: key}
  verified: {type: bool, analytics: value}
```

The parser refuses any other word, and a filter can never be a `key`. The public catalog contract
does not show the annotation.

Values are stored as the provider wrote them and not parsed here: country, region and size buckets
are derived at analysis time, so a better parser can re-read old events.

**Privacy.** Identifiers (email, name, domain, phone, URL, LinkedIn handle) leave only as HMAC
keys. `answer_values` is not identifier-free: a person's `location` can name a city, and next to
that person's keys it is personal data. It is kept on purpose for analysis. Never annotated as a
value: names, company names, descriptions, `about` text, follower counts, lists.

Normalization before hashing: emails lower-cased; domains without scheme, path or `www.`;
LinkedIn URLs and handles reduced to the lower-cased handle; other text case-folded with spaces
collapsed. So a find's `answer_keys['email']` equals a later verify's `input_keys['email']`.
