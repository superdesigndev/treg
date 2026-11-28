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
  - src/treg/alembic/versions/0127_callrecord_served_call_ref.py
  - tests/test_callrecord_served_call_ref_migration.py
  - src/treg/alembic/versions/0015_idempotentcall_membership_cascade.py
  - src/treg/alembic/versions/0053_idempotentcall_membership_expires_index.py
  - src/treg/alembic/versions/0099_idempotentcall_archive_link.py
  - src/treg/alembic/versions/0100_idempotentcall_archive_live_index.py
  - src/treg/alembic/versions/0122_archive_retention_cursor.py
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
  - src/treg/alembic/versions/0112_callrecord_outcome.py
  - src/treg/alembic/versions/0116_stamped_baseline_indexes.py
  - tests/test_stamped_baseline_indexes_migration.py
  - src/treg/alembic/versions/0117_callrecord_drop_unused_indexes.py
  - src/treg/alembic/versions/0070_api_key_spend_rollup.py
  - src/treg/alembic/versions/0072_callrecord_call_ref_index.py
  - src/treg/alembic/versions/0073_org_flow_control_version.py
  - src/treg/alembic/versions/0140_call_record_id_references_bigint.py
  - src/treg/alembic/versions/0141_call_record_partitioned_table.py
  - src/treg/alembic/versions/0142_call_record_swap.py
  - src/treg/application/call_record_partitions.py
  - tests/test_call_record_partitions.py
  - tests/test_call_record_swap.py
  - tests/test_org_flow_control_schema.py
  - src/treg/alembic/versions/0096_route_rule_version.py
  - src/treg/alembic/versions/0097_referral_affiliate.py
  - tests/test_referral_affiliate_schema.py
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
| `ArchiveKey`, `ArchiveSnapshot`, `ArchiveKeyOrg`, `ArchiveEndpointStat` | `archive.py`; deletes through `governance.teams` for erasure and row retention ([archive](archive.md)) |
| `ArchiveRetentionCursor` | `application/archive_retention.py` (`treg-worker archive retain`) |
| `ArchiveObjectPending` | `application/archive_retention.py` (`treg-worker archive retain`); the recorder only reads it `FOR SHARE` |
| `Media` | `application.media` ([media](media.md)) |
| `Feedback` | `domain.feedback` inserts, `application.feedback` commits ([feedback](feedback.md)) |
| `FeedbackHandling`, `FeedbackHandlingEvent` | the private admin service only |
| `Referral`, `ReferralAffiliateEvent` | `domain/referrals.py`; its money goes through `ledger.grant` |

Rollups and analytics tables are optional read models: they never determine a money transaction.

- **`Org.first_call_at`** is set once by a guarded UPDATE in the call path, never derived from
  `CallRecord`, because audit sheds rows exactly when traffic is highest. The `ad_*` and `utm_*`
  columns are set once at signup and never overwritten ([ads-conversions](ads-conversions.md)).
- **`ApiKey.last_used_at`** is throttled, best-effort display metadata, never written in the
  authentication transaction. Revoked and rotated rows stay for audit; Activity rows keep their key
  snapshot after rotation, revoke or membership removal.
- **`Invite.email_token_hash`** keys the emailed link, which only prefills the login; neither it
  nor the admin-visible code is an auth factor.
- **`Secret`**: expiry (`expires_at`, `last_refresh_at`, `last_error`) is a separate axis from
  `health_status`; a non-refreshable token stays healthy until it silently dies.
  `authorization_method` selects a grant's method; never infer it from the token's shape.
  `refreshable` is recorded with every oauth write so lists never open the credential
  ([auth-secrets](auth-secrets.md), Encryption).
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
- **`CallTask`** names the provider task of every root async answer, on any tier, by the call id the
  caller holds (a routed pending answer by its parent's). It is no money record; `/tasks/` reads
  it by primary key ([tasks](tasks.md)).
- **`EndpointStatDirty`**: marker writes never commit on their own; audit marks the day in the
  same transaction as its terminal update, and the worker acknowledges only an unchanged revision.
- **`Ephemeral`** is the DB home for state that must survive restarts and stay correct across
  instances (OTPs, rate windows, CLI pairing); see [api](../interface/api.md), "CLI pairing".
  `rate_check` reads and rewrites a key's hit list without a lock unless called with `atomic=True`
  (agent payments), which locks the row through an upsert until the commit.
  `_prune_handshakes` deletes at most 100 expired pairing rows per CLI start, so physical removal
  can lag while reads still enforce expiry.
- **`AdConversion`**: unique `(org_id, action)` is the sole idempotency mechanism, not a
  check-then-insert; rows are written synchronously in the firing transaction, unlike audit.
- **`AffiliateLead`**: one per team (unique `org_id`), staged in the team-creation transaction; it is
  both the Dub lead outbox and the record that the team is affiliate-attributed
  ([affiliates](affiliates.md)). A check constraint keeps exactly one source: `click_id` (a link
  click) or `partner_code` (a typed code, with the `country` Dub requires for it). Only a
  `verified_at` row attributes; `stripe_synced_at` records that an existing Stripe customer was
  brought in line. No foreign key to `org`: team deletion removes it in code.
- **`AffiliateBonus`**: one per paid top-up of a partner-brought team (unique `payment_intent`),
  staged with the credit and decided by the affiliate worker: `outcome` records what the partner
  offer paid and why, `block_id` the granted `bonus` block ([affiliates](affiliates.md) "Partner
  offer"). No foreign key to `org`: team deletion removes it in code.
- **`AffiliateCardClaim`**: a card (Stripe's opaque `fingerprint`) that already earned a fixed
  partner offer, unique. No `org_id`: it outlives team deletion, so a deleted team never frees its
  card to earn the offer again.
- **`AffiliateClaim`**: the person a partner brought, by `promo_inbox`, staged with the lead. No
  `org_id` and no foreign key: like `SignupPromoClaim` it outlives the team and the user, so a deleted
  team never makes its creator attributable again. Not unique, so a racing second team leaves a second
  row rather than failing the signup.
- **`DenyRule`**: an empty field means any, so a rule with only `method="DELETE"` blocks every
  delete, a request that asks its upstream to run it as DELETE included ([proxy-model](proxy-model.md),
  "Authorization and deny rules"). `delete_project` sweeps rules naming the project.
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

- **`AgentPayer` / `AgentPayment`**: the payer team of one agent-payments wallet address on one
  network (primary key `(network, address)`), and each payment it presented (`proof_id` UNIQUE,
  status `credit|settling|settled|failed|expired`; an MPP zero-amount proof leaves no row). `asset`,
  `pay_to` and `expires_at` let the reconciler find the transfer on chain; `stripe_payment_intent` is
  set once a payment settled to Stripe is recorded there. `governance.teams.make_payer_team` stages
  `AgentPayer`; `application.agent_payments` is the only writer of `AgentPayment`. Team deletion
  removes both ([agent payments](agent-payments.md)).

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

## Call-path tables have no foreign key to `org`

The tables in `ORG_ROWS_WITHOUT_FOREIGN_KEY` (`domain/governance/teams.py`) keep `org_id` (indexed)
but no foreign key, one revision per table from 0130. A foreign key makes every insert take
`FOR KEY SHARE` on the team's `org` row, the row every balance write updates. Locks of different
transactions on one row are recorded as a MultiXact, a new one each time another transaction
joins, so a busy team's concurrent inserts and balance writes keep writing MultiXacts that grow
with the number of lockers. On PostgreSQL 16 the MultiXact SLRUs are a few fixed pages behind one
lock each, shared by the whole server: once they thrash, inserts of every team wait on
`MultiXactOffsetSLRU`.

Team deletion deletes these tables in its walk and once more after the team row: that DELETE waits
for every transaction that updated the row and lets none update it after, so a row a call
committed while the walk ran is taken too (`tests/test_org_delete_race_postgres.py`). A table joins
the list only when no write to it can commit after the team row is gone: each of its writers
updates the team row first, share-locks it (the allowance fold, once per fold, since a fold of
unpaid or late-queued charges may touch no balance, block or hold of the team's), claims a row the
deletion also deletes, or writes through a foreign key that still stands. `CallRecord` keeps its foreign key for that reason: the audit queue writes it
from memory after the call, holding no lock on anything of the team's, so without the constraint a
row with the caller's email, tags and error evidence could land after the team is gone.

Each drop takes ACCESS EXCLUSIVE on the child table and on `org`, bounded by the revision's own
`SET LOCAL lock_timeout`; a timeout leaves the earlier drops applied (rolling back from there:
[deploy](../ops/deploy.md), "Rolling back past a revision").

## `CallRecord` and `LedgerEntry`: indexes are throughput

`CallRecord` (audit) and `LedgerEntry` (append-only, never pruned) are the largest tables.
`LedgerEntry` only grows. `CallRecord` grows until an operator turns its retention on (next
section). Every time-window question needs an index whose leading columns match the filter and
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
- A database that predates Alembic was stamped at the baseline and lacked every baseline index on a
  column the old `init_db` added by `ALTER TABLE ... ADD COLUMN`. `0072` builds
  `ix_callrecord_call_ref`; `0116` builds the ones on `org`, `invite`, `denyrule`, `secret` and
  `tool`; `0117` drops the six single-column `callrecord` indexes no query used, so every database
  matches the stamped ones. Check `pg_indexes` before relying on one.
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
- `refused_by`: a treg refusal's mechanism; null for an upstream answer (`payment` is an
  agent-payments 402 asking a keyless caller to pay or prove its wallet). `endpoint_stats` excludes
  refused rows.
- `call_ref` is the `X-Treg-Call-Id` and the ledger's `call_id` on a metered call: one value joins
  audit, money and the builder's records.
- `upstream_ms`: from sending the request until the answer is read (or headers arrive, when it
  streams on); excludes token refresh, archive lookup, retry sleeps and treg's work.
- `verdict`: one word from the contract's closed list, never content ([catalog](catalog.md)).
- `outcome`: what a v2 row's own declaration judged the answer (`hit`, `miss`, `error`,
  `not_found`); null where the status rule decides. Reliability reads it first ([catalog](catalog.md)).
- `archive_key_hash` / `archive_content_hash`: the link for `GET /calls/{id}/result`
  ([archive](archive.md)).
- `served_call_ref` (Alembic 0127): on a routed parent only, the `call_ref` of the child whose
  answer the caller received (`answered_by` in `route._run_routed`: the winning attempt, an async
  attempt's submission, an upgrade's own served child, under a merge the answer whose body is `raw`,
  not `served_by`). It names a child only when that child's body is the `raw` returned. NULL on a
  miss or a failure, when the router wrote `raw` itself (a declared-miss status, a failed async
  task, a non-JSON body), when re-serializing `raw` changes its value or that cannot be told
  (`route._delivered_as_received`: the provider's bytes read exactly, numbers as Decimal, in any
  encoding `json.loads` accepts and with no repeated key, against the delivered `raw` read back; a
  float past 15 significant digits or a dropped duplicate key fails it, and so does a body the check
  cannot read, which costs the link, never the call), on an answer over `route.SERVED_CHECK_MAX` (64
  KiB: the check parses the body again on the request path, so a larger one is served as before and
  not linked), on a pending 202 (the caller got the submission ticket, not the answer later filed
  under its ref), on an answer an overflow aggregator served (that row shares the child's ref with
  the direct attempt's, so a ref could not name one), on every non-routed row and on rows written
  before 0127, successful ones included: NULL means no usable link was recorded, never that no
  answer was served. Unindexed and never a foreign key: a reader starts from the parent by id and
  reads the child by that exact ref through `ix_callrecord_call_ref`. It is not review credit
  (`route_summary["credited"]`, [feedback](feedback.md)). `served_content_hash` (same revision) is
  the sha256 of the bytes returned as `raw`, in `archive.content_hash`'s digest: a reader takes an
  archived answer for this call only when its content hash equals it ([archive](archive.md), "The
  call-archive link").

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

## `CallRecord`: monthly partitions and a 64-bit id

On PostgreSQL `callrecord` is partitioned by month on `created_at`, and its id is `bigint`
(alembic `0140` to `0142`). Two limits of the plain table are the reason: a 32-bit id ends at
2,147,483,647, and changing its type in place rewrites the table under an ACCESS EXCLUSIVE lock;
and a plain table gives space back only through `DELETE`, which leaves the files as large as they
were. A month partition is dropped whole. SQLite keeps one plain table (its ids are 64-bit
already), as does a PostgreSQL database built from the models, and the code below is a no-op there.

**Shape.**

- The primary key is `(id, created_at)`, because a unique constraint on a partitioned table must
  include the partition column. Every id comes from one sequence, so `id` alone still names one
  row; that, not a constraint, is what the model's `primary_key` on `id` and `db.get(CallRecord, id)`
  rest on. Never insert an explicit id.
- The table has the same name, columns and index names as before, so readers did not change. Each
  index exists once per partition. A read bounded by `created_at` touches only its months. A read
  by `id`, `call_ref` or `(org_id, id)` probes that index in every partition and merges them,
  which stays cheap while each probe is bounded: keep the `LIMIT` or the id range such reads have.
- There is no foreign key to `org`: it would lock the `org` row on every audit insert. Team
  deletion removes a team's rows itself (`ORG_SCOPED_MODELS`).
- `CREATE INDEX CONCURRENTLY` does not work on a partitioned table. A new index is built
  concurrently on each partition, then attached to an index created `ON ONLY callrecord`.
- Columns that store a call record id are `bigint`: the rollup cursors (`callusagecursor`,
  `endpointstatcursor`, `shadowcursor`, `webarenacallcursor`, `arenainsightstate` (both cursors),
  `webarenaseedprogress`, `showcasecursor`) and `shadowpair.call_id`. `arenaobservation.id` is
  still `integer`; it must be widened before call record ids pass the 32-bit range.

**Partition upkeep** (`application/call_record_partitions.py`, the only writer of partitions). A
row whose month has no partition is refused, and the audit writer then drops it and logs an error,
so the coming months must exist first. `maintain()` creates the current month and the next
`MONTHS_AHEAD`; it runs at every release (`maintenance.RELEASE_TASKS`), in the
`treg-worker usage rollup` cron, and as `treg-worker admin callrecord-partitions`. A partition is
created as a plain table and attached, which takes SHARE UPDATE EXCLUSIVE on `callrecord`, so
inserts and reads do not wait; `CREATE TABLE ... PARTITION OF` would take ACCESS EXCLUSIVE. There
is no default partition for the same reason: every attach would lock and scan it, and
`DETACH ... CONCURRENTLY` refuses to run beside one. Autovacuum analyzes each partition but
never the partitioned parent, so `analyze_if_stale` (in `maintain()`) runs `ANALYZE` on the live
`callrecord` when its row estimate falls below 90 % of its partitions' (the staged table is
analyzed by the swap).

**Retention** is `TREG_CALLRECORD_RETENTION_MONTHS`, 0 by default, which keeps every month. With
`N`, `maintain()` detaches (`DETACH PARTITION ... CONCURRENTLY`) and drops each partition older
than the `N` full months before the current one. What goes with a month: its rows in Activity and
`/calls`, the link behind `GET /calls/{id}/result`, and its failure evidence. What stays: the
rollups (`CallUsageDay`, `EndpointDayStat`, `ApiKeySpendDay`) and the ledger. The readers that
rebuild from call records look back 31 days at most, and `N` = 1 always keeps more than that, so
`N` below 1 is not offered. Writing a detached month to object storage before dropping it is not
built: the place for it is between the detach and the drop in `drop_expired`.

**The swap** from the plain table. The history is copied first and the tables trade names last,
so no reader ever sees a table with part of its history, and only the rename blocks anyone:

1. `0140` widens the columns that store a call record id.
2. `0141` creates the empty partitioned table `callrecord_partitioned` beside the live one, on
   the same id sequence (changed to `bigint`), so ids continue and every cursor stays valid.
3. `treg-worker admin callrecord-backfill --days N` copies the last `N` days into it, newest
   first, in bounded transactions beside live traffic, for as long as that takes; run again, it
   resumes, and first copies what was written since. Each batch creates the month partitions its
   rows need and reads the column list again, so a column added meanwhile is copied from the next
   batch on. By default a batch is 5,000 rows followed by a 0.5 s pause (`--batch-rows`,
   `--pause-seconds`; `--max-replica-lag S` also waits while the read replica is more than `S`
   seconds behind, `pg_last_xact_replay_timestamp()`; without a replica behind the read pool it
   refuses and exits 2, since the primary's lag always reads 0), so the copy leaves the primary and its
   replicas room. The new table grows by about the size of the rows copied and the WAL by as much,
   and the legacy table keeps its space until step 5, so check disk headroom and watch replica
   lag while it runs. The first run notes when it started (`NOTE_BACKFILL_STARTED`, in a JSON
   comment on the table). One run works at a time: it holds a session advisory lock
   (`BACKFILL_LOCK_KEY`), and a second run returns `busy` and does nothing.
   - Each run first deletes the copied rows of teams that no longer exist
     (`delete_rows_of_deleted_teams`: the distinct teams walked through the `(org_id, id)` index,
     each kept only `NOT EXISTS` in `org`, deleted in committed batches with the same pause and
     replica brake between them), which closes the race with a team deleted while a batch copied
     its rows.
   - Each run last resyncs late `hit`/`verdict`/`cost_charged_micro` values (an async call's settled charge lands late too) (`_resync_late_verdicts`): a row copied
     before its verdict landed is stale, and a verdict lands at most a day after its row (an async
     task's answer is kept 24 h). A pass covers the rows created from `RESYNC_LOOK_BACK` (25 hours)
     before the previous pass began (before the first run began, while no pass has completed) up
     to the top copied id, then
     one short pass up to the top id copied meanwhile. It writes its cursor (`resync_pass`) to the
     note after each batch, so a stopped run resumes, and on completion `resynced_upto` and
     `resynced_at` (when the pass began), which the swap reads.
   - Run it once more just before the deploy. A small table skips this step: `0142` copies it
     whole.
4. `0142` refuses at once, changing nothing, when more than `_MAX_CATCH_UP_IDS` ids were written
   since the last backfill run, or more than `_MAX_RESYNC_IDS` ids lie above its `resynced_upto`,
   or its last resync did not finish. Otherwise, without a lock, it copies what was written since,
   resyncs only the rows above `resynced_upto` (noting progress after each batch, so a stopped
   upgrade resumes there), copies again what came in meanwhile and runs `ANALYZE` on the new
   table (with a 15-minute statement timeout for that statement only). Month partitions start at
   the oldest `created_at` still to copy, not the lowest id's month: ids are handed out before
   their rows commit. Then, within `_QUIET_WAIT_S`: it waits without queuing until no visible session has run
   a statement on `callrecord`, or kept a transaction open, for longer than
   `_QUIET_STATEMENT_S` (each poll first runs `pg_stat_clear_snapshot()`: inside a transaction
   PostgreSQL otherwise keeps serving the first `pg_stat_activity` it read); copies what came in
   during the wait and resyncs the rows above `resynced_upto` again; and tries the locks with a
   `_LOCK_TRY` lock timeout inside a savepoint, starting over on a timeout, so a lock request
   never queues inserts for longer than that. It refuses if the window ends first. Holding both
   locks, it copies the last rows and resyncs the late values of that range (`_RESYNC`, since a
   row the copy skips was copied before the lock), trades the table and index names, moves the sequence and
   repeats the old table's grants. Audit inserts wait for that transaction and land in the new
   table. A statement prepared before the swap whose result includes `id` is refused from then on
   (its result changed type), and behind a transaction pooler that keeps one server statement per
   query text it stays refused until that server connection closes; so the background pool reads
   and writes no `id` (see "Audit writer"). The old table stays as `callrecord_legacy`; an empty one is dropped. A late verdict on a
   row the last backfill run already resynced, landing between that run and the swap, stays in
   the legacy table only.
5. `treg-worker admin callrecord-drop-legacy` deletes the rows of teams deleted while the copy ran
   (`delete_rows_of_deleted_teams`, committed batches, never in the upgrade), drops the legacy
   table's foreign key to `org` the way the swap takes its lock (wait for quiet on `org`, then
   `_LOCK_TRY` attempts inside a savepoint, within `--window-s`, 120 s by default; run it in a quiet
   hour and again when it answers `dropped: false`), so an `org` read never
   queues behind it for long, then drops the table, so the file deletion holds no lock on `org`. Before that, the same backfill command
   copies still older history from it into `callrecord` if it is wanted.

**Columns and indexes move together.** The copy names the live table's columns and the swap
gives the staged table the live table's index names, so until the swap any migration that adds a
column or an index to `callrecord` must add it to `callrecord_partitioned` too, in the same
revision (on the partitioned table an index is created `ON ONLY` the parent, then built
concurrently per partition and attached). `test_the_staged_table_has_the_live_tables_columns` and
`test_the_staged_table_has_the_live_tables_indexes` fail until it does They build their database with every revision (`upgrade head`), so a later revision
that changes `callrecord` alone fails them; they skip once `callrecord` is partitioned.

`0142` refuses to swap a large table whose copy does not reach back 31 days (or to the first row)
and changes nothing. The reason is the readers that count "today" or rebuild a recent window from
call records: the trial allowance (`_trial_calls_today`), the spend recount, a catalog day repair
(`_repair_bucket`), a new usage or Arena cursor. On a table holding only its newest rows they
would give wrong answers without an error. Calls older than the copy leave every reader of
`callrecord` (Activity, `/calls`, `GET /calls/{id}/result`) at the swap, until the backfill copies
them back from `callrecord_legacy`. A rollback of `0142` is code only, to the build before it,
which runs on the partitioned table unchanged; its downgrade copies every row back in one
statement that blocks every audit insert. To copy before the release that carries `0142` is
deployed, apply the first two revisions alone (`alembic upgrade 0141`) and run the backfill from
a checkout of that release; older code serves unchanged on that schema.

Why a copy and not a view or two reads: a view over both tables must cast the legacy `integer` id
to `bigint`, and a cast column cannot use the legacy indexes, so every read by id would scan the
legacy table; reading both tables in code would change every reader and writer of the table. The
copy keeps one table and one read path, and the legacy table is only a source.

Before the swap, team deletion also deletes the team's rows from `callrecord_partitioned`. While
`callrecord_legacy` exists: nothing writes it; team deletion deletes the team's rows from it
(`cascade_delete_org`), which its foreign key to `org` requires; the admin total adds its row
estimate to the partitions' (the copied window counts twice until it is dropped); and dropping it takes a brief
ACCESS EXCLUSIVE lock on `org` to remove that foreign key, which `drop_legacy` bounds with a lock
timeout and retries.

## `IdempotentCall`

A caller-scoped replay cache keyed by `(membership_id, key)`. `delete_membership` removes it
explicitly and the FK cascades as a backstop, so token revocation never 500s on a cached answer.

- `request_fingerprint` hashes method, path, query and body, a JSON body as its canonical
  serialization (`_canonical_json`); a row stored with the raw-bytes fingerprint still matches
  the same bytes (`_raw_request_fingerprint`). A 409 on a live `pending` row names its `call_ref`.
- `surface` is `call` or `table`, the surface the label was taken on: a retry on the other one is
  the 422 mismatch, since `/table/` returns a reshaped answer. NULL on labels stored before 0128,
  which replay on either surface as before rather than becoming a new paid call; they expire
  within the window.
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
- `treg-worker idempotency prune` (hourly) deletes expired completed rows for every team, then
  `trim_archived_answers` drops `response_body` from a live `done` 2xx row whose call's archive answer
  `archive.bytes_on_file` confirms and whose own sha256 matches; after hashing it holds those keys `FOR SHARE SKIP
  LOCKED` and re-checks the answer in its UPDATE, so row retention cannot remove it meanwhile. The row keeps `archive_key_hash` /
  `archive_content_hash`; a replay reads the bytes with `archive.answer_bytes` after its session
  closes, hash-checked, and answers 410 `idempotency_response_lost` if they are gone, never a new run.
  On PostgreSQL only one run goes at a time (`infra.db.only_one_run`). The trim does not re-read the
  live day each run: it starts after `resume_after`, the last id up to which every row was decided,
  which the worker saves in `Ephemeral` after each committed page. A row stays undecided while it
  is a live pending claim (not expired, renewed within `IDEMPOTENCY_STALE_PENDING_S`), younger than
  `min_age_s`, its archive key was skipped by `FOR SHARE SKIP LOCKED`, or the time budget ran out
  before its update; a dead claim is passed over and kept. The walk stops at the first page whose
  rows were all claimed less than `min_age_s` ago (the claim time is `expires_at` less the window:
  a lease renewal rewrites `created_at`, never `expires_at`), and at the budget, checked before
  every update. A row passed over before its archive link existed keeps its own copy until it
  expires.

## Caller tags (`X-Treg-Meta`)

`TagSpend` and `MemberSpend` (one row per metered call of a member whose daily spend limit applies)
are written only by `domain/money` inside the money transaction; `TagBudget` rows are
auto-created on first sighting and bound tag cardinality. `RunRecord`, `AsyncTaskRecord`,
`AsyncResourceRecord` and `CallTask` carry nullable `tags` snapshots; no historical ownership is inferred, so NULL
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

`User.referral_affiliate` defaults false in both model and database; its version starts at zero.
`ReferralAffiliateEvent` is durable operation history, committed with the flag, independent of
best-effort call audit. It stores the authenticated principal, reason, old/new flag, operation UUID
and version. User id plus user creation time identifies the account incarnation; no foreign key
allows history to survive deletion without being attributed to a later SQLite account reusing its id.

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
  on an unknown newer one (all digits and above this build's head, `db._is_newer_revision`) so an
  additive release can roll back; any other unknown stamp is refused.
- `dispose_engine()` closes pooled connections before a maintenance event loop exits, so a later
  loop never inherits connections bound to a closed one.
- `reset_db()` is test-only: it deletes every row (one probe names the tables holding any, and only
  those are emptied), rewinds Postgres sequences and stamps head, rebuilding the schema only when
  its column-and-index fingerprint changed. The fingerprint is read only after some engine in the
  process ran DDL (a listener on every `Engine`, installed by the first reset, so a server never
  carries it), and only then are the pools disposed, so no connection keeps a statement prepared
  against an old table; otherwise pooled connections live on into the next test, as a server's do
  between requests. It works a few tables per transaction: Postgres's lock table is shared by every
  database on the server, and parallel test workers each locking every table at once exhaust it.
- `TREG_READ_DATABASE_URL` adds `read_session_maker`, read-only by default (PostgreSQL read-only
  transactions, SQLite `PRAGMA query_only`); these guard accidents, not deliberate SQL. Failures
  never fall back to the primary. Authorization always stays on the primary. Replication and lag
  are the operator's job ([deploy](../ops/deploy.md), Optional read replica).

## Alembic execution and the adoption floor

All production schema execution goes through `maintenance._upgrade_schema`. An empty or stamped
database runs `alembic upgrade head`; a non-empty unstamped database is refused without inspection
(it must pass through adoption release 0.14.x); an unknown newer revision is refused. Scripts ship
inside the wheel; Alembic runs through `asyncio.to_thread` because `env.py` owns its own
`asyncio.run`. On Postgres `env.py` sets `lock_timeout` from `TREG_MIGRATION_LOCK_TIMEOUT_S` (2 s) and
commits each revision separately, so `maintenance`'s bounded retry resumes at the revision that
timed out ([deploy](../ops/deploy.md), PostgreSQL lock queueing).

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
queue is empty, including tasks added during shutdown. Call records go in through `_insert`, one Core INSERT with
`inline()` that returns nothing: an ORM insert would return the new `id`, and a prepared statement
whose result changes type (the swap to a 64-bit id) is refused on every later run behind a
transaction pooler, which keeps one server statement per query text. The other models go through
the session. `_known_fields` filters telemetry against
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
by `app_txn_id` and time interval. A pool behind a connection pooler records no backend PID: the
id its client sees is the pooler's. PostHog is a lossy diagnostic sink, never evidence that a charge
committed; short-lived workers are verified from their local exit summary. The quota fail-open
summary is described in [org-endpoint quotas](org-endpoint-quota.md#store-contract).

## `CapabilityPin`

A gate, not a hint: a catalog call to a different provider of a pinned capability is refused in
`_resolve_marketplace_call` before anything is reserved, with `use_endpoint` in the 403. Both halves
are validated against the catalog when the pin is set. See the model docstring and
[catalog](catalog.md).
