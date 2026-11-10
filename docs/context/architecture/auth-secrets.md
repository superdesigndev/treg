---
title: Auth & secrets - injectors, encryption, OAuth freshness, health
status: shipped
sources:
  - src/treg/infra/upstream/injectors.py
  - src/treg/infra/upstream/ssrf.py
  - src/treg/crypto.py
  - src/treg/oauth.py
  - src/treg/domain/connections/__init__.py
  - src/treg/domain/connections/authorization.py
  - src/treg/domain/connections/oauth_flow.py
  - src/treg/domain/connections/refresh.py
  - src/treg/infra/oauth_exchange.py
  - src/treg/infra/oauth_refresh.py
  - src/treg/oauth_providers.py
  - frontend/src/state/session.js
  - frontend/src/state/keys.js
  - frontend/src/pages/TeamPage.vue
  - src/treg/health.py
  - src/treg/application/connect.py
  - src/treg/alembic/versions/0051_context_dev_tool_host.py
  - src/treg/routers/connections.py
  - src/treg/routers/resources.py
  - src/treg/domain/tools/__init__.py
  - src/treg/domain/tools/bindings.py
  - src/treg/domain/tools/bundles.py
  - src/treg/domain/identity/api_keys.py
  - src/treg/domain/identity/access.py
  - src/treg/routers/api_keys.py
  - tests/test_api_keys.py
  - tests/test_oauth_refresh.py
  - tests/test_key_providers.py
  - src/treg/config.py
related:
  - architecture/proxy-model.md
  - architecture/data-model.md
  - interface/api.md
---

# Auth & secrets

The hard part: match every credential shape a real skill uses, keep it encrypted, and keep OAuth
tokens alive, without the proxy ever branching on provider or shape. Provider-specific connect
facts (probe route, header spelling, quirks) live on the provider's `OAuthProvider` entry and its
catalog file, not here.

## Managed treg API keys

treg API keys authenticate callers; provider secrets authorize upstream services. The stores are
separate. New additional human and agent keys start with `treg_`, are returned once, and are stored
only as SHA-256 hashes plus a safe prefix. A signed identity key has no stored hash; its stable
`default_human` row controls that membership. New human memberships leave the legacy
`Membership.token_hash` empty and return a team-pinned signed Default key, so they create no
`legacy_human` row.

- Authentication loads the key first and the live membership second, so disable and revoke take
  effect without touching role, access, cap or billing data. Revoke is permanent.
- Rotation uses a conditional row update as its cross-process claim, then revokes the old row and
  links it to one new row; a competing request gets 409 rather than a second replacement. Revoked
  rows, key events and Activity snapshots stay for audit; none holds a complete secret.
- The signed Default key rotates in place: its row carries `default_generation`, the token the
  signed `kg` claim, so rotating invalidates only that team's Default key. Default keys cannot be
  revoked (disable is the temporary stop, rotate the replacement). Their `scp=team` signed `org`
  is authoritative, so a conflicting `X-Treg-Org` cannot redirect one team's key to another. Org-less
  `scp=bootstrap` login tokens are seven-day onboarding credentials, not managed keys.
- `last_used_at` is approximate. Authentication commits its read before `api_keys.touch()` schedules
  a best-effort, windowed background update, so requests sharing a key never serialize on a row
  write.
- Every response returning a complete caller credential uses `Cache-Control: no-store`. Owners and
  admins manage other humans' keys except rotation, which only the assigned human may do; admins
  manage agent keys.

## Injectors - the seam (`infra/upstream/injectors.py`)

The proxy calls `inject(headers, params, binding, secret, json_body=...)`, which dispatches on
`binding["injector"]` through the `INJECTORS` registry. Two mechanics: place a string
(`env_injector`, `cli_auth_injector`) or pull a token from a JSON blob first (`secret_file_injector`,
`oauth_injector`, default field `access_token`), then `_place()` renders `binding["format"]` into a
header, query parameter or top-level JSON field. The injected credential overwrites a same-named
caller value. Binding validation accepts only `header`, `query` and `json` and rejects duplicate
target names per location. Adding a shape is one function; the proxy never changes.

A `location: json` binding requires a JSON object body, overwrites only the named top-level fields,
rejects duplicate object keys and recalculates Content-Length. It reserializes the body, so it must
never be used for an upstream that signs or hashes raw body bytes (non-negotiable 4). A binding
whose `format` has no `{secret}` placeholder is a protocol constant (`required_headers`) and is
excluded from secret-evidence scrubbing.

## Encryption (`crypto.py`)

Secret values are Fernet-encrypted at rest with `TREG_SECRET_KEY`; unset, an ephemeral key is used,
so secrets do not survive a restart (`verify_db` refuses that on a real database). Caller tokens are
stored only as SHA-256 hashes. Values are never returned to clients, with one exception below.

## OAuth freshness (`domain/connections/refresh.py`)

`is_refreshable(blob)` (has `refresh_token`, `client_id`, `client_secret`) picks auto or manual mode;
a bare uploaded token is injected as-is and re-uploaded on expiry. `ensure_fresh` is the one refresh
path for the call runtime, health, resource discovery and resource selection; none of them
implements its own lock, exchange or write.

- The read transaction commits before token-endpoint I/O, and the write opens a new short
  transaction, so provider latency holds no pooled connection.
- A single-flight lock per secret id (bounded map) plus a re-check under the lock prevents an
  in-process stampede. The write-back is conditional on the prior ciphertext (`UPDATE ... WHERE
  value = old`), so across processes a second refresh cannot clobber a refresh token the first
  already rotated.
- The adapter updates both `access_token` and `token` so either `secret_field` stays fresh, stamps a
  fallback `expires_at` when the provider omits `expires_in` (else every call refreshes), and treats
  a naive ISO `expiry` as UTC.
- **Dialect snapshots.** The blob records the grant's `client_id_param` (TikTok reads `client_key`)
  and `token_endpoint_auth_method` (X and Pinterest demand HTTP Basic) at mint time, so a refresh
  months later speaks the dialect the grant was minted with. A legacy blob without the method tries
  body auth, then one Basic retry on a 4xx, and stamps whichever worked.
- **treg's own apps.** The blob also snapshots `client_secret` and `token_uri`, and a team member can
  rewrite the blob, so neither is trusted for treg's apps: when the `Secret` row's provider is a
  registry provider and the blob's `client_id` is the one this deployment configures for that
  profile, `oauth_providers.platform_refresh_blob` substitutes the current env secret and the
  profile's own `token_uri`, without writing them back. Any other blob, including one that merely
  names treg's `client_id`, refreshes with only what it stores. Rotating a treg app secret is an env
  change plus a deploy on every service that refreshes.

**Meta App Secret Proof.** treg's Meta app requires App Secret Proof, so Graph refuses any server
call whose user or Page token is not paired with `appsecret_proof` = HMAC-SHA256(app secret, token)
(400, code 100). Profiles on that app declare `appsecret_proof=True` (`facebook`, `meta-ads`,
Instagram's `facebook-page`), and `oauth_providers.appsecret_proof` computes one only when the
token's blob names the `client_id` this deployment configures for that profile and the request goes
to the profile's own API or discovery host. Every request sending such a token carries it: the relay
and health probe (`connection_appsecret_proof`, over the token the binding injects, so a Page call
is proved with the Page token) and the picker, Page-token lookup and resource setup in
`application/connect.py`. Token exchange and refresh send the app secret itself and need no proof. A
team's own Meta app never gets one computed with treg's secret.

**Connect flow.** `consent_url` defaults to `access_type=offline` + `prompt=consent` so a refresh
token comes back; a provider's `auth_params` replaces those defaults entirely (LinkedIn, X, TikTok and
Meta reject `access_type`). PKCE where required; TikTok's hex challenge (`pkce_loopback_hex`) applies
only when `public_url` is localhost, because TikTok treats a localhost redirect as a desktop app.
`long_lived_exchange` swaps Meta's short code-exchange token for its long-lived one, non-fatally.
The callback performs token and identity requests after closing the database session; a required
identity miss produces `setup_required` and no tool.

**Expiry is a separate axis from health.** Health answers "does it work"; expiry answers "how long
will it keep working", which matters for a non-refreshable token that reads healthy until it
silently dies. `expiry_state` returns `fresh|expiring|expired|unknown`; a refreshable credential is
always `fresh` (treg mints on demand), and only an unrenewable one earns a warning.
`connection_view()` is the metadata-only shape with one actionable `needs_reconnect` flag.

## Provider registry (`oauth_providers.py`)

Two ways to connect. **Bring-your-own:** `POST /oauth/start` with a caller-supplied client works for
any OAuth2 provider and is never metered. **Registry:** for providers where treg holds the approved
app, the user consents and supplies nothing; the gating cost on these platforms is the approval (a
Google Ads developer token, Meta App Review), which treg has already cleared. Each entry is a frozen
`OAuthProvider` in `REGISTRY`; treg's client credentials come from the settings it names.

- **`listing()`** (`GET /oauth/providers`) carries every UI-relevant field (configured state,
  plain-English scope labels, authorization methods, consent notice, metering), so the dashboard and
  CLI never map provider or method ids themselves.
  `test_every_requested_scope_has_a_plain_english_label` guards the labels.
- **Scopes are per capability**, cumulative supersets; `default_capability` is the broadest, because
  an agent product needs write eventually and one honest consent screen beats connecting twice.
  Google Tag Manager deliberately omits `tagmanager.delete.containers`, `tagmanager.manage.users` and
  `tagmanager.manage.accounts`: agents can publish and roll back, never delete containers or
  administer access.
- **`consent_notice`** is shown before the consent popup opens. Only the Meta family carries one: the
  shared Meta app is registered as **Crewlet**, a sibling product of the same company (Superdesign Dev
  Inc), and Facebook renders only that bare app name, so without the notice a treg user is asked to
  authorize a stranger. Meta Developer Policy 1.6 wants the relationship disclosed at the point of
  consent, which is why this is not a docs link.
  `test_only_the_meta_family_carries_a_consent_notice` derives the set from `auth_uri == _META_AUTH`.
- **Multi-method providers.** Capability ownership, method profiles, endpoint-method selection and
  scope translation live in `domain/connections/authorization.py`; `authorization_method` is stored
  on `PendingOAuth` and `Secret`; profile copy such as `identity_missing_detail` keeps shared code free
  of provider text. `TREG_OAUTH_REVIEW_PENDING` is the one source for incomplete reviews. See
  [instagram-oauth](instagram-oauth.md).
- **Resource-derived tokens.** A provider can declare a resource-token lookup: on resource selection,
  `select_connection_resource()` resolves the resource's own token (a Facebook Page token) into
  `page_access_token` inside the same encrypted blob, keeping `access_token` for discovery, and the
  tool's binding injects the derived field. Until a resource is selected the root token is bound so
  the probe and picker work. A reconnect replaces the blob, so the callback unselects and re-derives
  after its commit; if the new grant no longer reaches the resource, the connection reports
  `setup_required`. Instagram inbox calls go to the Page (`/{page_id}/conversations?platform=instagram`),
  not the Instagram account id, which Meta answers with error `(#3)`. Discovery and setup HTTP run
  after the read session closes. Listings and views never include the derived token.
- **Discovery** may walk a second listing (`discover_extra_path`): Meta's Business Manager owns
  assets on an agency member's behalf, so `/me/accounts` returns `[]` for Pages they manage daily.
  A failing extra listing reads as "no extra assets". YouTube's `quotaExceeded` maps to the stable
  `youtube_quota_exceeded` code.
- **`platform_billed`** marks providers whose upstream bills the app owner per use, whoever's token
  made the call (X). Gated on `TREG_OAUTH_BILLED_PROVIDERS`; a registry connect is then metered
  through `_billed_marketplace` (`application/call/resolve.py`) on the platform reserve/settle path
  ([money](money.md)). A priced catalog entry wins over the `billed_*` fallback rates, and "priced"
  must be read as *not free*: a `free` block has a falsy `usd`, and treating it as unpriced bills the
  fallback. So every X entry carries a real rate, and `tests/test_marketplace_call.py` asserts the
  published number equals the reserved one. X's owned-read rate is deliberately not a fallback,
  since a registry connect's member is never the app owner. A BYO connect is never metered: the
  callback stamps `secret.provider` only in registry mode, and that attribution is the whole
  detection.
- **Connecting persists the provider's `base_url` and `host` on the team's tool.** Changing the
  registry's base URL affects new connections and direct catalog calls only; a host migration must
  find provider-created tools through their connection binding and leave manually registered tools
  alone.

### Pasted keys (`auth_kind` `token` or `key`)

`token` and `key` share one connect, verify and auto-provision path (`uses_pasted_secret`); a key
provider needs nothing from treg, so it is always configured. The pasted credential rides in a
header, a query parameter or JSON fields (`token_*` fields), with `token_encode="base64"` turning
`login:password` into a Basic blob; the injector's `ensure_base64` re-applies that at call time, so a
raw pair stored by `treg secret add` (which never probes) renders the same header.

The connect probe hits `base_url` + `probe_path` or an absolute `probe_url`, and is free unless
declared otherwise. Many upstreams answer 200 to a bad key, so validity comes from declarative
metadata, never provider branches: HTTP status, `token_verify_field`, `token_ok_field` /
`token_ok_value`, `token_reject_field`, an `ERROR`-prefixed text body, `probe_reject_statuses` (a
status such as 402 can prove the key was recognized), and `probe_deferred_statuses` (a status that
cannot judge a key alone stores it unchecked, never labelled verified). A probe without a free route
declares `probe_cost_micro`: the connect dialog warns from it, and provisioning omits it from the
tool's health check so later health runs never spend the team's wallet. The recurring health check
uses only a free `probe_path`; an absolute `probe_url` is connect-only.

- **Second credentials.** `needs_extra_credential` covers a second value the primary slot cannot
  carry: treg-held (`extra_credential_setting`, Google Ads' developer token) or user-supplied via
  `POST /connections/{id}/extra-credential`, which rebuilds the tool with both bindings. On the
  platform tier the pair's other half is its own setting (`platform_extra_setting`), so a team's own
  pair never rides with treg's half.
- **`required_headers`** are fixed protocol headers sent with the credential on every request and
  probe, turned into constant-format bindings; `_platform_bindings` mirrors them so a platform key
  cannot lose one.
- **`CatalogTarget`** lets a provider approve exact extra HTTPS hosts for the same credential (with
  optional placement overrides). Catalog data cannot add credential destinations: resolution rejects
  an unapproved host before any secret reaches the relay or money is reserved.
- **`extra_tools`** gives split-host vendors one extra tool per host (GA4's admin host lists the
  property ids its data host needs), because `/call/` resolution is per host. The extra binds the
  same secret, upserts on connect, and `_backfill_provider_extra_tools` heals older connections in the
  release upgrade, so a new entry needs no one-off migration. `resource_example` stamps a ready call
  into the tool's examples when a resource is picked (marker `stamped: resource`).

### A versioned path expires on a calendar, not on a deploy

Google Ads puts its API version in the URL and sunsets each major about a year after release. When
it dies every Ads call, including the connect-time account listing, fails with no commit and no test
failure (nothing in the suite makes a live call). A version that never existed returns an HTML 404;
a sunset one returns a JSON 400 `UNSUPPORTED_VERSION`. `POST /health/run` surfaces it the day it
breaks, but nothing schedules it by default. Bump the version in all four places together:
`oauth_providers.GOOGLE_ADS`, `catalog/google-ads.yaml`, `catalog/google-ads.extended.yaml` and
`scripts/catalog_ingest.py:GADS_VERSION`.

## treg's own accounts

A provider's platform key is `TREG_PLATFORM_KEY_<PROVIDER>`; a paired half has its own setting.
`TREG_PLATFORM_ACCOUNTS` adds labelled accounts behind a provider, and a malformed value refuses to
start. Values live only in the environment: a binding names a setting and account label, never a
value, and the async worker builds the same bindings as the call path, so keys never reach task
records, logs or archive evidence ([proxy model](proxy-model.md)).

## Credential health (`health.py`)

`run_all(db, client, org_id=None)` refreshes each OAuth secret and runs each tool's optional probe.

- Filtered to `org_id` so `/health/run` never touches another org's credentials; a super-admin may
  pass `?all_orgs=1`.
- Each tool runs in its own try/except: one malformed tool marks its secrets `unknown` and the batch
  continues.
- Worst status wins within a run; transport errors, 5xx and 429 are `unknown`, never a false
  `invalid` that spams webhooks. Only secrets evaluated this run are notified, and a secret unbound
  from its last tool resets to `unknown`.
- The expiring sweep covers every OAuth secret, not only probed ones, because an unbound healthy
  credential can still be days from silent death.
- `_probe()` merges a binding's query with `copy_add_param`, since httpx `params=` would replace the
  probe path's own query string.
- A provider in `TREG_PAUSED_PROVIDERS` is skipped whole: the connection must be intact when the
  pause lifts. No job deletes or disables a connection after failed checks; removal is a user's
  disconnect only.
- The run also expires abandoned OAuth connects (`OAUTH_PENDING_TTL_MIN`), which hold an encrypted
  client secret and a replayable `state`.
- `_notify()` posts invalid credentials to the owner's `webhook_url`, searching all the owner's
  memberships, then a current org owner's. `webhook_url` is user-set, so `safe_webhook_url` rejects
  non-http(s) and non-globally-routable targets at set time and again before POST (blind-SSRF guard),
  including CGNAT `100.64.0.0/10` and NAT64 prefixes mapping non-global IPv4; see
  [proxy target guards](proxy-model.md#resolution-and-relay-guards).

## Secret kinds and the one release exception

`kind` `param` is a non-secret value (a project id) stored and injected like a secret but never
health-checked. Every kind is encrypted and never returned by `_secret_view`. The only path that
returns a value is a local-run grant ([local-run](local-run.md)): member+ only, owner-opt-in per
tool, audited, and for OAuth only the short-lived access token (an OAuth `secret_field` allow-list
keeps `refresh_token` and `client_secret` on the server). Even then the CLI runs as a dedicated
`treg-run` user, so the value is unreadable by the member's uid.

**Ownership boundary.** A member may only bind or inject a secret they own (`domain/tools/bindings.py`,
`require_secret_ownership`); admins and owners may wire shared-key tools. This stops a member
laundering a teammate's key into a tool they control and exfiltrating it through `base_url` or
`/grant`. Editing a tool grandfathers secrets already on it, so re-saving an admin-wired tool does
not lock its owner out. A `/grant` returning a secret the caller does not own requires the runner
proof (`X-Treg-Run-Proof` == `TREG_RUN_PROOF`, held only by the root-installed runner), so a direct
member call cannot read someone else's key.

A tool's `base_url` is checked against the internal-address block-list (including numeric IP
encodings) at registration, and the proxy re-resolves the host at call time
(`infra.upstream.ssrf.host_is_public`, gated by `proxy_ssrf_check`), so DNS rebinding does not
bypass it. `routers.resources._require_public_base_url` also rejects a `base_url` pointing back at
treg itself, so a team uses treg's tools by id instead of proxying treg through treg.
