---
title: Instagram OAuth - direct Login and optional Facebook Page tools
status: built
sources:
  - scripts/catalog_ingest.py
  - src/treg/application/call/access.py
  - src/treg/application/call/resolve.py
  - src/treg/application/call/service.py
  - src/treg/catalog/instagram.yaml
  - src/treg/catalog/instagram.extended.yaml
  - src/treg/cli.py
  - src/treg/domain/catalog/store.py
  - src/treg/domain/connections/authorization.py
  - src/treg/domain/connections/oauth_flow.py
  - src/treg/infra/oauth_exchange.py
  - src/treg/mcp.py
  - src/treg/routers/call.py
  - frontend/src/state/connections.js
  - frontend/src/pages/ProviderPage.vue
  - src/treg/alembic/versions/0010_oauth_authorization_method.py
  - tests/test_instagram_oauth_architecture.py
related:
  - architecture/auth-secrets.md
  - architecture/catalog.md
  - architecture/proxy-model.md
  - architecture/mcp-oauth.md
  - interface/api.md
  - interface/cli.md
  - interface/dashboard.md
---

# Instagram OAuth

## Contract

`instagram` is one catalog provider with two separate grants (authorization methods):

- `instagram-login`: an Instagram User token on `graph.instagram.com`.
- `facebook-page`: a Facebook Page token on `graph.facebook.com`; the selected Instagram
  Professional account must be linked to that Page. Its capabilities are cumulative: `page-tools`
  (no messaging permissions) and `page-messages` (adds `instagram_manage_messages` and
  `pages_messaging`).

Which endpoints each method serves is declared per row (`authorization_methods`) in
`src/treg/catalog/instagram*.yaml`; Meta serves hashtag, mention/tag, shopping and business
discovery endpoints only through the Page method.

The grants have separate `Secret` rows, tool names, scopes, tokens, expiry, health and resource
state. A direct grant never satisfies a Page-only endpoint, and a Facebook Pages connection never
creates an Instagram grant.

Everything Instagram-specific lives in the provider registry (`oauth_providers.py`): method labels,
help, capability details, review copy and the legacy method. Shared call, reconnect and dashboard code
must not branch on Instagram or on endpoint ids; they read method metadata.

Old Instagram grants used Facebook Login. Migration `0010` marks them `facebook-page` without reading
token material, and runtime treats an empty method on an Instagram row as the registry's
`legacy_authorization_method`, so rolling upgrades keep working.

## Review rollout

Meta App Review state is configuration, not code: `TREG_OAUTH_REVIEW_PENDING` lists the methods or
capabilities still under review, and the registry derives the recommended method, review warnings and
capability choices from it:

- `instagram-login,page-messages`: plain Connect uses Page `page-tools`; warnings and the optional
  Page-message choice stay visible for reviewers and app-role accounts.
- `instagram-login`: plain Connect uses full `page-messages`.
- empty: plain Connect uses direct Instagram `manage`; all review warnings disappear.

Change it only after Meta marks the related access approved. Development-mode tests with app-role
accounts do not prove review is complete, and production access to accounts the app owner does not
manage requires Advanced Access for each `instagram_business_*` permission the direct method requests.

## Connection and health

- Instagram Login exchanges the short token for a renewable long-lived token (refreshed with
  `ig_refresh_token`), then reads `/me?fields=user_id,username` and stores that one professional
  account as the selected resource. With no usable account the secret stays visible with
  `health=setup_required` and no tool is created. Provider requests run after the DB session closes.
- The Page grant reuses Page and Business discovery; the derived Page token is stored inside the
  encrypted OAuth blob.
- Reconnecting keeps a row's stored method and its widest granted capability.

## Calling

- Resolution selects a grant by provider and authorization method and checks expiry, scopes and
  selected resource before any upstream call. A missing grant returns HTTP 428 with stable fields
  (error, provider, endpoint id, method, capability, scopes, message, CLI command, dashboard action);
  both MCP surfaces return that object unchanged in the call result. Missing-scope guidance names the
  smallest capability that satisfies the endpoint, so a core Page grant on a messaging endpoint gets the
  `page-messages` upgrade command.
- A dual-method endpoint is connected when either compatible grant exists. Callers pick a method with
  `X-Treg-Authorization-Method` (API), `--authorization-method` (CLI) or `authorization_method` (MCP);
  the default is the endpoint's first declared method. The header is consumed by treg and never
  relayed upstream.
- When several grants match one method, the newest row wins.

## Setting up the Meta apps

No repository code changes Meta settings. An operator:

1. In the Meta App Dashboard, under **Instagram > API setup with Instagram login > Business login
   settings**, sets the redirect URI to `<public_url>/oauth/callback`.
2. Puts the Instagram App ID and secret (distinct from the Meta app's) in
   `TREG_INSTAGRAM_CLIENT_ID` and `TREG_INSTAGRAM_CLIENT_SECRET`.
3. Keeps the Meta app credentials in `TREG_META_CLIENT_ID` and `TREG_META_CLIENT_SECRET` for the
   Page method, with its Page and Instagram permissions in App Review (including
   `instagram_shopping_tag_products` while product tools are enabled).
4. Verifies a Business and a Creator account with no linked Page through the direct flow, and a
  linked Page account through `page-tools`, with a safe read each. Publishing and messaging are tested
  only with deliberate test content and recipients.
