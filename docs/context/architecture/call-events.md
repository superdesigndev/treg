---
title: Call events - one pseudonymous event per call, to an Iceberg table outside Postgres
status: shipped
sources:
  - src/treg/call_events.py
  - src/treg/application/call_events.py
  - src/treg/domain/catalog/routing/segments.py
  - src/treg/catalog/contracts.yaml
  - src/treg/application/call/call_errors.py
  - scripts/call_events_actionable_errors.sql
  - tests/test_call_events.py
  - tests/test_request_segments.py
  - tests/test_call_errors.py
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
(`:rN` waterfall attempt N, `:v` the check, `:u` the upgrade, `:pN` a routed poll, `:sN` a hub
step, `:sN.M` item M of a hub `for_each` step); events carry `parent_ref`, `step` and `waterfall_position` parsed from it. An async task's submission
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
keys. A direct or child call's request is read back through its adapter (`vendor_identity`).
Besides the `in` map, every field an `in_expr` sends where it can be found is read back
(`field_paths`): each expression is run on placeholder values, and a field is read from the
place its placeholder lands whole, provided it lands there alone, no other field's placeholder
reaches that expression, and none fills the place when the field is not sent. Text placeholders
are several upper-case words, so a lossy transform (`split_first(full_name)`) never reads back as
the whole field; a number is tried at sizes on both sides of the usual page-size clamps. So
`list(country)`, `obj('include', list(upper(country)))`, `list(obj('domain', company_domain))`,
`at_most(limit, 10)` and `at_least(limit, 10)` read back, and `coalesce(location,
country_name(country))` or a `join` of several fields do not. A name sent as
`split_first(full_name)` plus `split_last(full_name)` reads back as those two words joined, which
is exactly what the provider was asked. An `in` entry whose place holds an object (`{v: title}`)
gives way to the field's path inside it. A routed child therefore lands in its parent's segment for whatever it
sent; a filter its provider takes nowhere reads as not sent, and a clamped limit as the value sent.
Its `country_raw_case` is the spelling it sent the provider; group routed calls by the parent's
(`parent_ref`) for the caller's question. Categories need the request to read back into a complete
variant (`canonical_identity`; a list of titles counts); otherwise filters are not attached either,
so it records none rather than "none sent". The same function serves the event and the route rule's match, so a measured segment and
a routed segment cannot disagree; the patterns are module constants, so an offline analysis can
import them instead of keeping its own copy.

`people_search` (version 1), each key always present unless noted:

| Key | Vocabulary | From |
| --- | --- | --- |
| `title_class` | `none`, `leader`, `function`, `multi`, `other` | `multi`: several titles in one string (`,` `/` `\|` `;` ` or `); `leader`: function `executive` (CEO, founder, owner, president, managing director, partner, Geschäftsführer, directeur général, amministratore delegato, director general) |
| `title_function` | `sales`, `marketing`, `product`, `data`, `engineering`, `it_security`, `hr_talent`, `finance`, `operations`, `customer`, `legal`, `design`, `education`, `healthcare`, `real_estate`, `executive`, `other`, `none` | first matching pattern in `TITLE_FUNCTIONS` |
| `title_seniority` | `c_level_founder`, `vp`, `head_director`, `manager_lead`, `individual`, `none` | first matching pattern in `TITLE_SENIORITIES`; `president` after `vice` is a VP |
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
not the Arena's shorter reading; a routed parent over the answer it returned, an empty one
included, so a routed miss records `rows: 0`. A check the question
gives nothing to judge by is absent, not zero; a request that cannot be read back still records
`rows`, `rows_with_profile_url` and `rows_dup`.

| Key | Counts | Present when |
| --- | --- | --- |
| `rows` | rows in `people` | the answer has a `people` list |
| `rows_with_profile_url` | rows with an `http(s)` profile URL on a dotted host | always with `rows` |
| `rows_dup` | rows repeating an earlier row's person (same LinkedIn handle or URL, else same name and company) | always with `rows` |
| `rows_company_match` | rows whose company domain is the asked `company_domain` (or a subdomain of it); a row with no domain, when its name minus a legal form (`Inc`, `GmbH`) is exactly the domain's first label (`MetaMask` is not meta.com) | `company_domain` was asked |
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

## Failed calls

Every failed call (status 400 or above) also writes one `call_error` line on the `treg.call_errors`
logger, `call_error` followed by JSON, where its outcome is final: the call's audit (every resolved
catalog call, a routed child, check or poll included, and own tools), an unresolved catalog call,
a routed parent, a hub run that failed or was refused, and for a refusal or crash nothing
audited, the router's fallback on a root and `execute_call` on a child (`call_errors.emit`). It is
a log line, not a column: no table changes.

| Field | Value |
| --- | --- |
| `org_id`, `call_ref`, `step` | the team, the `X-Treg-Call-Id`, and `r2`/`v`/`u`/`p1`/`s1` on a child, `s1.0` on a hub `for_each` item (null on a root) |
| `road` | `catalog`, `routed` (the routed parent), `own_tool`, `hub`, `unresolved` |
| `endpoint_id`, `provider`, `tier` | a catalog id (null when the name is not one), and the credential tier |
| `status`, `refused_by` | the status of this attempt (the caller's, except on a direct attempt overflow served), and treg's refusal mechanism as the event's `refused_by` |
| `by` | `upstream` when the provider answered or failed to (timeout, dropped connection), else `treg` |
| `error_class` | one word below |
| `caller_side` | `true` for `caller_balance_or_cap`, `caller_policy` and `caller_provider_account` |
| `kind` | the failure kind treg raised (`daily_cap_reached`, `catalog_parameter_invalid`), or its internal `log_kind` when it has one |
| `signal` | the provider's documented error read from its answer (`balance`, `quota`, `burst`), per `domain/capacity/signatures.py` |
| `parameter`, `rule` | for treg's own refusal of a request: the field it is about (`queryParams.limit`, `body.urls.0`; `undeclared` for a name the endpoint does not declare, a field refused as undeclared, or an index that is not a short index into a field declared an array; a JSON key may hold dots, so it is never read as a path) and the rule it broke (`required`, `type`, `enum`, `range`, `repeat`, `undeclared`, `json`, `fixed`, `platform` for a limit of treg's shared key, `authorization_method`, `unsupported`). Both ride on the refusal (`CallFailure.parameter`, `.rule`), never in what the caller sees |

`error_class` is read in order from the failure kind (its class sits beside its blame in
`types.FAILURE_KINDS`, one table for both), else `refused_by`, else the provider's documented error,
else the status (`call_errors.py`). `caller_input`, `caller_balance_or_cap`, `caller_policy` (the
team's own deny rules: `policy_denied`, `tool_access_denied`, `tag_blocked`), `caller_auth` (no
valid treg token), `caller_provider_account`, `not_found`, `rate_limit`, `upstream_error`,
`timeout`, `treg_capacity`, `treg_policy` (a paused provider, a retired tool, an operator's quota or
flow control, fair share), `our_provider_account`, `treg_error`, `other`. A provider account error
belongs to whoever holds that account. An access error (an upstream 401/403) is
`caller_provider_account` on the `credential` and `tool` tiers. A credit error (an upstream 402, or
an out-of-credit answer, documented or matched by the `unrecorded` phrase check, whose `signal`
stays on the line) is the caller's only when the caller pays the provider: on treg's pay-per-use
OAuth app it is `our_provider_account`, like every error on treg's own key. An own tool's answer is
read with the documented errors of the provider whose base URL is on the tool's host (its line's
`provider` stays null), or with the generic ones and the phrase check when none is. A catalog call
refused for want of a credential (404, `credential_missing` to the caller and the Arena) is
`caller_provider_account` when only the team's own key can serve the endpoint, and
`our_provider_account` (`kind` `platform_key_missing`, the refusal's internal `log_kind`) when
treg's own key or the anonymous fallback should serve it but the deployment has no key or
allow-list entry for its provider. A routed parent the provider refused (`route_caller_fault`,
`route_not_found`) is `by` `upstream`; one refused because treg's capacity took every candidate
away (`route_no_candidate` answered 503) is `treg_capacity`. The answer is decoded by its
`Content-Encoding` before its error code is read. A failed hub run is read by the error its
detail names when that is a known failure kind: a crash in treg (`hub_run_crashed`) is
`treg_error`, `by` `treg`; the caller's price ceiling (`hub_run_max_cost`) and a refusal that stops
every step (the caller's balance or a cap) are `caller_balance_or_cap`.
Any other is `upstream_error`, `by` `upstream`: a failed step, the maker's script, or a limit the
maker's manifest declares (`hub_step_cap`, `hub_wall_clock`). No request or answer value, header,
credential or message text is logged; the answer is read only for its documented error code.

A default error view leaves `caller_side` out; the full count is the same query without that
filter. A routed call writes its parent's line and one per failed child, check or poll, so a count
of failed calls keeps only root lines (`step` empty); a direct attempt that overflow served is a
root line too, though the caller got the overflow's answer. In Loki, with the deployment's own
stream selector:

```logql
sum by (error_class, by) (count_over_time({<stream>} |~ "^call_error "
  | pattern "call_error <payload>" | line_format "{{.payload}}" | json | __error__=""
  | step="" | caller_side="false" [1h]))
```

The full count drops `| caller_side="false"`; a count of attempts drops `| step=""`.

`scripts/call_events_actionable_errors.sql` is the same view over the events: failed calls by
class, refuser, tier and endpoint, with per-class totals, leaving caller-side classes out unless
`include_caller_side` is set to true. An event has only `status_code`, `refused_by` and
`credential_tier`, so the query's class is the coarser reading: a refusal kind or a provider's
documented error code that changes the class shows only in the log line; a missing credential is
`not_found` there and stays in, since an event cannot tell the own-key-only half apart. A routed
parent the provider refused (`route_caller_fault`) is `refused_by` `request` in its event, so the
query counts it under treg while its line says `upstream`; for the treg-or-upstream split of wrong
parameters read the log line, or the query's `root_calls` for calls that are not routed. A direct
attempt that overflow later served still logs its line and keeps its event, as both record the
attempt.
