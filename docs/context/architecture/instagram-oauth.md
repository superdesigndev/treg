---
title: Instagram OAuth — direct Login and optional Facebook Page tools
status: built; Meta configuration and live verification pending
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
  - src/treg/web/index.html
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

`instagram` is one catalog provider with two separate grants:

- `instagram-login` uses an Instagram User token and `graph.instagram.com`. While its registry review
  key is pending, its connect copy says that only app-role accounts can complete it.
- `facebook-page` uses a Facebook Page token and `graph.facebook.com`. The selected Instagram
  Professional account must be linked to that Page.

`TREG_OAUTH_REVIEW_PENDING` controls the rollout without a code edit. The generic registry rules give
these three states:

- `instagram-login,page-messages`: plain Connect uses Page `page-tools`; warnings and the optional
  Page-message choice remain visible for reviewers and app roles.
- `instagram-login`: plain Connect uses full `page-messages`; its warning and extra Page permission
  choice disappear.
- empty: plain Connect returns to direct Instagram `manage`; all review warnings disappear.

The grants have separate `Secret` rows, tool names, scopes, tokens, expiry, health, and resource
state. A direct grant does not satisfy a Page-only endpoint. A Facebook Pages connection is also
separate and never creates an Instagram grant.

The `facebook-page` method has two cumulative capabilities. `page-tools` contains the approved Page
permissions for core Instagram actions plus hashtag, discovery, mention/tag, shopping, and recent
search tools. It does not request `instagram_manage_messages` or `pages_messaging`. `page-messages`
adds those message permissions. Method metadata supplies their labels, help, action copy, and
permission-card details; the dashboard contains no Instagram-specific branch. Missing-scope
guidance selects the smallest capability that satisfies the endpoint.

Old Instagram grants used Facebook Login. Migration `0010` marks them as `facebook-page` without
reading token material. Runtime metadata also treats an empty method on an old Instagram row as
`facebook-page`, so rolling upgrades keep working. Shared call and reconnect code asks the provider
registry for this legacy value; neither service contains an Instagram branch.

## Verified endpoint matrix (2026-09-01)

Legend:

- **D** = Instagram Login on `https://graph.instagram.com/v25.0` with an Instagram User token.
- **P** = Facebook Page authorization on `https://graph.facebook.com/v25.0` with the selected
  Page token.
- `IG` = the Instagram Professional account id. `Page+IG` = a linked Page and its Instagram
  Professional account.
- For a D+P row, the listed `instagram_business_*` scope is the direct scope. The Page profile
  maps it to its `instagram_*` equivalent and adds `pages_read_engagement`. Messaging also needs
  `pages_messaging`.

| Endpoint ID | Method and path | Grant | Required scope(s) | Resource | Token |
|---|---|---|---|---|---|
| `instagram.instagram.user.profile` | GET `/{ig_user_id}` | D+P | basic | IG | method-specific |
| `instagram.instagram.user.posts` | GET `/{ig_user_id}/media` | D+P | basic | IG | method-specific |
| `instagram.instagram.post.comments` | GET `/{ig_media_id}/comments` | D+P | basic, manage comments | IG | method-specific |
| `instagram.instagram.account.insights` | GET `/{ig_user_id}/insights` | D+P | basic, manage insights | IG | method-specific |
| `instagram.instagram.media.insights` | GET `/{ig_media_id}/insights` | D+P | basic, manage insights | IG | method-specific |
| `instagram.x.user-messages` | GET D `/{ig_user_id}/conversations`; P `/{page_id}/conversations` with `platform=instagram` | D+P | basic, manage messages | IG / Page+IG | method-specific |
| `instagram.instagram.message.send` | POST D `/{ig_user_id}/messages`; P `/{page_id}/messages` | D+P | basic, manage messages | IG / Page+IG | method-specific |
| `instagram.instagram.media.container.create` | POST `/{ig_user_id}/media` | D+P | basic, content publish | IG | method-specific |
| `instagram.instagram.media.container.status` | GET `/{ig_container_id}` | D+P | basic, content publish | IG | method-specific |
| `instagram.instagram.post.publish` | POST `/{ig_user_id}/media_publish` | D+P | basic, content publish | IG | method-specific |
| `instagram.x.comment-delete` | DELETE `/{ig_comment_id}` | D+P | basic, manage comments | IG | method-specific |
| `instagram.x.comment` | GET `/{ig_comment_id}` | D+P | basic, manage comments | IG | method-specific |
| `instagram.x.comment-replies` | GET `/{ig_comment_id}/replies` | D+P | basic, manage comments | IG | method-specific |
| `instagram.x.comment-reply-create` | POST `/{ig_comment_id}/replies` | D+P | basic, manage comments | IG | method-specific |
| `instagram.x.comment-hide` | POST `/{ig_comment_id}` with boolean `hide` | D+P | basic, manage comments | IG | method-specific |
| `instagram.x.media` | GET `/{ig_media_id}` | D+P | basic | IG | method-specific |
| `instagram.x.media-children` | GET `/{ig_media_id}/children` | D+P | basic | IG | method-specific |
| `instagram.x.media-comment-create` | POST `/{ig_media_id}/comments` | D+P | basic, manage comments | IG | method-specific |
| `instagram.x.user-content-publishing-limit` | GET `/{ig_user_id}/content_publishing_limit` | D+P | basic, content publish | IG | method-specific |
| `instagram.x.user-live-media` | GET `/{ig_user_id}/live_media` | D+P | basic | IG | method-specific |
| `instagram.x.user-stories` | GET `/{ig_user_id}/stories` | D+P | basic | IG | method-specific |
| `instagram.x.hashtag-search` | GET `/ig_hashtag_search` | P | `instagram_basic`, `pages_read_engagement` | Page+IG | Page |
| `instagram.x.hashtag` | GET `/{ig_hashtag_id}` | P | `instagram_basic`, `pages_read_engagement` | Page+IG | Page |
| `instagram.x.hashtag-recent-media` | GET `/{ig_hashtag_id}/recent_media` | P | `instagram_basic`, `pages_read_engagement` | Page+IG | Page |
| `instagram.x.hashtag-top-media` | GET `/{ig_hashtag_id}/top_media` | P | `instagram_basic`, `pages_read_engagement` | Page+IG | Page |
| `instagram.x.catalog-product-search` | GET `/{ig_user_id}/catalog_product_search` | P | basic, shopping products, Page read | Page+IG | Page |
| `instagram.x.user-mentioned-comment` | GET `/{ig_user_id}/mentioned_comment` | P | basic, Page read | Page+IG | Page |
| `instagram.x.user-mentioned-media` | GET `/{ig_user_id}/mentioned_media` | P | basic, Page read | Page+IG | Page |
| `instagram.x.user-product-appeal` | GET `/{ig_user_id}/product_appeal` | P | basic, shopping products, Page read | Page+IG | Page |
| `instagram.x.user-recently-searched-hashtags` | GET `/{ig_user_id}/recently_searched_hashtags` | P | basic, Page read | Page+IG | Page |
| `instagram.x.user-tags` | GET `/{ig_user_id}/tags` | P | basic, Page read | Page+IG | Page |
| `instagram.x.user-business-discovery` | GET `/{ig_user_id}` with required `fields=business_discovery...` | P | basic, Page read | Page+IG | Page |

The verified result is 21 endpoints that support both methods and 11 Page-only endpoints. This is
not the earlier assumed 23/9 split.

Every extended Instagram endpoint declares its path, query, and body inputs in the catalog. This is
the contract used by the dashboard form, generated CLI command, API example, and agent prompt. The
comment moderation write accepts `hide=true` and `hide=false`, so the same endpoint hides and unhides.

Official sources used for the audit:

- [Instagram API with Instagram Login](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/)
- [Instagram API with Facebook Login](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/)
- [Instagram Login permissions](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/)
- [Content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/)
- [Comment moderation](https://developers.facebook.com/docs/instagram-platform/comment-moderation/)
- [Insights](https://developers.facebook.com/docs/instagram-platform/insights/)
- [Messaging conversations](https://developers.facebook.com/docs/messenger-platform/instagram/features/conversation/)
- [Send messages](https://developers.facebook.com/docs/messenger-platform/instagram/features/send-message/)
- [Hashtag search](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/hashtag-search/)
- [Business discovery](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/business-discovery/)

## Connection and health flow

Instagram Login exchanges the short token for a renewable long-lived token. The refresh metadata
uses `ig_refresh_token` and the Instagram refresh endpoint. The callback then requests
`/me?fields=user_id,username`. It stores that one authorized professional account as the selected
resource and creates the direct tool.

If the identity response has no usable account, the secret remains visible with
`health=setup_required`. No tool is created. The detail tells the user to confirm that the account
is a Business or Creator account and connect again. The provider requests run after the database
session closes.

The Page grant keeps the existing Page and Business discovery. The user selects the linked
Instagram account. Treg derives and stores the Page token inside the encrypted OAuth blob. The
dashboard's **Add account** action presents the two grants as a single-choice method picker. Its
recommended method, conditional review copy, and effective capability choices all come from the same
registry review state. With both keys pending, Facebook Page tools is recommended and opens a core or
messaging choice. After Page messaging approval, that method continues directly with the full Page
grant. After direct approval, Instagram Login is recommended and opens the usual read/post/manage
capability picker. Existing connection rows retain their stored method and widest granted capability
when they reconnect.

Before a catalog call, resolution selects a grant by endpoint provider and authorization method.
It checks token expiry, scopes, and the selected resource before any upstream call. A missing grant
returns HTTP 428 with stable fields: error, provider, endpoint id, method, capability, scopes,
message, CLI command, and dashboard action. Both MCP surfaces return this object unchanged inside
the call result body.

Catalog availability is authorization-method-aware too. A dual-method endpoint is connected when
either compatible grant exists; a Page-only endpoint is connected only when the `facebook-page`
grant exists. The dashboard derives this from each endpoint's `authorization_methods` and each
connection's stored `authorization_method`, rather than special-casing endpoint ids. Its access
dry-run preserves the selected method's registry guidance, including the Page-only
`--capability page-tools` command and action label. A core Page grant does not mark a messaging
endpoint as callable; its access check returns the `page-messages` upgrade command.

For a dual-method endpoint, callers can select `instagram-login` or `facebook-page`. The dashboard
shows a selector; the CLI uses `--authorization-method`; MCP tools use the
`authorization_method` argument; and direct API calls use `X-Treg-Authorization-Method`. Selection
also controls which path and inputs are shown. If omitted, the endpoint's first declared method is
the deterministic default (Instagram Login for shared endpoints). The control header is consumed by
treg and never relayed upstream. Shared clients accept method ids and display labels from registry
metadata. `POST /oauth/start` returns the selected method description as `connect_guidance`, which
lets the CLI explain the grant without provider-specific code.

When several stored grants match one method, catalog fallback selects the newest row. Tool-grant
resolution loads the provider's secrets once and matches bindings from that set, rather than loading
one secret for every binding.

## Meta configuration and live verification

No repository task changes Meta settings. A human must do these steps:

1. Open [Meta App Dashboard](https://developers.facebook.com/apps/) and select the app that will
   own Instagram Login.
2. Add or open **Instagram > API setup with Instagram login > Business login settings**.
3. Set the production redirect URI to `https://treg.to/oauth/callback`. For local testing, set
   `https://<your-tunnel-host>/oauth/callback` only while that tunnel is active.
4. Copy the separate Instagram App ID and Instagram App Secret into local `.env` as
   `TREG_INSTAGRAM_CLIENT_ID` and `TREG_INSTAGRAM_CLIENT_SECRET`. Do not put these values in chat or
   source control.
5. Request Advanced Access for `instagram_business_basic`,
   `instagram_business_manage_insights`, `instagram_business_content_publish`,
   `instagram_business_manage_comments`, and `instagram_business_manage_messages` when users will
   connect accounts that the app owner does not manage.
6. Keep the existing Meta app credentials in `TREG_META_CLIENT_ID` and
   `TREG_META_CLIENT_SECRET` for `page-tools`. Confirm its Page and Instagram permissions in App
   Review, including `instagram_shopping_tag_products` if product tools remain enabled.
7. Verify one Business account and one Creator account with no linked Page through the direct flow.
   Then verify a linked Page account through `page-tools`. Run a safe read from each class. Test
   publishing and messaging only with deliberate test content and recipients.

App Review is required for production access to accounts that the app owner does not own or manage.
Development-mode tests with app-role accounts do not prove that review is complete. Change
`TREG_OAUTH_REVIEW_PENDING` only after Meta marks the related access as approved.
