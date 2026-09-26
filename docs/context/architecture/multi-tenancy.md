---
title: Multi-tenancy — orgs, memberships, invites, per-org scoping
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
  - src/treg/domain/governance/access.py
  - src/treg/domain/governance/budgets.py
  - src/treg/domain/governance/publicdemo.py
  - src/treg/domain/governance/teams.py
  - src/treg/domain/governance/usage.py
  - src/treg/domain/identity/access.py
  - src/treg/domain/identity/api_keys.py
  - src/treg/domain/identity/session.py
  - src/treg/domain/identity/promotions.py
  - tests/test_team_limit.py
  - tests/test_auth.py
  - tests/test_token_revocation.py
  - src/treg/routers/auth.py
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

The registry is **tenant-isolated**: an **Org** owns resources, a **User** is a global identity, and a
**Membership** links them with a role and IS where the caller's token lives. A token = a `(user, org)`
pair, so every list/create/mutation and the proxy are scoped to the caller's org. Design source:
`docs/MULTI-TENANCY-PLAN.md` (standalone plan).

## Owned-team limit

An account may own at most 10 teams (`MAX_OWNED_TEAMS`). All owner memberships count,
including demo and suspended teams; joining as a member, admin or viewer does not.
`require_owned_team_slot` locks the user through the ownership write and commit, then checks
current owner memberships. The identity `lock_user` uses a no-op update for cross-process
serialization on Postgres and SQLite. Normal creation, onboarding demo creation and explicit
owner promotion share the guard. Requests over the limit return HTTP 403 with an actionable message.
Deleting a team or relinquishing ownership frees a slot. Existing excess teams stay accessible,
and recovery when an administrator deletes a sole owner is preserved; no migration or balance
change is required. This is a current-ownership cap, not a daily creation limit.

## The model (`models.py`)

[Enrich Arena](../interface/enrich-arena.md) runs and evaluations require both the creating user
and the active team to match. Regular team membership alone does not expose another member's results.
Team deletion removes evaluations before their runs through `ORG_SCOPED_MODELS`.

- **`Org`** — `id, name, slug (unique), previous_slug, suspended, demo, public_demo, created_at`. The tenant that owns
  secrets/tools/bundles. **`public_demo`** marks a team whose member token is PUBLISHED (e.g. on the
  landing page): non-admin members are locked to `/call` + reads and may never act as a user — enforced in
  `require_member` / `require_identity`.
- **`User`** - identity only: `id, email (unique), created_at`, plus `email_verified_at` and
  `signup_promo_available`. No token, no role. Verified accounts can claim signup credit once
  across all teams; old accounts cannot claim again. See [money](money.md#signup-credit-eligibility).
- **`Membership`** — `user_id, org_id, role (owner|admin|member|viewer), token_hash (idx), webhook_url,
  daily_call_cap` (per-user daily usage cap; `-1` = unlimited, admin-set — see the API fragment's
  usage-metering section), **`tool_access`** (JSON; **NULL = ALL tools** — the default, so nobody is
  restricted on upgrade — else the list of allowed tool NAMES) and **`local_run_enabled`** (bool, default
  true); unique `(user_id, org_id)`. One person in N orgs has N memberships (N tokens). `ROLE_RANK` orders
  owner > admin > member.
- **`ApiKey`** resolves a credential to a membership. The membership is still the only source for
  role, tool access, project access, local-run permission, daily cap, and billing identity. More
  keys do not create more quota. A known disabled or revoked key cannot fall back to
  `Membership.token_hash`. New human memberships use only their signed default key and leave that
  legacy compatibility field empty; existing hash-backed credentials continue through migration.
  After membership removal, signed-token resolution checks the detached, revoked Default control so
  the old token remains unauthorized; a token pinned to a deleted team is invalid rather than an
  ambiguous missing-team request. A signed Default token also carries the row's team-local generation;
  rotation increments it on the same row, invalidating the prior token without affecting another team.
- **`Invite`** — `org_id, email, role, code_hash (idx), status (pending|accepted|revoked), invited_by,
  expires_at, email_token_hash (idx, nullable)`, plus **`tool_access` + `local_run_enabled`** (the access
  to seed onto the membership when accepted — set access at invite time, edit later). Attached to an
  **email**: redeem the one-time code, **or** prove that email (any identity door) and accept it code-free
  — the code is a shortcut, not a requirement. `email_token_hash` is the inbox-only **second secret** in
  the emailed link — it can sign the invitee in (`GET/POST /auth/invite-signin?t=`, one-time), while the
  admin-visible code never can.
- **`Project`** — an OPTIONAL sub-scope inside an org (`org_id`, `name`, `slug`, unique `(org_id, slug)`).
  The org stays the hard isolation boundary; a project is a softer grouping on top. Deliberately a
  **label + ACL scope, NOT a namespace**: `Tool.name` stays unique per `(org_id, name)`, so no unique
  constraint had to be rebuilt (the flat-to-orgs migration had to do that once already, and SQLite cannot
  alter constraints portably). `Tool.project_id` NULL = **org-wide**, which is every tool that predates
  projects — so this shipped purely additive. Secrets stay org-level on purpose: one shared credential
  legitimately backs tools in several projects.
- **Machine identities** — a `User` on an **unroutable domain**, which is what makes it a machine
  rather than a person. Two exist: the published demo token (`PUBLIC_DEMO_DOMAIN`) and an **agent**
  (`AGENT_DOMAIN` = `agents.treg.local`). Both are minted by an admin and act ONLY by their token:
  `_is_machine_email` gates them out of every login door and out of `require_identity` (below). An
  agent needs **no new table and no migration** — it is a `Membership`, so it inherits `daily_call_cap`,
  `tool_access`, `local_run_enabled` and per-identity audit for free, which is exactly what makes those
  controls *per-agent*. NOTE: "agent" here is an IDENTITY; `agents.py` is the unrelated skill-directory
  table ("where does each coding agent keep its skills").
- Resource tables (`Secret`/`Tool`/`Bundle`/`CallRecord`/`PendingOAuth`) carry `org_id`; `owner`
  (creator email) is kept for audit + the member role gate. `Tool.name` is unique **per `(org_id, name)`**
  (`UniqueConstraint("org_id", "name")`), so two orgs may reuse a name.

## Enforcement (`domain.identity.access` and `domain.governance.access`)
- **`require_member`** resolves a signed default key or a hash-backed managed key → a live
  `Membership` → a `Caller` (`membership, user, org, api_key`, with
  `org_id`/`email`/`role` properties). It checks managed state before the legacy membership-hash
  fallback. It returns 401 for a missing, disabled, or revoked key. Last-used display metadata is a
  throttled background write after this dependency commits; it never extends the authentication
  transaction or changes authorization.
- Newly minted signed human credentials declare their purpose in the signed `scp` claim.
  `scp=team` is a Default key: its `org` is authoritative, so a conflicting `X-Treg-Org` is rejected
  instead of charging another team. That response tells older CLI users to run `treg update`, then
  `treg login`, because those clients can save a team before they obtain its key. `scp=bootstrap` is
  the short-lived, org-less login credential:
  it may identify the account for onboarding (list teams, create one, inspect/accept invitations),
  but `require_member` rejects it even when a caller supplies `X-Treg-Org`. Untyped tokens minted by
  older releases retain the prior header-first behavior during the compatibility window.
- **`_role_at_least` + `_can_manage`**: admin/owner may manage any resource in the org; a member only
  what they created (`resource.owner == caller.email`). Update/delete return 404 when the resource is in
  another org, 403 when the role gate fails. **`_require_can_register`** gates create (secrets/tools/
  skills/oauth): a **viewer** (rank below member) may `call` + list only, and gets 403 on any register.
  `ROLE_RANK` orders owner > admin > member > viewer.
- **Per-member tool ACL (the release feature).** `_require_tool_access(caller, tool.name)` gates **all**
  use of a tool — the proxy `call_tool`, the server `run_tool_server`, AND the local `grant_local_run`:
  allowed if the member's `tool_access` is NULL (all) or names the tool; the **owner is exempt**
  (`_tool_allowed`), admins + members can be restricted. `_require_local_run(caller)` additionally gates the
  LOCAL tier on `local_run_enabled` (off → server runs only). Set via `set_member_access`
  (`PATCH /orgs/{id}/members/{user}/access`, admin+; an owner can't be restricted): `_normalize_tool_access`
  validates the names against the org's tools (422 on unknown) and **collapses an all-tools selection back
  to NULL** so a fully-checked member keeps auto-getting new tools. It's an **explicit allow-list**: a
  *customized* member does NOT auto-get a newly-registered tool (the dashboard toasts a reminder). `Invite`
  carries `tool_access`/`local_run_enabled` (validated at `create_invite`) → copied onto the membership at
  both accept doors. `list_members` returns both fields. ACL refusal details originate as
  `AccessPolicyError`; call, run, and resource HTTP surfaces translate them to the same 403 response.
- **Agents (`create_agent` / `list_agents` / `revoke_agent`, `/orgs/{id}/agents`, admin+).** Mints a
  member identity for a machine caller, reusing the `create_public_token` recipe (re-POST the same name
  **rotates** — the old token dies there; revoke deletes the membership). Three invariants, each closing
  a real hole:
  1. **An agent token can never act as a USER.** `create_org` depends on `require_identity`, so without
     the `_is_machine_email` refusal there an agent could create a fresh org **in which it is owner** —
     and owners are exempt from `_require_tool_access` / `_require_local_run`, escaping every limit on it.
  2. **An agent can never be an owner** — blocked in `create_agent` AND in `set_member_role`, for the
     same exemption reason.
  3. **The address is org-scoped** (`agent-{org.slug}-{name}@…`, mirroring `_public_demo_email`): two
     orgs must each own an agent called `deploy` without sharing one `User` row, or a superadmin
     suspending one tenant's agent would kill the other's. Agents are always looked up by
     *(org + domain)*, never by recomputing the address, so an org rename can't orphan them;
     `_agent_name` strips the current OR previous slug prefix so pre-rename agents keep their name.
  Every identity door is blocked at the shared choke point `_find_or_create_user`, plus `register_user`
  (which predates it and creates a `User` directly) and `auth_email_start` (refuse early, mint no code).
  `list_members` carries `is_agent` so one roster can show people and machines apart.
- **Email-domain blocklist.** The same choke points, for throwaway mail and domains used for bulk
  registration. New verified accounts can receive one promotional balance, so farming verified inboxes
  remains an abuse path even though repeated team creation no longer earns credit. **Entirely configuration**: the classifier
  (`_is_blocked_email` in `domain/identity/access.py`, pure — it only answers) reads
  `TREG_BLOCKED_EMAIL_DOMAINS` and nothing else, parsed once per distinct value in `config.py`
  (trim, drop a leading `@`/`.`, lowercase, and drop any dotless entry so a typed `com` cannot
  refuse the world). Unset — the default — blocks nothing, and the next domain is a **dashboard
  edit, no redeploy**. No list lives in the code: a blocklist is a speed bump, since a new domain
  costs the other side minutes, so its only real value is being editable in the same minutes, which
  a deploy is not. Substring rules on the domain were tried and removed — measured against a public
  throwaway-domain corpus they matched 0.17% of it, added nothing over the exact entries, and
  refused a real company whose domain merely contained one of the strings. The rules that remain,
  each because the obvious implementation is wrong: match the **domain only**, never the whole
  address (matching the address false-flags real people whose username happens to contain a listed
  string); **walk parent domains**, whole labels off the front and never the bare last label,
  because registering `<random>.<listed-domain>` is otherwise a one-line bypass; **sign-in as well
  as sign-up** (an account that predates the listing gets no new session; existing accounts are
  suspended out of band). The DECISION lives in the application
  layer, `signup.blocked_email(email, door)`: it refuses, writes one structured line per block
  (`event=signup_blocked_domain door=<door> domain=<domain>` — the refusal reveals nothing, so the
  log is the only detection a burst has), and **fails open**, logging `event=blocklist_error`
  and letting the sign-in through if the classifier ever raises, because a misconfiguration must
  never break a real sign-in. The doors: `start_email_login` (before the rate window, so no code and
  no mail), `find_or_create_user` (so OTP verify, the GitHub and Google callbacks and the emailed
  invite link `POST /auth/invite-signin` refuse before the row lookup, raising
  `signup.BlockedEmailError` which each door translates to a `blocked_domain` kind), `register_user`
  (`POST /users` mints user + team + promo in one call), `create_org` (`POST /orgs`, the other promo
  door, reachable with a token minted before the listing) and the code-based `POST /invites/accept`
  (which constructs a `User` directly, so it guards itself). Every refusal is the `machine_identity`
  sibling's exact 403 `this address cannot be used to sign in` (a brand page on the browser doors,
  like `suspended`): the caller learns neither that a list exists nor what is on it. Deliberately a
  blocklist and nothing more: no allowlist, no table, no admin UI. Not covered: a session or identity
  token already live when the domain was listed keeps working until suspension or expiry (the
  out-of-band suspension); the promo grant and referral bonus are not separately gated, since with
  the doors closed no promo-funded team on a blocked domain can come into existence; and vendoring a
  full public disposable-domain list is a follow-up (megabytes of package data in the base wheel,
  which also ships the light CLI, and not yet checked against real users).
  **A rotate replaces the TOKEN, never the limits.** Because rotate is the same endpoint as create, an
  absent optional field used to fall back to its permissive default — and the dashboard's Rotate button
  sends only `{name, role, daily_call_cap}`, so a scoped agent silently became unrestricted
  (`tool_access=None` = every tool) just by getting a new token: round-4 blocker #2. `create_agent` now
  writes a field **only when the caller actually sent it** (`body.model_fields_set`, the shape
  `set_member_access` already used for `project_access`); a brand-new agent, having nothing to keep,
  still takes the documented defaults. `AgentIn` now DOES take `project_access` (slugs or ids, via
  `_normalize_project_access`) so an agent can be project-scoped at mint time — under the same
  sent-guard, so a rotate that omits it still preserves it. `Membership.created_by` (migration A23)
  stamps the minting admin, giving every agent an owner in the roster.
- **Observed agents (`GET /orgs/{id}/agents/observed`, admin+).** The OTHER half of the agents story:
  the runtimes already calling under members' own tokens. The CLI fingerprints its host runtime
  (`CLAUDECODE` → `claude-code`, `CODEX_*`, `CURSOR_*`, …; `TREG_CLIENT` overrides) and sends
  `X-Treg-Client`; `_client_of` normalizes it (slug ≤32, versions stripped, unknown-but-well-formed
  kept so a new runtime needs no release) onto `CallRecord.client` / `RunRecord.client` at all three
  audit points. The endpoint aggregates 30 days into one row per (member, runtime), excluding
  `''`/`cli` (a roster listing every human twice teaches nothing) and machine identities (already
  attributed to themselves). **Attribution, never authentication** — anything holding the token can
  claim any name, so nothing gates on it; scoping a detected agent for real = minting it a token
  (the dashboard's "Scope this agent" promotion).
- **Two ACL axes, composed as AND** (`_tool_usable` = `_tool_allowed` AND `_project_allowed`). The
  project scope is the coarse dial, `tool_access` the fine one; both are NULL-means-everything and the
  owner is exempt from both. `project_access` holds project **IDs**, not slugs, so the hot-path check is
  a pure set test (no id→slug query per call) and a rename cannot strand an access list.
  `project_access=[X]` with `tool_access=NULL` means "every tool in project X, **including ones added
  later**" — the composition that makes the coarse dial useful alone. `_normalize_project_access`
  accepts slugs or ids, 422s on an unknown one, and collapses an all-projects selection back to NULL
  (mirroring `_normalize_tool_access`). Endpoints: `create_project` / `list_projects` (a scoped member
  sees only their own) / `delete_project` (admin+) — deleting **frees** its tools back to org-wide rather
  than hiding them, and drops the id from every member's scope, **storing an emptied list as `[]`, never
  NULL**. NULL means *every project*, so collapsing `[]` would hand a member scoped to only the deleted
  project the run of every OTHER project's tools — a privilege escalation fired by an unrelated delete
  (round-4 blocker #1, `test_security_round4.py`). `[]` already carries the intended meaning (org-wide
  tools only), and nobody is locked out because the **freed tools** are what they keep: whatever the
  member could reach before the delete they can still reach after it. That, not a widened scope, is
  what "never lock anyone out" rests on. Invites carry `project_access` onto the membership at both
  accept doors, exactly as `tool_access` does.
- Every list filters by `caller.org_id`; every create stamps `org_id = caller.org_id` +
  `owner = caller.email`; `_resolve_call` scopes **both** the named lookup and the host/longest-prefix
  passthrough to the org; `call_tool` loads only same-org secrets. See [proxy-model](proxy-model.md).

`domain.identity.access` is the shared identity/access boundary: `Caller`, token/session/org resolution,
dependencies, role comparison, and machine classification. Session signing and validation live in
`domain.identity.session`. Two token families share one HMAC key but newly minted credentials carry a
signed audience: `make_session` creates `aud=session` with a required 7-day `exp`, while
`make_identity` creates `aud=identity`; copied team keys omit `exp`, while an org-less
`scp=bootstrap` identity expires after seven days. `read_session_claims` and
`read_identity_claims` reject the other audience in both directions; `token_version` remains the
revocation mechanism for either family.

Legacy tokens predate `aud`, so the compatibility boundary follows what the signed shape can actually
prove. An `org` claim identifies a team-pinned copied key, which remains usable after its former
30-day `exp`; an untyped no-`exp` key is also identity-only. An untyped org-less token with `exp` is
indistinguishable from a browser session: it works on either path only until that timestamp, and the
bearer path refuses it once expired rather than reviving an expired cookie.
- **Registration is shared across doors:** `application.signup.find_or_create_user(db, email)` finds a user or creates them
  — **the user ONLY, no auto personal org**. Every identity door calls
  it (GitHub / Google callbacks, email OTP), so "first proof = registration" is identical. A brand-new
  user therefore lands with **zero teams** and must name + create their first one (the dashboard's
  mandatory welcome, or `treg org create`). Their seven-day bootstrap token works before an org exists
  but cannot call or read team resources. **`create_org` uses `require_identity`, NOT
  `require_member`** — else a zero-org user could never make their first team — and returns the new
  membership's team-scoped Default key. See [api](../interface/api.md).
- **Code-free invites:** `my_invites` (`GET /invites/mine`, `require_identity`) lists pending invites for
  the caller's proven email, newest creation time first with descending ID breaking timestamp ties; `accept_my_invite` (`POST /invites/{id}/accept`, `require_identity`) joins
  with no code (403 if `invite.email != user.email`, 409 if already a member). The code path stays.
- **Org management endpoints:** `register_user` (`POST /users`, legacy open-registration, used by the
  test fixture) still creates the user + an org + owner membership via `_make_org_membership` (mints the
  token) — NOT reached by the dashboard/CLI login doors, which no longer auto-make an org. Both this door
  and `create_org` read the first-party ad-click cookie (`application.signup._ad_attribution_from`) and,
  when enabled and present, stamp `Org.ad_gclid`/`ad_click_id_type`/`ad_landing`/`ad_click_at` on the
  new org — preserving whether the click was a GCLID, GBRAID or WBRAID — see
  [ads-conversions](ads-conversions.md). `create_org`
  (`POST /orgs`, `require_identity`),
  `list_orgs` (`GET /orgs`), `create_invite` (`POST /orgs/{id}/invites`, admin+), `accept_invite`
  (`POST /invites/accept`, open + code-protected → registers the user if new, joins them to the invited
  team, mints its token; a brand-new invitee joins the invited team **only** — no separate personal org),
  `list_members`
  / `remove_member` (`GET`/`DELETE /orgs/{id}/members[/{user}]`, admin+; owners cannot be removed).
  `_require_admin_of(org_id, caller)` gates the admin endpoints (token must be for that org + role ≥ admin).
- **An identity leaving takes its caller-owned state with it (`delete_membership`).** A `DenyRule` aimed at
  one caller (`user_id` set) is meaningless once that caller is gone, and it lingers in the Policy
  table naming a user id nobody can resolve. `remove_member`, `leave_org` and `revoke_agent` sweep the
  rules for that `(user_id, org_id)`; `admin_delete_user` sweeps **every org's** rules for that user,
  because `DenyRule.user_id` is a foreign key and a surviving row would dangle — Postgres rejects that
  outright, while SQLite only hides it by not enforcing FKs (so the test suite alone cannot catch it).
  ORG-wide rules (`user_id` NULL) are never touched: they are about the team, not about one caller.
  The same helper deletes `IdempotentCall` rows keyed to the membership before deleting it; those are
  replay caches, not audit history, and no valid caller remains after revocation. The foreign key also
  uses `ON DELETE CASCADE` as a database-level backstop. This closes the production failure where
  revoking an agent that had made an idempotent paid call returned 500 and rolled its token revocation
  back. Mirrors how `delete_project` sweeps the id it deletes out of every `project_access`.
- **Org administration:** `set_member_role` (`PATCH /orgs/{id}/members/{user}`, **owner-only** via
  `_require_owner_of`; a `_count_owners` last-owner guard blocks demoting the sole owner — ownership
  transfer = promote another to owner, then step down), `leave_org` (`POST /orgs/{id}/leave`, self-removal,
  same last-owner guard), `delete_org` (`DELETE /orgs/{id}`, owner-only, cascades every org-scoped row
  through `cascade_delete_org` / `ORG_SCOPED_MODELS` in `domain/governance/teams.py` - including any
  pending `AdConversion`: a queued conversion belongs to the team it would be attributed to,
  `Media`: hosted reference files would otherwise outlive the team until their TTL, and
  `HubListing`/`HubTool`: a maker's published tools and search listings go with the team that owned
  them).
  **That list is the only one**, plus one named exception: `cascade_delete_org` also deletes every
  `HubRun` where `caller_org_id == org.id` before that sweep, because a run names the team that
  CALLED by that foreign key and the maker only by number — a caller's traces go with the caller, a
  maker's deletion leaves callers' history intact. Owner delete, admin force-delete, the landing-sandbox reaper and the
  demo reset all go through it; `test_org_delete_clears_EVERY_org_scoped_table` walks the models module
  for anything carrying `org_id` and also refuses a reaper that keeps a private copy. The sandbox reaper
  did until 2026-09-02, its copy never learned about `IdempotentCall` (which references a Membership),
  and every sandbox mint 500'd at the foreign key until it was fixed.
- **Invites lifecycle:** one-time **and** time-bounded — `Invite.expires_at` (default `INVITE_TTL_DAYS`),
  `accept_invite` returns `410` past expiry. `list_invites` (`GET /orgs/{id}/invites`, admin+) and
  `revoke_invite` (`DELETE /orgs/{id}/invites/{invite}`, admin+); expired codes are garbage-collected by
  `health.gc_expired_invites` (opportunistically on list, periodically in the health run).

## Hardening (invariants enforced)
- **Email is a case-insensitive identity.** `_norm_email` (strip + lowercase) is applied at every
  identity door and every invite comparison, so `Bob@X.com` and `bob@x.com` are one user/one personal
  org and an invite is always redeemable regardless of the case typed.
- **Invite hygiene.** `create_invite` refuses to invite an email that is already a member (409, no
  dead-end invite) and **supersedes** any prior pending invite for that email (one live code per
  invitee). `revoke_invite` only deletes a still-`pending` invite. An admin may not issue an `admin`
  invite (owner-only, mirroring `set_member_role`). Suspended users/orgs can neither view nor accept.
- **Governance never evaporates.** `admin_delete_user` promotes the earliest-joined survivor to owner
  when it removes an org's sole owner; the accept/create paths return a clean `409` (not a 500) on the
  membership/slug uniqueness race (`create_org` retries with a fresh `_unique_slug`).
- **Slug vs id.** `_resolve_org` resolves `X-Treg-Org` by slug first (an all-digit slug like `2024` is
  producible and must not be reinterpreted as a primary key), then by `previous_slug`, then by id.
- **Rename.** `PATCH /orgs/{id}` (admin+, `teams.rename_org`) changes `name` and/or `slug`. The slug
  is baked into signed team keys, `~/.treg`, MCP pins and agent addresses, so a slug change retires
  the old one into `previous_slug` instead of revoking every copied key: it still resolves, and no
  other team may take it (`_slug_taken` checks both columns). One alias only; a second rename
  overwrites it. Slugs are validated as their own `_slugify`, 3–40 chars, never `sbx-` (the sandbox
  shape). Stripe metadata and the analytics group key keep the slug they were stamped with.
- **Reserved names.** `teams.reserved_reason(text, extra)` refuses a name or slug that *reads* as
  treg itself, as "official"/"verified", or as a catalog provider/platform (a hub tool's callable id
  is `<team slug>.<name>`, so a squatted slug could pass a stranger's tool off as ours or a
  provider's). Matching is by how the text reads, not its bytes: NFKC-fold, map lookalike
  Latin/Cyrillic/Greek letters and leetspeak digits (`teams._LOOKALIKE`), strip separators, then
  compare words — so `trеg-hub` (Cyrillic е), `t-r-e-g`, `apol1o` and `Hunter.io data` are all
  caught. `signup.reserved_team_names()` supplies the catalog-derived `extra` set (provider slugs
  exact-or-prefix, platform slugs `=`-marked for an exact-word match only). Checked at `create_org`,
  `register_user`'s default team name, and `rename_org` (`PATCH /orgs/{id}`); a superadmin may bypass
  it (`allow_reserved`).

## Schema ownership
Alembic owns the multi-tenant schema. The 0.14.x adoption release converted and stamped legacy
databases; current releases refuse a non-empty unstamped database and direct the operator through that
floor. `db.verify_db()` checks revision compatibility without creating or repairing tenancy tables.

> Health (`run_all`) takes an `org_id` filter so `/health/run` never leaks other orgs' credentials, and
> alerts resolve the owner's per-org membership webhook. See [auth-secrets](auth-secrets.md).

## Caller tags and pinned read scopes

Caller-supplied `X-Treg-Meta` tags remain attribution labels: an unpinned org token may choose any
valid value. A restricted agent's `Membership.pinned_tags` is enforced by the server; a conflicting
header is a 403. An unpinned operator retains the org-wide view and the shared balance.

`domain.governance.access.pinned_tag_predicates` requires every pinned key/value in the stored tags.
`/calls`, `/calls/{id}/result`, `/calls/{call_ref}` and `/runs` apply that scope before pagination or
loading archive bodies. A known foreign or unattributed id is a 404, just like an unknown id.
`POST /reviews` resolves its call through the same predicates, so a pinned caller can neither rate nor
probe another pin's call. Every audit writer stores the pin, the routed parent row and the router's
refusal fallback included. A feedback report snapshots the reporter's pin (`Feedback.tags`, also in
revision `0042`): `GET /feedback/{id}` reads through it and a pinned reporter's `call_ids` verify only
against its own pin's rows.
Matching is by pin, not by current membership: two identities with the same pin share that view.
Changing a membership's pin does not relabel its earlier records.

`AsyncTaskRecord.tags` and `AsyncResourceRecord.tags` snapshot the effective submission tags in their
own transaction, independent of the lossy audit queue. Resources discovered by a terminal poll or
worker inherit the original task's tags. Shared-provider poll/fetch ownership checks use those
snapshots as well as org, provider and resource identity; a pinned refusal is 404. The unpinned
403 contract is unchanged. BYOK and own-tool upstream access still follows the existing credential
and tool ACLs: these read scopes do not partition a team's own provider account.

The reserve ledger entry freezes authoritative tags in `meta.tags`; `/calls/{call_ref}` can therefore
serve an authorized ledger-only history even when audit was shed and the hold was released. Missing
historical attribution is never inferred from current membership or mutable spend counters.
Revision `0042` adds nullable tags to run/task/resource records without backfilling guesses. Old
untagged rows remain readable by unpinned org members, but not by pinned identities. An older binary
can run with the additive schema, but does not enforce the new read boundary.

Both run audit writers store membership pins. Both history sources in `/runs` filter before their
limits. Runs do not gain caller-supplied metadata parsing in this change.

Treg's replay key includes the full pin as well as the existing primary-tag scope, so changing a
secondary pin cannot expose an old replay. On the shared provider credential, the forwarded
idempotency label is additionally partitioned by the full pin; unpinned forwarding is byte-for-byte
unchanged. The plain BYOK label remains verbatim. Public media URLs are still bearer-by-possession
links for vendor fetching; filtering history prevents discovery through those authenticated reads,
not access by someone already holding a URL.

`TagBudget` remains a ceiling on the org's shared balance, never a sub-account. `TagSpend` and
`TagBudget` remain org-scoped in `domain.governance.teams.ORG_SCOPED_MODELS`, with `TagSpend` ahead of
the ledger/hold it references. Pinned read scopes do not change budget concurrency or settlement.

- **Shared-provider async objects are org-scoped.** Platform-key poll and result-fetch utility calls
  must resolve their id through an org-owned `AsyncTaskRecord` or `AsyncResourceRecord` before the
  upstream is contacted. `_one_resource_value` reads the id from the query string or, when the
  catalog descriptor names a body parameter (`in: body`), from the JSON body — either way exactly one
  value must be supplied. BYOK calls keep access to ids in the team's own provider account.
- **Shared-provider durable objects are org-scoped.** A platform-key managed-resource call verifies
  every scalar or array id against `ProviderResource` before contacting the provider. A `use` tool
  may additionally declare a read-only public lookup: an id absent from the ownership table is
  accepted only after the provider confirms the catalog predicate with no DB connection held. Any
  id assigned to another organization or tombstoned is denied locally and never sent through that
  lookup. All members may create, list, use, rename and delete their team's objects; the organization
  boundary, not the creator, owns them. BYOK bypasses this table.
  The dashboard's Team resources inventory explicitly requests `source=platform`; a connected BYOK
  account therefore never replaces or widens the organization's durable-resource inventory.

## Signup analytics boundary

`find_or_create_user` optionally collects the IDs it actually inserted after a successful flush;
a concurrent insert loser returns the existing user without marking it new. Email OTP and
GitHub/Google auth pass that collection to `track_signup` **after their commit**, emitting
`signup_completed` only for new accounts. The optional entry-surface cookie is analytics metadata,
allowlisted by `analytics.funnel_surface`; it never affects authentication or team access.


## Released CLI compatibility

The unmodified PyPI CLIs 0.16.0 and 0.19.0 can use existing saved tokens, complete browser login,
and exchange Default keys with `org use`. Their email flow discards the browser cookie and would
save a restricted bootstrap token. Their team-create and identity-mode invite flows keep the
previous token after selecting the new team. A scoped Default key must still reject that mismatch.

`routers.auth_helpers.require_managed_cli` stops these known old-client requests with HTTP 426
before issuing email credentials, creating a team, or consuming an invite. The response tells the
user to run `treg update` and retry. Current CLI requests send `X-Treg-Key-Protocol: 1` and save the
returned team's key. The legacy-client hint is the released CLI's `python-httpx/` User-Agent plus
`ngrok-skip-browser-warning: 1`, without that protocol marker. It is a compatibility check, not an
authorization boundary or a universal client-version detector. Browsers and generic API clients
retain their API behavior; omitting or forging the hint never relaxes token restrictions.

Existing unscoped tokens retain their old team-create behavior. Fresh email login and team changes
with typed credentials require the updated CLI on the affected paths. This is a controlled upgrade
requirement, not full support for all fresh-login flows in old clients. The released-wheel test in
`test_released_cli_compat` checks that refusal preserves config bytes and the prior usable team.
