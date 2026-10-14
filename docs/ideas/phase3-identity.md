# Phase 3 identity and control design

Status: Checkpoint 0 draft. This file is a review artifact only and must not be committed.

Source of truth read for this design:

- `/tmp/treg-phase3-brief.md`
- `docs/REFACTOR-PLAN.md` sections 1.1, 1.2, 1.3, 1.6, 1.7, Stage 3, and section 5
- the current `src/treg/api.py`, `src/treg/routers/dependencies.py`, `src/treg/session.py`,
  `src/treg/mcp_oauth.py`, `src/treg/bootstrap.py`, and import-linter configuration
- `tests/snapshots/routes.json`, `openapi.json`, `composition.json`, and `lifespan.json`
- the relevant context fragments: composition, import boundaries, MCP OAuth, multi-tenancy,
  super-admin, API, and dashboard

The branch is `refactor/phase3-identity` at `5636794`. The tracked worktree initially contained an
accidental `uv.lock` rewrite. It contained lock-format timestamp/wheel expansion and no intended
project dependency change, so it was restored exactly as the brief instructs. Only the known ignored
local artifacts remain untracked.

## 0. Decisions required before code movement

Checkpoint 0 found two source-of-truth conflicts and three scope ambiguities. They are not resolved in
this document. Code work must wait for review to choose the boundary.

### 0.1 `session.py` role disagrees with the running dependency graph

Section 5 says `session.py -> domain/identity`, role `control`. The current dataplane MCP path imports
`mcp_oauth` in `mcp._oauth_claims` and the invalid-token guard. `mcp_oauth._key()` imports
`session._key()`. The dataplane therefore needs the session signing-key implementation to validate an
MCP access token, even though it does not run browser login.

Proposed correction for review: the destination stays `domain/identity`, but its runtime role is
`both`, with browser-session issuance used only by control and shared token-signature validation used
by both. This PR must not edit the plan unless review explicitly approves that wording.

### 0.2 Byte-identical movement conflicts with a thin `application/auth` boundary

The multi-step auth implementations currently are the FastAPI endpoint functions themselves. Their
source includes `@app.*` decorators, `Depends`/`Cookie`/`Form` parameters, HTTP exceptions and response
objects, and database commits. The hard rule says the moved symbol, including its decorator, must be
byte-identical. Section 1.1 says routers translate HTTP and are thin; the brief says multi-step login,
CLI pairing, and OAuth authorization-server flows become application auth use cases.

Those constraints cannot all be achieved in one mechanical move:

1. Moving the exact decorated functions to `routers/auth.py` preserves bytes and surface, but leaves
   orchestration and commits in the router.
2. Moving them exactly to `application/auth.py` requires an `app = APIRouter()` decorator target and
   makes the application layer depend on FastAPI, so it is not the target application boundary.
3. Extracting a framework-neutral application function and leaving a thin router wrapper changes the
   original handler source. It can be behavior-equivalent and tested, but it is not a byte-identical
   symbol move.

Recommended review choice: use two commits per auth journey despite the general one-move wording. The
first mechanically moves the complete decorated block to `routers/auth.py`; the next, still scoped to
the same journey, extracts a new application function and changes only the already-moved router into a
thin wrapper. The second is a behavior-neutral refactor, not a behavior change, and must carry the same
E2E journey plus all four snapshots. If the intended standard is instead one commit per journey total,
review must approve option 1 as the Stage 3 intermediate architecture.

### 0.3 `resources` has no section 5 row because `api.py` is intentionally omitted

Section 1.1 names a `routers/resources` destination, but section 5 says `api.py` is not repeated. The
route snapshot gives an obvious core block at positions 156 through 174: secrets, tools, local-run
grant/report, skills, and bundles. Two edges are not obvious:

- `POST /tools/{name}/grant` and `POST /tools/{name}/run-report` are resource-shaped URLs but execute
  the `application/run` concern and write audit rows. Proposed owner: leave them in `api.py` until the
  runner extraction, unless review defines `resources` to include them in Stage 3.
- `GET /calls`, `GET /calls/{call_ref}`, and `GET /runs` immediately follow the resource block but are
  audit/activity views. Proposed owner: leave them for the call/run stages.

The remainder of this document treats resource CRUD as positions 156 through 164 and skill/bundle as
167 through 174, leaving 165, 166, and 175 through 177 in `api.py` pending review.

### 0.4 Capability pins are not assigned to governance in the domain matrix

Routes 149 through 151 live among org policy routes, but `CapabilityPin` selects a Catalog provider.
Section 1.3 assigns teams, members, projects, deny rules, limits, and budgets to governance and assigns
capability/pricing selection to catalog. Proposed owner: leave capability-pin routes in `api.py` for
the catalog stage. If Stage 3 means every current org-prefixed control route, review must explicitly
assign pins to governance.

### 0.5 Old module import paths need a compatibility ruling

Tests and runtime code import `treg.session` and `treg.mcp_oauth` directly. Unlike `treg.api:app`, the
brief does not explicitly require these paths to remain public, but deleting them would be an observable
import change. Proposed approach: leave thin re-export shims at both paths for this stage, while all
runtime owners import `treg.domain.identity.session` and `treg.domain.identity.mcp_oauth`. The section 5
destination is then real, but the old paths remain compatibility facades. `routers/dependencies.py` is
different: Stage 3 explicitly requires deletion, so it receives no shim.

## 1. Reconciliation against destination map and snapshots

### 1.1 Destination package shape

`domain/identity` must be a package, not one `identity.py`. Both `session.py` and `mcp_oauth.py` contain
distinct `_key`, `_b64`, and `_unb64` symbols, so combining them would force renaming and violate the
byte rule. Proposed shape:

```text
src/treg/domain/
  __init__.py
  identity/
    __init__.py       public identity exports
    session.py        exact body of current treg/session.py
    mcp_oauth.py      exact body of current treg/mcp_oauth.py
    access.py         Caller, resolution, roles, and auth dependencies

src/treg/application/
  __init__.py
  auth.py             multi-step identity proof, pairing, OAuth AS orchestration
  signup.py           identity provisioning and first-team creation/grant sequencing
  connect.py          OAuth/key connection workflows

src/treg/routers/
  auth.py
  orgs.py
  resources.py
  connections.py
```

The exact split between `routers/auth.py` and `application/auth.py` is blocked on section 0.2.

### 1.2 `session.py -> domain/identity`

Exact symbols:

- constants: `TTL_SECONDS`, `COOKIE`, `_EPHEMERAL_KEY`
- functions: `_key`, `_b64`, `_unb64`, `make`, `read_claims`, `read`

Current importers:

- runtime: `api.py`, `routers/dependencies.py`, `routers/web.py`, `mcp.py`, and `mcp_oauth.py`
- tests: `test_auth.py`, `test_login_page.py`, `test_token_revocation.py`, `test_mcp_oauth.py`,
  `test_legacy_host_redirect.py`, `test_bughunt_server.py`, and `test_single_user.py`

Reconciliation result: destination agrees, role does not. See 0.1. No lifespan task is owned by this
module. The route snapshot depends on it indirectly through every auth dependency and the MCP mount.

### 1.3 `mcp_oauth.py -> domain/identity` grant/refresh family

Exact existing symbols:

- constants: `ACCESS_TTL_SECONDS`, `_TOKEN_TYPE`, `_CIMD_TIMEOUT_S`, `_CIMD_MAX_BYTES`,
  `_CIMD_REFRESH_S`
- token/resource functions: `_key`, `_b64`, `_unb64`, `mcp_resource_url`,
  `mcp_resource_audiences`, `normalize_resource`, `read_access_token_any`,
  `protected_resource_metadata`, `authorization_server_metadata`, `make_access_token`,
  `looks_like_access_token`, `read_access_token`, `verify_pkce`
- client functions: `valid_redirect_uri`, `redirect_uri_allowed`, `fetch_client_id_metadata`,
  `new_client_id`

The section 5 phrase "grant/refresh family" also maps these current `api.py` primitives into identity:

- `_ensure_grant`
- `_family_org`
- `_refresh_is_live`
- `_issue_refresh`
- `_revoke_refresh_family`

`_refresh_grant` is orchestration: it checks a presented token, rotates or kills a family, commits,
and constructs an HTTP response. It belongs to `application/auth`, not the identity leaf. The OAuth
route handlers call the five domain primitives through application auth.

Current importers are `api.py`, `mcp.py`, `tests/test_mcp_oauth.py`, `tests/test_mcp.py`, and
`tests/test_legacy_host_redirect.py`. No monkeypatch targets a `mcp_oauth` symbol. Two tests patch
`health.safe_webhook_url`, `health.host_is_public`, and `httpx.AsyncClient.get`, which remain valid
because `fetch_client_id_metadata` performs those lazy imports at call time.

Reconciliation result: route positions 67 through 75 and 92 through 93 use this family on control;
position 72 and the `/mcp` mount validate it on dataplane. The file currently writes no database rows;
the `oauthclient`, `oauthgrant`, and `oauthrefresh` writes named in section 5 are still in `api.py`, which
explains rather than contradicts the destination wording.

Layer debt to record, not fix in a pure move: `fetch_client_id_metadata` imports concrete `httpx` and
the root `health` module from inside a domain function. It does not create a sibling-domain edge today,
but it is not the final infra-port shape described by section 1.4.

### 1.4 Transitional dependencies dissolution

Move to `domain.identity.access`, byte-identically per symbol:

- `Caller`
- `_membership_by_token`
- `_user_from_session`
- `_resolve_org`
- `require_identity`
- `_user_from_identity_token`
- `require_member`
- `require_superadmin`
- `_role_at_least`
- `_norm_email`
- `PUBLIC_DEMO_DOMAIN`, `AGENT_DOMAIN`
- `_is_agent_email`, `_is_machine_email`

The HTTP cookie helpers are not identity domain rules and must not create a future
`identity -> referrals` sibling edge:

- `_is_https`, `OAUTH_RETURN_COOKIE`, `_remember_oauth_return`, `_take_oauth_return` move with the auth
  interface helper module.
- `REFERRAL_COOKIE`, `REFERRAL_COOKIE_MAX_AGE`, `_remember_referral`, `_take_referral` move to a small
  router/shared signup-cookie module used by `routers.web` and signup orchestration. They must not live
  under `domain.identity` because `_take_referral` calls `referrals.normalize_code`.

`api.py` may re-export the identity names during the staged moves, but all new `Depends(...)` sites must
import directly from `domain.identity`. `routers/admin.py`, `routers/catalog.py`, and `routers/web.py`
must be updated in the identity commit. At the end, full-repo `rg routers.dependencies` must return no
Python import and `src/treg/routers/dependencies.py` is deleted. There is intentionally no compatibility
shim for that transitional path.

### 1.5 Routes reconciliation and registration points

The current relevant route order is:

| Snapshot positions | Concern | Stage 3 disposition |
|---:|---|---|
| 24-38 | social login, CLI pairing, login page, auth/me/logout, email OTP, invite sign-in | auth |
| 39-66 | moved web routes | unchanged separator |
| 67-75 | OAuth client registration, authorize, revoke/token, discovery/challenge | auth |
| 76-87 | moved public docs and static mounts | unchanged separator |
| 88-89 | CLI token and token revocation | auth |
| 90-91 | legacy user registration and org creation | signup/governance |
| 92-93 | OAuth grants list/team move | auth |
| 94-97 | org list and invite create/accept/list | governance |
| 98-110 | onboard/demo/Stripe/skill samples | stays in `api.py` |
| 111-123 | invites, members, usage/settings/budgets | governance, except money ownership is preserved |
| 124-131 | billing/referrals | stays in `api.py` |
| 132-148 | member access/lifecycle, machine identities, projects | governance |
| 149-151 | capability pins | proposed stay for catalog, review required |
| 152-155 | deny policy | governance |
| 156-164 | secret and tool CRUD | resources |
| 165-166 | local grant/report | proposed stay for runner, review required |
| 167-174 | skills and bundles | resources |
| 175-177 | calls and runs activity | stays in `api.py` |
| 178-189 | OAuth connect, connections, discovery/resource selection, health | connections |
| 190-197 | existing admin read router | unchanged |
| 198-202 | admin identity/org mutations | identity/governance control |
| 203-206 | existing admin reports router | unchanged |
| 207-210 | access preflight, call, run, MCP | unchanged |

Each target router exports several ordered `APIRouter` blocks, and `api.py` appends each at the exact
original point using the existing direct attachment convention. No `include_router()` regrouping is
allowed. The planned attachment shape is:

```text
routers.auth
  login_router       24..38
  oauth_server_router 67..75
  token_router       88..89
  grants_router      92..93

routers.orgs
  signup_router      90..91
  org_entry_router   94..97
  org_detail_router  111..123
  org_control_router 132..148
  policy_router      152..155
  admin_mutations_router 198..202

routers.resources
  crud_router        156..164
  skill_router       167..174

routers.connections
  router             178..189
```

This preserves every interleaving with Stage 2 routers, onboarding, billing, admin reads/reports, and
call/run. Stacked discovery decorators remain in their original text and therefore retain the current
snapshot order: `/oauth-protected-resource/mcp` before `/oauth-protected-resource` in the rendered
route list.

### 1.6 Lifespan reconciliation

`lifespan.json` has no auth, governance, or resource tasks. It has one Stage 3 connection-owned startup
write:

- `treg.api._backfill_provider_extra_tools`

The function belongs with connection provisioning, but the snapshot and all three role startup
manifests name the compatibility path. Move its exact body to application connect and re-export the
same function object from `api.py`; do not change the snapshot string or role manifests. It remains in
all, control, and dataplane startup under the section 1.6 transitional write carve-out, which expires
at Stage 5. No new startup write may join it.

## 2. Proposed movement units and commit order

The order follows imports: identity leaf first, then auth, governance, resource ownership, and finally
connections, whose auto-provisioning depends on resource helpers. Every moved function/class retains
its exact source. Constants and block comments move with their block. New module docstrings, import
blocks, router aliases, attachment comments, and compatibility re-exports are the only mechanical
scaffolding exceptions.

The auth sequencing below assumes review approves the two-step approach in 0.2. If review chooses the
intermediate-router approach, omit each `application` follow-up and record that the thin-router target
is deferred.

### Commit 1: identity leaf and transitional dependency deletion

Ownership:

- `domain.identity.session`: every symbol in 1.2
- `domain.identity.mcp_oauth`: every existing symbol in 1.3 plus `_ensure_grant`, `_family_org`,
  `_refresh_is_live`, `_issue_refresh`, `_revoke_refresh_family`
- `domain.identity.access`: the identity/access list in 1.4
- auth/referral HTTP cookie helpers move to their non-domain homes described in 1.4
- update all importers and delete `routers/dependencies.py`
- optional `treg.session` and `treg.mcp_oauth` compatibility shims, pending decision 0.5

Import-linter in the same commit: add `Identity is a domain leaf`, with
`source_modules = ["treg.domain.identity"]`, `as_packages = true`, and explicit forbidden sibling
packages `governance`, `connections`, `tools`, `catalog`, `capacity`, and `money`. The contract must be
tested against import-linter 2.13 before commit; if nonexistent forbidden modules are rejected, create
the contract when the first sibling package is introduced in Commit 7, not by adding fake placeholder
domains. Review approval is required because the brief asks for activation in the extraction commit.

Journey: dependency behavior is pinned by the full `tests/test_router_dependencies.py`, plus
`tests/test_auth.py::test_session_scopes_by_x_treg_org` and
`tests/test_mcp_oauth.py::test_our_own_token_is_accepted_and_carries_the_team`.

### Commit 2: email OTP login journey

Symbols from `api.py`:

- `EmailStartIn`, `EmailVerifyIn`
- `EMAIL_CODE_TTL`, `MAX_OTP_ATTEMPTS`, `OTP_NS`, `OTP_START_NS`, `OTP_START_WINDOW_S`,
  `OTP_START_MAX_PER_EMAIL`, `OTP_START_MAX_PER_IP`
- `auth_email_start`, `auth_email_verify`
- `_find_or_create_user` and `_client_ip` as upstream use-case helpers

The exact decorated endpoints first move to `routers.auth.login_router`. Application extraction, if
approved, places identity proof/rate-limit/session sequencing in `application.auth`; first identity
creation remains the shared signup command rather than being copied across OAuth doors.

Journey: `test_auth_email.py::test_first_login_registers_user_with_no_org_then_reuses_identity` covers
start, dev code, verify, session/token issuance, and reuse. Rate-limit and one-time-code tests remain in
the same file.

### Commit 3: CLI pairing journey

Symbols from `api.py`:

- state/constants: `CLI_TOKEN_TTL`, `HANDSHAKE_TTL`, `CLI_APPROVE_MAX_TRIES`, `_PAIR_ALPHABET`,
  `_cli_pending`, `_cli_results`, `_LOGIN_ID_RE`
- `CliApproveIn`, `_prune_handshakes`, `_norm_pair_code`, `_orgs_brief`
- `auth_cli_start`, `auth_cli_poll`, `auth_cli_orgs`, `auth_cli_approve`, `login_page`,
  `_login_page_html`
- the exact login page constants `_AUTH_HEAD`, `_LOGIN_CSS`, `_LOGIN_JS`

`_same_origin` is shared with logout and OAuth approval, so it moves once into the auth interface
helper before these units. `_auth_page` is shared by social login, invite login, OAuth connect callback,
and remains a shared auth presentation helper rather than being copied.

Journey: `test_login_page.py::test_email_door_completes_the_handshake` proves
email start -> verify -> CLI start -> approve -> poll. The pairing guard is separately pinned by
`test_approve_requires_a_started_login_and_matching_code` and `test_wrong_code_attempts_are_capped`.

### Commit 4: GitHub/Google browser login journey

Symbols from `api.py`:

- `auth_github`, `auth_github_callback`, `auth_google`, `auth_google_callback`
- `_finish_oauth_login`, `_auth_page`, `_login_callback_base`
- `auth_me`, `auth_logout`, `_intercom_user_hash`

Journey: `test_auth.py::test_github_login_creates_user_session_but_no_auto_org` and
`test_google_login_creates_user_session_but_no_auto_org`; `test_bad_state_rejected` and
`test_google_bad_state_rejected` pin CSRF refusal.

### Commit 5: invite sign-in auth journey

Symbols from `api.py`:

- `_live_invite_by_email_token`
- `auth_invite_signin`, `auth_invite_signin_confirm`

This unit imports governance invite reads through a narrow command/read function once governance
exists. Because auth must move before governance for the identity-first order, the first mechanical
move may continue to read shared models directly; do not create an `identity -> governance` edge.

Journey: `test_invites_mine.py::test_email_link_post_signs_in_once_and_lands_on_invite_org`, plus the
replay and suspended-user tests in that file.

### Commit 6: MCP OAuth authorization-server journey

Symbols from `api.py`:

- DTO/constants: `OAuthClientRegistration`, `GrantTeamIn`, `AUTH_CODE_TTL_S`, `REFRESH_TTL_S`,
  `_CONSENT_CSS`
- client/authorize helpers: `_resolve_oauth_client`, `_consent_page`, `_wrong_resource`,
  `_same_mcp_resource`, `_oauth_error`, `_authorize_request`
- refresh orchestration: `_refresh_grant`
- endpoints: `oauth_register`, `oauth_authorize`, `oauth_authorize_approve`, `oauth_revoke`,
  `oauth_token`, `oauth_protected_resource`, `oauth_authorization_server`,
  `openai_apps_challenge`, `oauth_grants`, `oauth_grant_set_team`

Identity grant/refresh primitives already live in Commit 1. The auth application unit owns session and
transaction sequencing. Route blocks attach at 67 through 75 and 92 through 93.

Journey: `test_mcp_oauth.py::test_the_whole_flow_end_to_end` proves register -> authorize -> approve ->
exchange -> MCP validation. Refresh rotation/replay and team-move tests remain mandatory for this commit.

### Commit 7: CLI token issue and user token revocation

Symbols from `api.py`:

- `auth_cli_token`, `auth_revoke_tokens`
- `_is_last_active_superadmin` remains shared with admin controls until Commit 13

Journey: `test_token_revocation.py::test_revoke_kills_old_identity_token_and_issues_a_fresh_one` and
`test_auth.py::test_cli_token_bakes_the_active_org_and_works_as_a_BARE_bearer`.

### Commit 8: signup and team creation

Symbols from `api.py`:

- DTOs: `UserIn`, `OrgIn`
- helpers: `_slugify`, `_unique_slug`, `_make_org_membership`, `_grant_signup_promo`,
  `_ad_attribution_from`, `_utm_attribution_from`, `_stamp_utm`, `_redeem_referral`
- endpoints: `register_user`, `create_org`, `list_orgs`

The application signup unit owns the multi-domain promotional grant and commit. Org slug and membership
rules belong to governance. Existing commits/rollbacks listed in section 4 are preserved during movement.

Journey: `test_orgs_mgmt.py::test_create_org_returns_a_working_token`; email-first zero-team creation is
also covered by the OTP journey.

### Commit 9: invite lifecycle

Symbols from `api.py`:

- `InviteIn`, `AcceptIn`, `INVITE_TTL_DAYS`
- `create_invite`, `accept_invite`, `my_invites`, `accept_my_invite`, `list_invites`, `revoke_invite`

Dependencies moved with this unit: `_require_admin_of`, `_known_access_names`,
`_normalize_tool_access`, `_normalize_project_access`. If those normalizers would create a premature
governance-to-tools write edge, expose them as validation reads over the shared schema only.

Journey: `test_orgs_mgmt.py::test_invite_and_accept_new_user`, plus
`test_invites_mine.py::test_invite_seen_and_accepted_without_code`.

### Commit 10: member management and limits

Symbols from `api.py`:

- DTOs: `RoleIn`, `CapIn`, `AccessIn`
- `_require_admin_of`, `_require_owner_of`, `_count_owners`, `_known_tool_names`,
  `_known_access_names`, `_normalize_tool_access`, `_used_today_by_user`, `count_today`,
  `_day_start_utc`, `_drop_member_deny_rules`
- `list_members`, `set_member_cap`, `set_member_access`, `my_usage`, `remove_member`,
  `set_member_role`, `leave_org`, `delete_org`, `_cascade_delete_org`

Journey: `test_orgs_mgmt.py::test_transfer_ownership_then_demote`,
`test_access.py::test_set_access_validates_and_collapses`, and
`test_orgs.py::test_org_delete_clears_EVERY_org_scoped_table`.

### Commit 11: machine identity journey

Symbols from `api.py`:

- `AgentIn`, `_public_demo_email`, `_agent_email`, `_agent_name`
- `create_public_token`, `delete_public_token`, `create_agent`, `list_agents`, `agent_checkin`,
  `list_observed_agents`, `revoke_agent`
- `_norm_client` and `_client_of` move to a neutral cross-cutting caller-metadata module because call
  and run still use them. Governance imports that neutral module, not a sibling domain.

Journey: `test_agent_identity.py::test_agent_can_call_but_cannot_act_as_a_user` and
`test_agent_identity.py::test_rotate_kills_the_previous_token`.

### Commit 12: projects, policy, settings, usage, and budgets

Symbols from `api.py`:

- project: `ProjectIn`, `_project_view`, `_normalize_project_access`, `_resolve_project`,
  `create_project`, `list_projects`, `delete_project`
- deny: `DenyRuleIn`, `_deny_view`, `_deny_match`, `_org_deny_rules`, `_enforce_deny`,
  `create_deny_rule`, `list_deny_rules`, `list_cli_deny`, `delete_deny_rule`
- org settings/usage: `OrgSettingsIn`, `get_org_settings`, `set_org_settings`, `org_usage`,
  `_usage_rollup`, `list_tag_keys`, `usage_by_tag`
- budgets: `TagBudgetIn`, `_tag_budget_view`, `list_tag_budgets`, `set_tag_default`,
  `set_tag_budget`, `delete_tag_budget`

Call/runtime consumers import public governance reads and gates from the new domain. The call functions
`_primary_dim_of`, `_budget_dims_of`, `_effective_daily_cap`, `_validate_tag_pair`, and `_tag_budget`
remain where they currently execute until Stage 4 unless moving them is required to avoid a reverse
import. If required, stop for review rather than creating `domain.governance -> api`.

Journey: `test_projects.py::test_project_scope_gates_listing_and_calling`,
`test_deny_rules.py::test_deleting_the_rule_restores_the_call`, and the org settings/budget journey in
`test_tag_billing.py`.

### Commit 13: admin identity and org controls

Symbols from `api.py`:

- `BoolIn`, `_is_last_active_superadmin`, `_cascade_delete_org`, `_drop_member_deny_rules`
- `admin_set_superadmin`, `admin_suspend_user`, `admin_delete_user`, `admin_suspend_org`,
  `admin_delete_org`

The router block attaches between existing admin reads and existing admin reports, at 198 through 202.
Shared governance/identity commands are called directly; admin router does not import `api.py`.

Journey: corresponding mutation tests in `tests/test_admin.py`, including last-active-superadmin and
delete cascade coverage.

### Commit 14: secret CRUD resource journey

Symbols from `api.py`:

- DTOs: `SecretIn`, `SecretUpdate`
- `create_secret`, `list_secrets`, `update_secret`, `delete_secret`
- `_require_not_live_demo_secret`, `_validate_bundle_id`, `_secret_view`, `_visible_secret_ids`,
  `_can_manage`, `_require_can_register`

`_can_manage` and `_require_can_register` are governance/identity authorization rules and should be
public commands from the already-extracted domain, not copied into resources.

Journey: `test_step3_crud_auth_audit.py::test_secret_crud_and_owner_stamp` and
`test_step3_crud_auth_audit.py::test_cannot_delete_secret_in_use`.

### Commit 15: tool CRUD resource journey

Symbols from `api.py`:

- DTOs: `ToolIn`, `ToolUpdate`
- helpers: `_host_of`, `_normalize_scheme`, `_flat_binding`, `_require_not_live_demo_tool`,
  `_require_public_base_url`, `_validate_bindings`, `_require_secret_ownership`,
  `_validate_cli_profile`, `_validate_cli_secrets`, `_allowed_server_bins`, `_tool_view`
- endpoints: `create_tool`, `list_tools`, `get_tool_by_name`, `update_tool`, `delete_tool`

Governance ACL reads `_tool_allowed`, `_project_allowed`, `_tool_usable`, `_require_tool_access`, and
`_require_tool_use` remain public governance rules because the call runtime also consumes them. If the
review assigns resource authorization to `domain.tools` instead, activate the allowed
`tools -> connections` edge when that package appears.

Journey: `test_step3_crud_auth_audit.py::test_tool_crud_and_duplicate_name`, plus project/access tests
that prove listings do not widen.

### Commit 16: skill and bundle resource journey

Symbols from `api.py`:

- DTOs: `BundleUpdate`, `SkillSecretIn`, `SkillToolIn`, `SkillIn`, `SkillFileIn`, `SkillAnalyzeIn`,
  `SkillImportIn`
- `_sanitize_bundle_files`, `_register_skill_bundle`, `_check_upload_size`,
  `_materialize_skill_files`, `_scan_uploaded_skills`, `_bundle_allowed`, `_bundle_view`
- `register_skill`, `analyze_skill_folder`, `import_skill_folder`, `list_bundles`,
  `get_bundle_by_name`, `get_bundle`, `update_bundle`, `delete_bundle`

Journey: `test_skill_upload.py::test_import_registers_recipe_and_tool` and
`test_skill_upload.py::test_reimport_is_idempotent_not_500`.

### Commit 17: OAuth and pasted-token connection establishment

Symbols from `api.py`:

- DTOs: `OAuthStartIn`, `TokenConnectIn`
- `_free_connection_name`, `_provider_bindings`, `_autoprovision_provider_tool`,
  `_upsert_provider_extra_tools`, `_provider_tool_examples`, `_record_connected_identity`, `_dig`
- `oauth_providers_list`, `oauth_start`, `oauth_callback`, `connect_with_token`, `oauth_status`
- `_backfill_provider_extra_tools`, with the `api.py` compatibility re-export required by lifespan

The application connect use case owns the session and commit sequence and calls public tools and
connections commands. The startup backfill remains a separately named startup operation, not an excuse
to add writes to dataplane request handling.

Journey: `test_connections.py::test_registry_connect_autoprovisions_a_callable_tool` and
`test_oauth_connect.py::test_callback_exchanges_code_and_creates_oauth_secret`.

### Commit 18: connection resources, extra credentials, revoke, and health

Symbols from `api.py`:

- DTOs: `ResourceRefIn`, `ExtraCredentialIn`
- `_owned_connection`, `_enrich_resource_labels`
- `list_connections`, `connection_resources`, `set_connection_resource`, `set_extra_credential`,
  `revoke_connection`, `run_health`, `get_health`

Journey: `test_connections.py::test_set_and_read_back_the_selected_resource`,
`test_connections.py::test_revoke_removes_the_connection`, and
`test_connection_health.py::test_a_failed_refresh_records_why`.

## 3. Importers and compatibility strategy

### 3.1 `api.py`

`api.py` remains the ordered route-table host and `treg.api:app` compatibility entry point. During the
sequence it imports each router block and attaches it at the old position. It may re-export symbols
that have known test/runtime imports, but moved routers and domains never import `api.py`.

Known direct compatibility names to retain or update deliberately:

- `api.Caller`, `api.require_identity`, `api.require_member`, `api.require_superadmin`, and the rest
  pinned by `tests/test_router_dependencies.py`
- `api._deny_match` imported by `tests/test_deny_rules.py`
- `api.count_today` imported by `tests/test_usage_caps.py`
- OTP and CLI constants imported inside auth tests
- `api._backfill_provider_extra_tools` invoked by bootstrap manifests and
  `tests/test_connections.py`
- `api.app`, which is non-negotiable

Compatibility re-exports are wiring only. New code imports the owning module directly.

### 3.2 Call/runtime consumers that remain in `api.py`

These moved governance/resource symbols still serve the later call stage and therefore need direct
owner imports into `api.py`: caller resolution, tool/project ACLs, deny enforcement, visible secret ids,
daily caps, client attribution, tool/secret views, resource URL validation, and connection provider
binding helpers. This allowed direction is legacy API -> domain/application, never the reverse.

### 3.3 Router import rule

The existing `Routers do not depend on the legacy API module` contract must stay green throughout.
Every new router imports identity, application, domain, DB/model types, or neutral helpers directly.
No callback rebinding to an `api` function is allowed.

## 4. Session-discipline audit

Current code violates the target discipline in many Stage 3 flows. This is recorded, not repaired as a
side effect of mechanical movement.

Auth/application functions that currently receive a request session and commit or roll back inside the
handler/helper include:

- `auth_github_callback`, `auth_google_callback`
- `auth_email_start`, `auth_email_verify`, `auth_invite_signin_confirm`
- `oauth_register`, `_resolve_oauth_client`, `oauth_authorize_approve`, `_refresh_grant`,
  `oauth_revoke`, `oauth_token`
- `auth_revoke_tokens`, `oauth_grants`, `oauth_grant_set_team`
- `_find_or_create_user` rolls back on a uniqueness race

Commit 7B reloads the authenticated user by id inside `revoke_identity_tokens`. A concurrent user
deletion in that narrow window raises `IdentityLookupError`; the old shared-session handler could mint
a token from its stale ORM object. The refusal is preferable and the microsecond-scale race needs no
compatibility path.

Governance/resource/connection functions with the same debt include:

- `register_user`, `create_org`, create/accept/list/revoke invite paths
- org settings, budgets, member access/lifecycle, public tokens, agents, projects, pins, and deny rules
- secret/tool/bundle writes and `_register_skill_bundle`
- OAuth start/callback, connection discovery/resource writes, token connect, extra credentials, revoke
- `_backfill_provider_extra_tools`

Domain functions introduced by the move must not gain new commit/rollback calls. Existing commits should
remain in application/router orchestration until a dedicated behavior-neutral transaction extraction is
approved. In particular, `_refresh_grant` cannot move into the identity leaf because it commits.

## 5. Monkeypatch and source-comparison audit

Full-repo scans used:

- `monkeypatch.setattr` / `monkeypatch.delattr`
- `mock.patch` / `patch(...)`
- imports of `treg.api`, `treg.session`, `treg.mcp_oauth`, and `routers.dependencies`

There are no current monkeypatch targets on the Stage 3 handler/helper symbols, except compatibility
module consumers noted below:

- connection tests import `treg.api as A` and call `A._backfill_provider_extra_tools`; preserve or
  update that target in the connection commit.
- MCP OAuth metadata tests patch `health.safe_webhook_url`, `health.host_is_public`, and
  `httpx.AsyncClient.get`, not a moved module attribute. Their lazy-import target remains effective.
- many call tests patch `api.relay`, `_resolve_call`, `_secret_renderings`, and call billing helpers;
  none is in this Stage 3 move and none may change.
- `test_single_user.py` and security tests patch `api.get_settings`; the single-user bootstrap is not
  a Stage 3 unit and stays untouched.

Before every commit, repeat a symbol-specific scan for every moved name, not only this checkpoint scan.
For exact-source review, capture each AST function/class node including decorators from its parent commit
and compare it with the destination. Constants and load-bearing comments are reviewed as raw block text.
The only allowed differences are new module docstrings/imports and explicitly reviewed wiring aliases.

## 6. E2E journey gate before movement

All five candidate journeys already exist before any Stage 3 movement:

| Journey | Existing pre-move test |
|---|---|
| Email OTP in dev mode | `test_auth_email.py::test_first_login_registers_user_with_no_org_then_reuses_identity` |
| CLI pairing start/poll/approve/token | `test_login_page.py::test_email_door_completes_the_handshake` |
| MCP OAuth register/authorize/token/validate | `test_mcp_oauth.py::test_the_whole_flow_end_to_end` |
| Org create and member management | `test_orgs_mgmt.py::test_create_org_returns_a_working_token` plus invite/member tests |
| Connection establishment | `test_connections.py::test_registry_connect_autoprovisions_a_callable_tool` |

No journey-prep commit appears necessary. Before Commit 1, run these exact tests together on the parent
and record the green result. Before and after each movement commit, run the journey assigned above on the
same database backend. Full SQLite and the agreed Postgres subset remain the commit review gate.

## 7. Domain import matrix activation

Identity is a leaf and imports no sibling domain. When later Stage 3 packages appear:

- governance may import identity
- tools may import connections
- every other sibling edge is forbidden
- connections may not import tools, so connection auto-provisioning must compose through
  `application.connect`, not call a tools domain implementation backward

Activate contracts incrementally:

1. identity leaf in Commit 1, subject to the nonexistent-module behavior decision
2. governance contract when governance appears, allowing only `governance -> identity`
3. tools/resources contract when tools appears, allowing only `tools -> connections`
4. connections contract when connections appears, with no sibling imports

An import-linter contract is not a substitute for table-write ownership. The Stage 3 context update must
record that governance owns org/membership/project/deny/settings/budget writes, connections owns secret
credential state, and tools owns tool/bundle writes, while the existing money and audit exceptions remain.

## 8. Snapshot and verification contract

All four files are immutable in Stage 3:

- `tests/snapshots/routes.json`
- `tests/snapshots/openapi.json`
- `tests/snapshots/composition.json`
- `tests/snapshots/lifespan.json`

No regeneration command is permitted. Every commit renders all four in memory and compares bytes with the
committed files. A diff means route attachment order, decorator metadata, dependency identity, role ownership,
or lifespan compatibility changed and the design is wrong.

Per commit:

1. run the unit's existing E2E journey before movement
2. perform byte-identical symbol movement only
3. update monkeypatch/import targets in the same commit
4. run the same E2E journey after movement
5. run `uv run --frozen lint-imports`, expecting the kept count to rise with approved domain contracts
6. run full SQLite and the Stage 3 Postgres subset
7. update `architecture/composition.md`, `interface/api.md`, and each source-owning fragment in the same
   commit; add new fragments for application/domain packages where the map has no owner
8. run the context drift script before push

`bootstrap` route ownership keys remain path/method/function-name based and must not change. If moving a
handler makes a key change appear necessary, stop for review. The deployed `treg.api:app` remains the all-role
entry point. No snapshot change is an accepted outcome.

## 9. Parallel-main merge constraint

PR #205 changes `_legacy_host_redirect`, `_security_headers`, and three middleware registration lines in
`bootstrap.py`. None belongs to a Stage 3 movement unit. When it lands, merge `main` into this branch as the
brief directs, do not rebase, and resolve by taking main's middleware implementation intact. Re-run all four
in-memory snapshot comparisons before resuming the next unit.
