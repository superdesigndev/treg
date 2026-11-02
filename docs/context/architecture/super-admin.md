---
title: Super-admin - cross-tenant read + control
status: shipped
sources:
  - src/treg/api.py
  - src/treg/routers/admin.py
  - src/treg/application/evidence_retention.py
  - src/treg/domain/identity/access.py
  - src/treg/config.py
related:
  - architecture/multi-tenancy.md
  - interface/cli.md
---

# Super-admin

Everything else is org-scoped; super-admin is the one capability that sees across orgs, deliberately
separate from org roles.

## Authorization

`require_superadmin` (`domain.identity.access`) accepts either the env `TREG_ADMIN_TOKEN`
(`hmac.compare_digest`, principal `"env-admin"`) or a token whose user has `is_superadmin` and is not
suspended (principal = that email); otherwise 403. The env token bootstraps; `POST
/admin/users/{id}/superadmin` grants named users.

The gate always checks the primary (admin pool, `get_admin_session`), so replication lag never delays
suspension or revocation, and commits before returning to release that connection. Read-only reports
may then use `get_admin_read_session` (the read database, else the admin pool); a reader failure never
retries on the primary. No admin path uses the API pool (`ops/deploy.md`).

`require_member` returns 403 for a suspended user or org; super-admin routes are unaffected.

## Rules the endpoints keep

Routes live in `routers.admin` (plus `GET /admin/feedback` and `/admin/reviews`, see
[feedback](feedback.md)).

- **Failure evidence** (`admin_errors`) returns `CallRecord.error_request`/`error_response`, the
  redacted capture of customer request content ([data-model](data-model.md)). It is super-admin, not
  org-admin, for that reason, and `GET /calls` deliberately neither exposes nor fetches those columns.
  Past `ERROR_EVIDENCE_TTL_DAYS` a row is listed as `expired` even before `treg-worker admin
  purge-evidence` (`application/evidence_retention.py`) blanks it.
- **Reconciliation** (`admin_reconcile_*`, `src/treg/reconcile.py`) aggregates platform-tier spend
  across orgs, hence super-admin; query-time, read-only, no scheduler.
- **Last-superadmin floor:** demoting, suspending or deleting the last active super-admin user is 409;
  the env token bypasses the floor so the platform can always recover.
- **Deleting a user** removes memberships, deletes any org left with no members via
  `cascade_delete_org` (`domain/governance/teams.py`, shared with the owner's own delete) and promotes a
  survivor to owner in any org left without one.
- **Org credit** (`POST /admin/orgs/{org_id}/credit`, HTTP twin of `scripts/manual_grant.py`) grants
  through `money.grant()` with `kind="promotional"` so it burns before purchased balance; `ref` is the
  idempotency key (duplicate for the same org is 409).

## CLI

`treg admin ...` ([cli](../interface/cli.md)) sends the saved `admin_token` if present, else the
active org token, which works for an `is_superadmin` user.
