---
title: Multi-tenancy - orgs, memberships, invites, per-org scoping
status: shipped
sources:
  - src/treg/domain/governance/access.py
  - src/treg/alembic/versions/0042_pinned_read_scope.py
  - tests/test_pinned_read_scope.py
  - src/treg/models.py
  - src/treg/api.py
  - src/treg/caller_metadata.py
  - src/treg/application/auth.py
  - src/treg/application/asynctasks.py
  - src/treg/application/call/resolve.py
  - src/treg/application/provider_resources.py
  - src/treg/domain/provider_resources.py
  - src/treg/routers/provider_resources.py
  - src/treg/alembic/versions/0043_provider_resources.py
  - src/treg/application/signup.py
  - src/treg/domain/governance/budgets.py
  - src/treg/domain/governance/publicdemo.py
  - src/treg/domain/governance/teams.py
  - src/treg/domain/governance/usage.py
  - src/treg/domain/identity/access.py
  - src/treg/domain/identity/api_keys.py
  - src/treg/domain/identity/session.py
  - src/treg/application/enterprise_sso.py
  - src/treg/alembic/versions/0079_enterprise_sso.py
  - src/treg/alembic/versions/0080_enterprise_sso_audit.py
  - src/treg/alembic/versions/0081_sso_manual_block.py
  - src/treg/alembic/versions/0082_oauth_sso_provenance.py
  - src/treg/alembic/versions/0083_enterprise_sso_safety.py
  - src/treg/alembic/versions/0084_enterprise_workos_cleanup.py
  - src/treg/alembic/versions/0085_enterprise_sso_blocks.py
  - src/treg/worker.py
  - src/treg/routers/enterprise_sso.py
  - src/treg/infra/workos.py
  - tests/test_enterprise_sso.py
  - src/treg/domain/identity/promotions.py
  - tests/test_team_limit.py
  - tests/test_auth.py
  - tests/test_token_revocation.py
  - src/treg/routers/auth.py
  - src/treg/infra/turnstile.py
  - src/treg/client_ip.py
  - tests/test_client_ip.py
  - src/treg/web/captcha.js
  - tests/test_turnstile.py
  - frontend/tests/captcha.test.ts
  - src/treg/routers/orgs.py
  - src/treg/routers/resources.py
  - src/treg/domain/tools/bundles.py
  - src/treg/infra/db.py
  - src/treg/alembic/versions/0017_async_task_record.py
  - src/treg/alembic/versions/0018_async_resource_ownership.py
  - tests/test_asynctasks.py
related:
  - architecture/data-model.md
  - architecture/proxy-model.md
  - interface/api.md
---

# Multi-tenancy (orgs)

An **Org** (team) owns resources, a **User** is a global identity, and a **Membership** links them
with a role (owner > admin > member > viewer). A credential resolves to one membership, so every list,
create, mutation and the proxy are scoped to the caller's org. Every list filters by `caller.org_id`,
every create stamps `org_id` and `owner`, and `_resolve_call` scopes both the named lookup and the
host passthrough to the org ([proxy-model](proxy-model.md)). Health runs take an `org_id` filter so
`/health/run` never leaks other orgs' credentials.

## The model

- **`Org.public_demo`** marks a team whose member token is published: non-admin members are locked to
  `/call` plus reads and may never act as a user (`require_member` / `require_identity`).
- **`ApiKey`** resolves a credential to a membership; the membership stays the only source of role,
  tool and project access, local-run permission, daily cap and billing identity, so more keys never
  mean more quota. A known disabled or revoked key never falls back to `Membership.token_hash`
  (legacy, left empty for new memberships). A signed Default token carries the row's team-local
  generation; rotation increments it, invalidating the prior token without touching other teams.
  After membership removal the detached, revoked Default row keeps the old token unauthorized.
- **`Invite`** is attached to an email: redeem the one-time code, or prove that email through any
  identity door and accept code-free. `email_token_hash` is the inbox-only second secret in the
  emailed link and may sign the invitee in (`/auth/invite-signin`, one-time); the admin-visible code
  never can. The invite carries the access (`tool_access`, `project_access`, `local_run_enabled`)
  copied onto the membership at both accept doors.
- **`Project`** is an optional label and ACL scope, not a namespace: `Tool.name` stays unique per
  `(org_id, name)`, `Tool.project_id` NULL means org-wide, and secrets stay org-level because one
  credential legitimately backs tools in several projects.
- **Machine identities** are users on an unroutable domain: the published demo identity
  (`PUBLIC_DEMO_DOMAIN`) and agents (`AGENT_DOMAIN`). They act only by token; `_is_machine_email`
  keeps them out of every login door and `require_identity`. An agent is just a membership, which is
  what makes cap, ACL and audit per-agent. (`agents.py` is the unrelated skill-directory table.)
- [Enrich Arena](../interface/enrich-arena.md) runs require both the creating user and the active
  team to match; team membership alone does not expose another member's results.
- `OrgFlowControlVersion.org_id` intentionally has no foreign key; management writes must validate Org
  existence and coordinate with deletion ([configuration storage](data-model.md#org-flow-control-configuration-storage)).

## Tokens

`domain.identity.session`: sessions and identity tokens share one HMAC key but carry a signed
audience (`aud=session`, 7-day `exp`; `aud=identity`), and each reader rejects the other audience.
`token_version` revokes either family. Signed human credentials declare `scp`: `scp=team` is a
Default key whose `org` is authoritative (a conflicting `X-Treg-Org` is rejected, never charged to
another team); `scp=bootstrap` is the 7-day org-less login credential that may list, create and join
teams but is rejected by `require_member` even with `X-Treg-Org`.

Legacy tokens predate `aud`: an `org` claim marks a team-pinned copied key, usable past its former
`exp`; an untyped no-`exp` key is identity-only; an untyped org-less token with `exp` is
indistinguishable from a browser session and works only until that timestamp.

`require_member` checks managed key state before the legacy hash fallback and returns 401 for a
missing, disabled or revoked key. Last-used metadata is a throttled background write after the
dependency commits; it never extends the auth transaction.

## Access control

- **Roles.** Admin and owner manage any resource in the org; a member only what it created
  (`resource.owner == caller.email`). Another org's resource is 404, a failed role gate 403. A viewer
  may call and list only (`_require_can_register`).
- **Two ACL axes, ANDed** (`_tool_usable` = `_tool_allowed` and `_project_allowed`). Both are
  NULL-means-everything, and the owner is exempt from both. An all-selected list collapses back to
  NULL so the member keeps getting new tools; a customized list is an explicit allow-list that does
  not. `project_access` stores ids, not slugs, so the hot path is a set test and a rename cannot strand
  it. `project_access=[X]` with `tool_access=NULL` means every tool in X, including later ones.
  Deleting a project frees its tools to org-wide and removes its id from every member's scope, storing
  an emptied list as `[]`, never NULL: NULL would grant every other project's tools.
- `_require_local_run` gates the local tier on `local_run_enabled`. Server-side CLI execution is
  disabled for every caller, owners included.
- ACL refusals originate as `AccessPolicyError` and every HTTP surface translates them to the same 403.

### Agents

`/orgs/{id}/agents` (admin+) mints a machine member; re-POSTing a name rotates its token. Invariants:

1. An agent token can never act as a user (`create_org` uses `require_identity`, which refuses machine
   emails); otherwise an agent could create an org it owns and escape every limit.
2. An agent can never be an owner (`create_agent` and `set_member_role`), for the same exemption.
3. The address is org-scoped (`agent-{org.slug}-{name}@...`) so two orgs' `deploy` agents never share
   a `User`; agents are looked up by org and domain, and `_agent_name` strips the current or previous
   slug so a rename orphans nothing.
4. A rotate replaces the token, never the limits: `create_agent` writes only fields the caller sent
   (`model_fields_set`), because the dashboard's Rotate sends only name, role and cap.

**Observed agents** (`GET /orgs/{id}/agents/observed`) aggregate the CLI-reported `X-Treg-Client`
runtime per member. This is attribution, never authentication: anything holding a token can claim any
name, so nothing gates on it. Scoping a detected agent means minting it a token.

## Sign-in doors

- `application.signup.find_or_create_user` is the shared registration choke point for every identity
  door; it creates the user only, so a new user has zero teams and must create one. `create_org`
  therefore uses `require_identity`, not `require_member`. `create_org` stamps ad attribution
  ([ads-conversions](ads-conversions.md)). `accept_invite` joins a new invitee to the invited team
  only.
- `find_or_create_user` reports ids it actually inserted, so `signup_completed` fires only for new
  accounts and only after the door's commit.
- **Email-domain blocklist.** `TREG_BLOCKED_EMAIL_DOMAINS` is the only list (unset blocks nothing) so a
  new domain is a config edit, not a deploy. Rules, each because the obvious version is wrong: match
  the domain only, never the address; walk parent domains by whole labels, never the bare TLD, or
  `<random>.<listed>` bypasses it; drop dotless entries so a typed `com` cannot refuse the world;
  refuse at sign-in as well as sign-up. `signup.blocked_email(email, door)` decides, logs
  `event=signup_blocked_domain` (the only detection, since the refusal reveals nothing) and fails open
  on a classifier error. Doors: `start_email_login`, `find_or_create_user`, `create_org`, and `POST /invites/accept` (which builds a `User` directly). Every refusal is the same
  403 as a machine identity, so the caller learns nothing about the list. Live sessions keep working
  until suspension.
- **Client address.** Every per-IP limit keys on `client_ip.of(request)`: `TREG_CLIENT_IP_HEADER`
  when set and valid, else the `X-Forwarded-For` entry `TREG_TRUSTED_PROXY_HOPS` from the right, else
  the socket peer. Never the first `X-Forwarded-For` entry, which is the client's own claim. MCP tools
  call the API in process and forward the resolved address (`client_ip.forwarding_headers`) so MCP
  callers keep their own buckets.
- **Turnstile on email start**, the only door minting an inbox proof from a bare HTTP call. Off unless
  site key and secret are set. Every page posting to `/auth/email/start` loads `/captcha.js` and sends
  `captcha_token` (a test enforces it); a script that cannot get a token sends `!blocked`, `!pending`
  or `!error`, never forwarded to Cloudflare. `infra/turnstile.py` only classifies (`pass`, `missing`,
  `invalid`, `unavailable`) and never raises; a rejected secret is `unavailable`, not every caller a
  bot. The rate window runs and commits before the check so junk tokens cannot drive unbounded
  siteverify calls and no connection waits on Cloudflare. `TREG_TURNSTILE_MODE=observe` (default) only
  logs; `enforce` refuses `missing`/`invalid` with 400 and lets `unavailable` through, but a new user
  created after a non-`pass` gets `signup_promo_available=false`. Captcha refusals carry
  `X-Treg-Captcha: required` so `treg login --email` falls back to the browser handshake.
  `TREG_FIXED_LOGIN_CODES` accounts skip the check.
- **Turnstile on social sign-in**, because a provider proves an identity, not a person, and fresh
  GitHub or Google accounts are cheap. The check runs where credit is decided: after the provider
  callback, and only for an address no verified user holds yet. The callback parks the proven login
  (encrypted, `HANDSHAKE_TTL`) under a nonce in the `treg_social_check` cookie and sends the browser
  to `GET /auth/social/check`, which runs `social_start` and posts its token back. `POST` takes the
  login once, classifies the token under a per-IP window (over it the token is not sent), and signs
  in. Nothing is refused: in `enforce` a check that is not `pass` creates the user without credit,
  or, for an unverified user this proof verifies first (an invitation-code member), withdraws its claim
  (`find_or_create_user`, shared with the email door's outage case). Verified users sign in
  without the check. Each first social proof logs `event=social_signup` with the outcome and, for
  GitHub, the account age in days.

## Membership and team lifecycle

- **Owned-team limit:** at most `MAX_OWNED_TEAMS` current owner memberships (demo and suspended teams
  count). `require_owned_team_slot` locks the user (`lock_user`) through the write and commit; creation,
  demo creation and owner promotion share it. It caps current ownership, not creation rate.
- **Owner changes:** `set_member_role` is owner-only and an admin may not issue an `admin` invite; a
  last-owner guard blocks demoting or leaving as sole owner (transfer = promote, then step down). Owners
  cannot be removed. `admin_delete_user` promotes the earliest survivor when it removes a sole owner.
- **`delete_membership`** (remove, leave, revoke agent) deletes that caller's `DenyRule`s and
  `IdempotentCall` rows; `admin_delete_user` sweeps every org's rules for the user. Org-wide rules are
  untouched. SQLite does not enforce FKs, so the suite alone cannot catch a dangling row here.
- **`cascade_delete_org`** with `ORG_SCOPED_MODELS` (`domain/governance/teams.py`) is the only team
  deletion path: owner delete, admin force-delete, reapers and demo reset all use it, and
  `test_org_delete_clears_EVERY_org_scoped_table` refuses any table with `org_id` missing from the list
  or a private copy. One named exception: it first deletes every `HubRun` whose `caller_org_id` is the
  team, because a run belongs to its caller, and a maker's deletion leaves callers' history intact.
  Workbench `TableDoc` rows go before their `TeamDatabase` ([workbench-storage](workbench-storage.md)).
- **Invites** are one-time and expire after `INVITE_TTL_DAYS` (410). Inviting an existing member is
  409; a new invite supersedes the prior pending one for that email. Suspended users and orgs can
  neither view nor accept.
- **Email** is case-insensitive (`_norm_email`) at every door and invite comparison.
- **Slug resolution:** `X-Treg-Org` resolves by slug first (an all-digit slug must not be read as an
  id), then `previous_slug`, then id. A rename moves the old slug to `previous_slug` instead of
  revoking every copied key; it still resolves and no other team may take it. One alias only. `sbx-`
  is reserved.
- **Reserved names:** `teams.reserved_reason` refuses a name or slug that reads as treg, "official",
  or a catalog provider or platform (a hub tool id is `<team slug>.<name>`), matching how text reads
  (NFKC, lookalike letters, leetspeak, separators stripped). Checked at `create_org` and `rename_org`; a superadmin may bypass.
- **Uniqueness races** on membership or slug return 409, never 500.

## Enterprise SSO

WorkOS handles the customer's SAML or OIDC connection; treg keeps its own users, memberships, roles,
sessions and keys. Rules a change must keep:

- A WorkOS code exchange is accepted only when the returned organization is this team's stored WorkOS
  organization and the connection is the one previously used. Email domain alone never grants access;
  discovery only routes to a team link, and membership comes only from the state-checked callback.
- A profile links by stable profile and IdP ids; later email changes need review, never silent
  reassignment. A new account needs an email on a verified organization domain; an existing global
  account needs a concurrent normal treg session for the same verified user before first linking.
- After enforcement, non-owners need a signed SSO session for this team; an SSO session or derived
  token reaches only that team. The owner keeps a regular-login recovery path. A regular session
  cannot mint a Default key for an enforced team. Earlier non-owner bearer keys and pre-enforcement
  OAuth grants stop authorizing the team. CLI pairing, OAuth issuance, exchange, refresh and use all
  recheck SSO scope.
- Directory Sync: once a directory is connected, sign-in requires a matching active roster user;
  inactive or deleted users lose membership and keys. Only directories in the `linked` state count,
  so a disconnected directory cannot authorize anyone. Groups never map to roles. WorkOS event ids
  make redelivery harmless, and a worker can reconcile the full roster. `EnterpriseSSOBlock` (a manual
  removal) beats active directory records until the owner restores access, across replacement
  connections.
- Access changes and their `EnterpriseSSOAudit` rows commit together.
- `past_due` keeps SSO active while Stripe retries. When the subscription ends, enforcement lifts and
  only SSO-created memberships are removed; the cleanup worker deletes the WorkOS directory and
  connection and records completion before a new checkout can start.

## Caller tags and pinned read scopes

`X-Treg-Meta` tags are attribution labels an unpinned caller chooses freely. A membership's
`pinned_tags` are enforced: a conflicting header is 403. `pinned_tag_predicates` requires every pinned
key/value and is applied before pagination or loading archive bodies on `/calls`, `/calls/{id}/result`,
`/calls/{call_ref}`, `/runs`, `/activity`, `POST /reviews` and feedback reads; a foreign or
unattributed id is 404 like an unknown one. Matching is by pin, not current membership: identities
sharing a pin share the view, and changing a pin does not relabel earlier records. Untagged historical
rows are readable by unpinned members only; attribution is never inferred from current membership.

Every audit writer stores the pin; async task and resource records snapshot submission tags in their
own transaction, and resources found later inherit the task's tags. The reserve ledger entry freezes
tags in `meta.tags`, so `/calls/{call_ref}` can serve ledger-only history when audit was shed. treg's
replay key includes the full pin; on the shared key the forwarded idempotency label is partitioned by
it too (unpinned and BYOK forwarding unchanged). Public media URLs stay bearer-by-possession. A
`TagBudget` is a ceiling on the shared balance, never a sub-account.

## Shared-provider objects are org-scoped

- **Async ids.** A platform-key poll or result fetch must resolve its id through an org-owned
  `AsyncTaskRecord` or `AsyncResourceRecord` before the upstream is contacted. Exactly one value is
  read (query, or the declared `in: body` parameter); in a body array every element must name that
  same id, so an owned first task cannot smuggle another team's id. A `produces` path may use `*`
  to own every task of one submission.
- **Durable resources.** A platform-key managed-resource call verifies every id against
  `ProviderResource` first. A `use` tool may declare a read-only public lookup for ids absent from the
  table, checked with no DB connection held; an id owned by another org or tombstoned is denied
  locally and never looked up. Every member may manage the team's objects. The dashboard inventory
  requests `source=platform` so a BYOK account never widens it.
- BYOK calls keep access to the team's own provider account; these scopes never partition it.

## Released CLI compatibility

Released CLIs that would save a bootstrap token or keep the previous team's token after switching are
stopped with 426 by `routers.auth_helpers.require_managed_cli` before email credentials are issued, a
team is created or an invite consumed. Current CLIs send `X-Treg-Key-Protocol: 1`. The legacy hint
(`python-httpx/` User-Agent plus `ngrok-skip-browser-warning: 1` without the marker) is a
compatibility check, not an authorization boundary: forging or omitting it never relaxes token
restrictions. `test_released_cli_compat` checks the refusal preserves config bytes.
