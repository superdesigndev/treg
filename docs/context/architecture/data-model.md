---
title: Data model — the registry tables, async DB, audit writer
status: shipped
sources:
  - src/treg/alembic/versions/0042_pinned_read_scope.py
  - alembic.ini
  - src/treg/alembic/env.py
  - src/treg/alembic/versions/0001_baseline_current_schema.py
  - src/treg/alembic/versions/0002_archive_tables.py
  - src/treg/alembic/versions/0003_callrecord_cached.py
  - src/treg/alembic/versions/0004_archivekey_request_shape.py
  - src/treg/alembic/versions/0005_capacity_policy_snapshot.py
  - src/treg/alembic/versions/0006_overflow_route.py
  - src/treg/alembic/versions/0007_overflow_spend.py
  - src/treg/alembic/versions/0008_org_platform_overflow_disabled.py
  - src/treg/alembic/versions/0009_callrecord_hit.py
  - src/treg/alembic/versions/0017_async_task_record.py
  - src/treg/alembic/versions/0018_async_resource_ownership.py
  - src/treg/alembic/versions/0019_async_poll_failures.py
  - src/treg/alembic/versions/0020_callrecord_created_at_indexes.py
  - src/treg/alembic/versions/0021_ledgerentry_org_created_at_index.py
  - src/treg/alembic/versions/0022_org_spent_today_counter.py
  - src/treg/alembic/versions/0023_callrecord_org_user_created_at_index.py
  - src/treg/alembic/versions/0024_membership_calls_today_counter.py
  - src/treg/alembic/versions/0027_enrich_arena.py
  - src/treg/alembic/versions/0028_arena_insights.py
  - src/treg/alembic/versions/0029_arena_verification_snapshot.py

  - src/treg/alembic/versions/0011_callrecord_archive_link.py
  - src/treg/alembic/versions/0015_idempotentcall_membership_cascade.py
  - src/treg/alembic/versions/0053_idempotentcall_membership_expires_index.py
  - src/treg/alembic/versions/0054_callrecord_org_id_id.py
  - src/treg/alembic/versions/0061_remove_redundant_unique_indexes.py
  - src/treg/alembic/versions/0091_remove_prefix_covered_indexes.py
  - src/treg/alembic/versions/0034_managed_api_keys.py
  - src/treg/alembic/versions/0035_default_key_generation.py
  - src/treg/alembic/versions/0036_activity_key_indexes.py
  - src/treg/alembic/versions/0038_endpoint_day_stats.py
  - src/treg/alembic/versions/0074_endpoint_stat_dirty.py
  - src/treg/infra/catalog_stat_dirty.py
  - src/treg/maintenance.py
  - src/treg/web/sitetrack.js
  - src/treg/models.py
  - src/treg/alembic/versions/0052_async_task_hit.py
  - src/treg/alembic/versions/0031_archive_result_admission.py
  - src/treg/alembic/versions/0032_archive_body_storage.py
  - src/treg/alembic/versions/0039_archive_own_key_and_repeat_pricing.py
  - src/treg/alembic/versions/0043_provider_resources.py
  - src/treg/domain/provider_resources.py
  - src/treg/routers/provider_resources.py
  - src/treg/alembic/versions/0033_signup_promo_eligibility.py
  - src/treg/alembic/versions/0071_signup_promo_claim.py
  - src/treg/alembic/versions/0041_searchlog.py
  - src/treg/alembic/versions/0055_find_v2_log.py
  - src/treg/alembic/versions/0056_searchlog_verdict.py
  - src/treg/alembic/versions/0065_call_verdict.py
  - src/treg/alembic/versions/0066_endpointdaystat_verdicts.py
  - src/treg/alembic/versions/0070_api_key_spend_rollup.py
  - src/treg/alembic/versions/0072_callrecord_call_ref_index.py
  - src/treg/alembic/versions/0073_org_flow_control_version.py
  - tests/test_org_flow_control_schema.py
  - src/treg/alembic/versions/0096_route_rule_version.py
  - src/treg/alembic/versions/0075_ad_click_stash.py
  - src/treg/alembic/versions/0079_enterprise_sso.py
  - src/treg/alembic/versions/0082_oauth_sso_provenance.py
  - src/treg/alembic/versions/0083_enterprise_sso_safety.py
  - src/treg/alembic/versions/0084_enterprise_workos_cleanup.py
  - src/treg/alembic/versions/0085_enterprise_sso_blocks.py
  - src/treg/alembic/versions/0087_rankings_snapshot.py
  - src/treg/alembic/versions/0088_call_usage_rollup.py
  - src/treg/alembic/versions/0089_ledgerentry_settle_spend_index.py
  - src/treg/alembic/versions/0090_ledgerentry_topup_index.py
  - src/treg/alembic/versions/0095_ledgerentry_grant_index.py
  - src/treg/application/call_usage_rollup.py
  - src/treg/application/call_usage.py
  - tests/test_call_usage_rollup.py
  - src/treg/timeutil.py
  - src/treg/infra/db.py
  - src/treg/infra/money_timing.py
  - src/treg/infra/money_admission.py
  - src/treg/infra/kv.py
  - src/treg/infra/money_admission_reporting.py
  - src/treg/infra/money_trace.py
  - src/treg/infra/money_trace_runner.py
  - src/treg/domain/referrals.py
  - src/treg/audit.py
  - src/treg/application/evidence_retention.py
  - src/treg/analytics.py
  - src/treg/bootstrap_handlers.py
  - src/treg/ratestore.py
  - src/treg/application/auth.py
  - tests/test_postgres_reset.py
  - tests/test_alembic_expand_safety.py
  - tests/test_redundant_index_migration.py
  - tests/test_api_keys.py
  - tests/test_money_timing.py
  - tests/test_money_admission_reporting.py
  - tests/test_kv_lease_diagnostics.py
  - tests/test_money_trace.py
  - tests/test_money_trace_postgres.py
  - tests/test_money_trace_runner.py
related:
  - architecture/archive.md
  - architecture/proxy-model.md
  - architecture/auth-secrets.md
  - architecture/ads-conversions.md
---

# Data model

`src/treg/models.py` is authoritative for tables, columns, indexes and defaults, and its class
docstrings explain each table. This fragment keeps only what a declaration cannot say: who may
write a table, ordering and locking rules, and the database constraints that shape the code.
[Multi-tenancy](multi-tenancy.md) defines org scoping, role gates and team deletion.

## Ownership and behavior the schema cannot show

Single writers (AGENTS.md lists the money, audit and showcase ones):

| Table(s) | Only writer |
|---|---|
| `EndpointDayStat`, `EndpointStatCursor` | `application/catalog_stats.py` (dirty markers: `infra/catalog_stat_dirty.py`) |
| `ApiKeySpendDay`, `ApiKeySpendCursor` | `application/key_spend_rollup.py` (`treg-worker spend rollup`) |
| `CallUsageDay`, `CallUsageCursor` | `application/call_usage_rollup.py` (`treg-worker usage rollup`) |
| `CapacityPolicy`, `CapacitySnapshot` | `treg-worker capacity sweep`, never the call path ([capacity](../ops/capacity.md)) |
| `OverflowRoute` | `treg-worker overflow sync`; read-only to the call path |
| `OverflowSpend` | the overflow child's settle transaction (and the shadow probe); not a balance |
| `ProviderResource` | `domain/provider_resources.py`, driven by the call runtime; BYOK objects are never written here |
| `ArenaObservation`, `ArenaInsightState` | `application.arena_insights` |
| `ArenaVerificationSnapshot` | `application.arena_verification_insights.publish_snapshot` |
| `RankingsSnapshot` | `application.rankings.refresh`, replacing the row whole |
| `AdClickStash` | `application.signup.stash_ad_click` |
| `ArchiveKey`, `ArchiveSnapshot`, `ArchiveKeyOrg`, `ArchiveEndpointStat` | `archive.py` ([archive](archive.md)) |
| `Media` | `application.media` ([media](media.md)) |
| `Feedback` | `domain.feedback` inserts, `application.feedback` commits ([feedback](feedback.md)) |
| `FeedbackHandling`, `FeedbackHandlingEvent` | the private admin service only |
| `Referral` | `domain/referrals.py`; its money goes through `ledger.grant` |

Rollups and analytics tables are optional read models: they never determine a money transaction.

- **`Org.first_call_at`** is set once by a guarded UPDATE in the call path, never derived from
  `CallRecord`, because audit sheds rows exactly when traffic is highest. The `ad_*` and `utm_*`
  columns are set once at signup and never overwritten ([ads-conversions](ads-conversions.md)).
- **`ApiKey.last_used_at`** is throttled, best-effort display metadata, never written in the
  authentication transaction. Revoked and rotated rows stay for audit; Activity rows keep their key
  snapshot after rotation, revoke or membership removal.
- **`Invite.email_token_hash`** is the inbox-only sign-in secret, nulled on first use; the
  admin-visible code is join-only, never an auth factor.
- **`Secret`**: expiry (`expires_at`, `last_refresh_at`, `last_error`) is a separate axis from
  `health_status`; a non-refreshable token stays healthy until it silently dies.
  `authorization_method` selects a grant's method; never infer it from the token's shape.
- **`PendingOAuth`** carries every per-provider quirk through the redirect so the callback
  exchanges the code exactly as the consent URL was built.
- **`ProviderResource`** deletes tombstone the row, preserving retry authorization and audit.
  `key_slot` on it, `AsyncTaskRecord` and `AsyncResourceRecord` names the treg-owned account the
  object lives on; NULL means the primary ([proxy-model](proxy-model.md), "Several treg-owned
  accounts").
- **`AsyncTaskRecord`**: `attempts` doubles as a claim version, so an old worker cannot overwrite a
  newer claim; caller polling can finalize independently and the terminal-state guard prevents a
  double charge. The audit writer locks the task row before inserting the submission `CallRecord`,
  so a poll finishing first still gives that row its `hit`/`verdict`; when the audit row wins, the
  finalizer queues a background correction. Both key on the original `call_ref`.
- **`EndpointStatDirty`**: marker writes never commit on their own; audit marks the day in the
  same transaction as its terminal update, and the worker acknowledges only an unchanged revision.
- **`Ephemeral`** is the DB home for state that must survive restarts and stay correct across
  instances (OTPs, rate windows, CLI pairing); see [api](../interface/api.md), "CLI pairing".
  `_prune_handshakes` deletes at most 100 expired pairing rows per CLI start, so physical removal
  can lag while reads still enforce expiry.
- **`AdConversion`**: unique `(org_id, action)` is the sole idempotency mechanism, not a
  check-then-insert; rows are written synchronously in the firing transaction, unlike audit.
- **`DenyRule`**: an empty field means any, so a rule with only `method="DELETE"` blocks every
  delete. `delete_project` sweeps rules naming the project.
- **`Tool.bindings`** is a list and a request applies all of them (google-ads is an OAuth bearer
  plus a `developer-token` header); injection is in [auth-secrets](auth-secrets.md).
- **Enterprise SSO**: team deletion removes the team-scoped rows before the `Org`, and an active
  subscription blocks team deletion until canceled. Keys issued before SSO enforcement carry no
  issuance proof and cannot enter an enforced team. The cleanup-completed marker keeps cancellation
  retries idempotent and blocks a new checkout while billable WorkOS resources may remain.
  `EnterpriseSSOBlock` survives connection cleanup, leaves with the team or the erased user, and
  holds no email or WorkOS identifiers.
- **Signup credit**: `User.signup_promo_available` keeps a database default of false for old
  writers; new users insert true. `SignupPromoClaim.user_id` has no FK and is staged with the
  user-level claim ([money](money.md#signup-credit-eligibility)).
- **Arena** tables have no audit foreign key because audit retention is independent; none stores
  raw requests, responses, contacts or credentials. `RankingsSnapshot` holds no team, key or money,
  so team deletion has nothing to remove.

## Org flow control configuration storage

`OrgFlowControlVersion` is keyed by `(org_id, version)`; the schema enforces positive versions and
valid publication metadata. Allocating increasing versions, validating the JSON and keeping
snapshots immutable are the management writer's job; the schema does not prevent arbitrary SQL
updates. `org_id` has no foreign key, keeping the table independent of locks on the balance table,
so `cascade_delete_org` removes this history explicitly, and the writer must validate Org existence
and coordinate saves with Org deletion. A schema downgrade deletes the saved history.

## Route rule storage

`RouteRuleVersion` is keyed by `version` alone: one global rule set. The schema enforces positive
versions, a reviewer on every reviewed row, a publisher on every published row, and refuses any
publication state on a version that is not approved. Allocating versions, validating the rule JSON
and keeping snapshots immutable are the private admin's job; the call path never reads the table
([route-rules.md](route-rules.md)). A schema downgrade deletes the saved history.

## `CallRecord` and `LedgerEntry`: indexes are throughput

`CallRecord` (audit) and `LedgerEntry` (append-only, never pruned) are the largest tables and both
only grow. Every time-window question needs an index whose leading columns match the filter and
end in `created_at`; otherwise the planner picks an index on the other column and filters the date
in memory, reading a team's or an endpoint's whole history. All pools share one Postgres, so one
such scan queues every other query and the API pool empties into `503 treg_saturated`
([deploy](../ops/deploy.md), Database pools).

- Hot-path gates never aggregate these tables. The fail-closed daily spend cap reads
  `Org.spent_today_micro` / `spent_today_day`, kept by `domain/money` inside the balance UPDATE
  (`spent_today_from_ledger` survives for reconciliation); the per-member call cap reads
  `Membership.calls_today`. A team's busiest rows sit on every page of the day, so even an
  `(org_id, created_at)` index scan is too slow on the money path.
- Partial indexes (`kind = 'settle'`, `kind = 'topup'`, `kind = 'grant'`, `kind = 'local_run'`)
  are matched only when the query states the predicate as a constant, never a parameter. The
  spend rollup's per-team recount (`key_spend_rollup`) and the billing reads state it with
  `literal_column`.
- `SELECT DISTINCT` over a team's rows in a large index reads every one of them: Postgres before 18
  cannot skip through an index. The distinct values are walked instead, one probe per value
  (`money.tag_dims`, a recursive CTE asking for the next value after the last).
- A bound taken from a scalar subquery is unknown to the planner, which then estimates a team's
  whole history and can spend longer compiling the statement (JIT) than running it. Where the
  subquery must stay for snapshot consistency, the read turns JIT off for its transaction
  (`call_usage.rows`). A constant read earlier is no substitute: a rollup rebuild moves the cursor
  back.
- Statistics end before today, so a `created_at >= today` bound is estimated at one row and picks
  the `(org_id, created_at)` index even for a team with a full day behind it. The usage tail
  (`call_usage.rows`) bounds itself by its UTC day expression instead, leaving `(org_id, id)` to
  find the rows after the cursor.
- Watermark walks state the bound as `created_at >= w AND (created_at > w OR id > w_id)`, never as
  one OR: inside an OR Postgres bounds the scan only from above and walks from the oldest row.
- A database that predates Alembic was stamped at the baseline and may lack baseline indexes
  (`ix_callrecord_call_ref` is the known case, rebuilt by `0072`). Check `pg_indexes` before
  relying on one.
- A covering index on `callrecord` for reports was rejected: it would copy wide string columns and
  slow every audit insert. Reports read rollups instead.

**Rollups** (`EndpointDayStat`, `ApiKeySpendDay`, `CallUsageDay`) walk their source by primary key
or a `(created_at, id)` watermark and move counts and cursor in one transaction. The cursor lock is
a no-op UPDATE, because SQLite ignores `SELECT ... FOR UPDATE`. A new cursor deletes old day rows in
the transaction that creates it, so a rebuild never adds to a previous history. Call usage has no
recount: audit is lossy by design. A report reads folded rows and the rows after the cursor in one
statement, so both halves share a snapshot. With no cursor row (the worker never ran, as on a small
self-hosted registry) reports aggregate `callrecord` live; with a cursor still catching up they say
so (`ready: false`) instead of guessing. Team deletion clears a team's rollup rows.

**Columns that carry contracts:**

- `kind`: `call`, `local_run`, or `async_poll` (an authorized free status read, excluded from
  `/calls` before pagination).
- `refused_by`: a treg refusal's mechanism; null for an upstream answer. `endpoint_stats` excludes
  refused rows.
- `call_ref` is the `X-Treg-Call-Id` and the ledger's `call_id` on a metered call: one value joins
  audit, money and the builder's records.
- `upstream_ms`: from sending the request until the answer is read (or headers arrive, when it
  streams on); excludes token refresh, archive lookup, retry sleeps and treg's work.
- `verdict`: one word from the contract's closed list, never content ([catalog](catalog.md)).
- `archive_key_hash` / `archive_content_hash`: the link for `GET /calls/{id}/result`
  ([archive](archive.md)).

**Failure evidence.** `error_request` / `error_response` hold redacted, truncated failure evidence
on every tier; `/calls` never fetches them, `GET /admin/errors` reads them, and
`application/evidence_retention.py` (the `treg-worker admin purge-evidence` cron) blanks them past
retention. Purging never runs on a request. Captured provider headers use an allowlist. Unmetered
uploads are buffered for evidence only with a declared `Content-Length` up to 64 KiB; a failed
streaming response keeps at most the first 8 KiB while all bytes reach the caller.

Redaction in `application.call.evidence` is security-sensitive and order matters:

1. Render every injected credential, including OAuth/secret-file JSON, sensitive leaf strings,
   binding-selected fields and Bearer forms. Preserve non-secret diagnosis fields.
2. Mask exact values and encoded forms: percent escapes in either case, form encoding,
   JSON escaping, and decoded Basic-auth credentials plus their username/password halves.
3. Apply pattern masking, then re-scan a percent-decoded, JSON-unescaped, lowercased copy.
   A rendering failure or surviving secret replaces the whole snippet.
4. Truncate only after masking, so a partial credential cannot escape exact matching.

## `IdempotentCall`

A caller-scoped replay cache keyed by `(membership_id, key)`. `delete_membership` removes it
explicitly and the FK cascades as a backstop, so token revocation never 500s on a cached answer.

- A `pending` row is a lease owned by one call's `call_ref`: the owner renews `created_at` every
  `IDEMPOTENCY_LEASE_RENEW_S`, and every store, release and renewal is fenced on `call_ref`.
- A lease older than `IDEMPOTENCY_STALE_PENDING_S` with no open hold or pending async task under
  its `call_ref` (and children) is closed by compare-and-swap with a stored terminal 410
  (`idempotency_response_lost` with the charge when the ledger shows one, else
  `idempotency_outcome_unknown`). The key never runs again: a lapsed lease does not prove its
  owner stopped. Rows without a `call_ref` answer 409 until they expire.
- An answer over `archive_max_body_bytes` is not stored; a terminal 410
  `idempotency_response_too_large` is kept instead.
- The per-call expired-label sweep deletes at most `IDEMPOTENCY_SWEEP_BATCH` rows with
  `FOR UPDATE SKIP LOCKED`, so a member's concurrent calls never queue on each other.

## Caller tags (`X-Treg-Meta`)

`TagSpend` is written only by `domain/money` inside the money transaction; `TagBudget` rows are
auto-created on first sighting and bound tag cardinality. `RunRecord`, `AsyncTaskRecord` and
`AsyncResourceRecord` carry nullable `tags` snapshots; no historical ownership is inferred, so NULL
snapshots are invisible to pinned readers. The reserve entry's `LedgerEntry.meta.tags` freezes
attribution, allowing scoped ledger-only reads after audit loss. Design: [money](money.md).

## OAuth (the MCP authorization server)

`OAuthCode` and `OAuthRefresh` are in `ORG_SCOPED_MODELS`; `OAuthGrant` is cleared explicitly by
`cascade_delete_org` because its FK is named `current_org_id`. The cascade revokes the union of
families that name the deleted team through current authority or any historical
`OAuthRefresh.org_id`: deleting only a retired provenance row would erase the replay evidence
while leaving live descendants usable. `OAuthRefresh.org_id` is immutable issue provenance; moving
a family updates only `OAuthGrant.current_org_id`. A used code is deleted, not flagged. See
[mcp-oauth](mcp-oauth.md).

## `Referral`

Two UNIQUE columns arbitrate, not application code: `referred_org_id` (an org is referred once)
and `qualifying_payment_intent` (one payment funds one qualification). Every terminal status is
kept, never deleted. `User.referral_code` is on the user, not the org, because anyone may create
unlimited orgs. `Referral.card_fingerprint` is Stripe's opaque per-card id, not card data, and
lives only here so `Org` keeps its no-card-data posture.

## Async DB (`src/treg/infra/db.py`)

Three async engines against one database (`session_maker` for the API, `admin_session_maker`,
`background_session_maker` for audit, archive writes and workers) form a bulkhead; sizes and
timeouts are in [deploy](../ops/deploy.md), Database pools. On SQLite all three alias one engine.
The request session commits before the relay ([proxy-model](proxy-model.md)).

- **Timestamps are naive UTC.** Columns are `TIMESTAMP WITHOUT TIME ZONE` and asyncpg rejects
  tz-aware values, including query parameters compared with them. Use `timeutil.utcnow_naive` and
  `timeutil.as_naive`.
- `verify_db()` is the read-only lifespan and worker guard: it refuses a missing Fernet key on a
  real database, requires the stamp at head, refuses a known older revision, and warns but serves
  on an unknown newer one so an additive release can roll back.
- `dispose_engine()` closes pooled connections before a maintenance event loop exits, so a later
  loop never inherits connections bound to a closed one.
- `reset_db()` is test-only: it deletes every row, rewinds Postgres sequences and stamps head,
  rebuilding the schema only when its column-and-index fingerprint changed.
- `TREG_READ_DATABASE_URL` adds `read_session_maker`, read-only by default (PostgreSQL read-only
  transactions, SQLite `PRAGMA query_only`); these guard accidents, not deliberate SQL. Failures
  never fall back to the primary. Authorization always stays on the primary. Replication and lag
  are the operator's job ([deploy](../ops/deploy.md), Optional read replica).

## Alembic execution and the adoption floor

All production schema execution goes through `maintenance._upgrade_schema`. An empty or stamped
database runs `alembic upgrade head`; a non-empty unstamped database is refused without inspection
(it must pass through adoption release 0.14.x); an unknown newer revision is refused. Scripts ship
inside the wheel; Alembic runs through `asyncio.to_thread` because `env.py` owns its own
`asyncio.run`. On Postgres `env.py` sets `lock_timeout = 5s` and commits each revision separately,
so `maintenance`'s bounded retry resumes at the revision that timed out.

- The drift guard upgrades to head, autogenerates against `SQLModel.metadata` and requires an
  empty diff (SQLite in the suite, Postgres in CI).
- `test_alembic_expand_safety.py` permits a closed set of additive operations in `upgrade()`.
  Anything else must set module-level `contract = True` and name its rollback floor in the
  docstring.
- Build indexes on large tables in a separate revision, `CONCURRENTLY` on Postgres, never while
  holding column DDL locks; keep a valid existing index, rebuild INVALID debris from an
  interrupted build, and restore the normal timeouts afterwards (`0036`, `0072` follow this).

## Audit writer (`audit.py`)

`record_call` and `record_run` schedule best-effort writes on `background_session_maker`; they
never delay or fail a proxied response. A loop-bound semaphore limits active writes and
`_MAX_PENDING` sheds excess rows. Write failures and shed rows log at ERROR. `drain` loops until the
queue is empty, including tasks added during shutdown. `_known_fields` filters telemetry against
model fields so one new field cannot discard a row; schema and telemetry changes still land
together. Audit is lossy analytics and diagnostics: money must use the synchronous ledger.

## Product analytics writer (`analytics.py`)

The same lossy discipline, with PostHog's `/batch/` as the sink. `capture()` is synchronous and
never raises (call sites sit inside the Stripe webhook, where an exception would 500 and retry an
already-credited payment); it queues up to `_MAX_PENDING` and one flusher micro-batches. An empty
`posthog_key` turns the module off. Every event carries `build` and `archive_config` (a digest of
the archive settings that change what a call does) so an analysis can be bounded to one version or
configuration. It drains last in the lifespan, after `audit.drain()` and `archive.drain()`, because
those report their losses into it. Server events never carry URLs, search data, bodies or amounts
beyond their declared fields; `funnel_surface` accepts only fixed names.

**Faults.** `capture_fault` sends `$exception` with a fixed `treg-server` identity, the exception
class and at most 500 characters of its string, with URL query strings replaced by `?[redacted]`
before truncation; never frames, locals, bodies or user identity. `FaultCaptureHandler` mirrors
ERROR+ records, ignores the `treg.analytics` tree, and is installed on root and directly on
`uvicorn.error` (which does not propagate to root). Repeats roll up per `(fault type, site)` window:
the first is reported at once and the count is released on the flusher's timer and in `drain()`,
never on the next occurrence. A window's payload is fixed at construction because PostHog
fingerprints on type plus value; `sum(fault_occurrences)` is the true total. Cost is bounded by key
cardinality, not a global rate, so one loud key cannot silence others.

**Losing data is ERROR, not WARNING.** `FaultCaptureHandler` starts at ERROR, so a loss logged
below it is invisible to alerting. Degradations that cost nothing (an archive lookup falling back
to a live call) stay at WARNING.

**Money diagnostics** (`infra.money_timing`, `money_admission_reporting`, `infra.kv` lease
diagnostics, `infra.money_trace` and its runner) are bounded, per-process, best-effort summaries.
Their contract for anyone changing them: they issue no SQL, add no awaits to money operations,
never change a transaction boundary or swallow an exception or cancellation, and carry no SQL text,
parameters, amounts, exception messages, keys or tokens. Org and call ids appear only in bounded
slow-operation logs, never as aggregate dimensions. A backend PID is reusable, so join trace records
by `app_txn_id` and time interval. PostHog is a lossy diagnostic sink, never evidence that a charge
committed; short-lived workers are verified from their local exit summary. The quota fail-open
summary is described in [org-endpoint quotas](org-endpoint-quota.md#store-contract).

## `CapabilityPin`

A gate, not a hint: a catalog call to a different provider of a pinned capability is refused in
`_resolve_marketplace_call` before anything is reserved, with `use_endpoint` in the 403. Both halves
are validated against the catalog when the pin is set. See the model docstring and
[catalog](catalog.md).
