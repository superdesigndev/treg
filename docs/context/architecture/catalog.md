---
title: Endpoint catalog — what you can DO with a connected key, and which provider should do it
status: shipped
sources:
  - src/treg/catalog/contracts.yaml
  - src/treg/catalog/adapters.yaml
  - src/treg/catalog/examples/findymail.search.business-profile.json
  - src/treg/domain/catalog/routing/__init__.py
  - src/treg/domain/catalog/routing/contracts.py
  - src/treg/domain/catalog/routing/paths.py
  - src/treg/domain/catalog/routing/plan.py
  - src/treg/domain/catalog/routing/synthetic.py
  - src/treg/application/call/route.py
  - tests/test_routing.py
  - .github/workflows/catalog-drift.yml
  - scripts/catalog_drift.py
  - scripts/catalog_ingest.py
  - scripts/catalog_validate.py
  - src/treg/catalog/aliases.yaml
  - src/treg/catalog/fx.yaml
  - src/treg/catalog/aviato.yaml
  - src/treg/catalog/crustdata.yaml
  - src/treg/catalog/examples/aviato.companies.acquisitions.json
  - src/treg/catalog/examples/aviato.companies.employees.json
  - src/treg/catalog/examples/aviato.companies.enrich.bulk.json
  - src/treg/catalog/examples/aviato.companies.enrich.json
  - src/treg/catalog/examples/aviato.companies.founders.json
  - src/treg/catalog/examples/aviato.companies.funding_rounds.json
  - src/treg/catalog/examples/aviato.companies.investments.json
  - src/treg/catalog/examples/aviato.companies.outbound_investments.json
  - src/treg/catalog/examples/aviato.companies.search.json
  - src/treg/catalog/examples/aviato.linkedin.company.posts.json
  - src/treg/catalog/examples/aviato.linkedin.post.comments.json
  - src/treg/catalog/examples/aviato.linkedin.post.reactions.json
  - src/treg/catalog/examples/aviato.linkedin.post.reposts.json
  - src/treg/catalog/examples/aviato.linkedin.user.posts.json
  - src/treg/catalog/examples/aviato.people.contact.get.json
  - src/treg/catalog/examples/aviato.people.email.find.json
  - src/treg/catalog/examples/aviato.people.enrich.bulk.json
  - src/treg/catalog/examples/aviato.people.enrich.json
  - src/treg/catalog/examples/aviato.people.phone.find.json
  - src/treg/catalog/examples/aviato.people.search.json
  - src/treg/catalog/examples/aviato.people.search.simple.json
  - src/treg/catalog/examples/crustdata.companies.autocomplete.json
  - src/treg/catalog/examples/crustdata.companies.enrich.json
  - src/treg/catalog/examples/crustdata.companies.identify.json
  - src/treg/catalog/examples/crustdata.companies.jobs.search.json
  - src/treg/catalog/examples/crustdata.companies.search.json
  - src/treg/catalog/examples/crustdata.people.autocomplete.json
  - src/treg/catalog/examples/crustdata.people.enrich.json
  - src/treg/catalog/examples/crustdata.people.search.json
  - src/treg/catalog/google-search-console.yaml
  - src/treg/catalog/google-search-console.extended.yaml
  - src/treg/catalog/google-tag-manager.yaml
  - src/treg/catalog/google-tag-manager.extended.yaml
  - src/treg/catalog/instagram.yaml
  - src/treg/catalog/instagram.extended.yaml
  - src/treg/catalog/justoneapi.extended.yaml
  - src/treg/catalog/minimax.yaml
  - src/treg/catalog/apify.yaml
  - src/treg/catalog/brightdata.yaml
  - src/treg/catalog/companyenrich.yaml
  - src/treg/catalog/oceanio.yaml
  - src/treg/catalog/akta.extended.yaml
  - src/treg/catalog/dataforseo.extended.yaml
  - src/treg/catalog/tikhub.extended.yaml
  - src/treg/catalog/examples/minimax.video-gen.result.retrieve.json
  - src/treg/catalog/examples/minimax.video-gen.from_image.json
  - src/treg/catalog/examples/minimax.video-gen.task.status.json
  - src/treg/catalog/openrouter.yaml
  - src/treg/catalog/openrouter.extended.yaml
  - src/treg/catalog/examples/openrouter.x.alibaba-wan-3-0.json
  - src/treg/catalog/replicate.yaml
  - src/treg/catalog/replicate.extended.yaml
  - src/treg/catalog/examples/replicate.image-gen.flux-schnell.json
  - src/treg/domain/catalog/__init__.py
  - src/treg/domain/catalog/store.py
  - src/treg/domain/money/settlement.py
  - src/treg/domain/catalog/stats.py
  - src/treg/infra/catalog_observations.py
  - src/treg/routers/catalog.py
  - tests/test_aigc_pr_b.py
  - tests/test_catalog_api.py
  - tests/test_catalog_validate.py
related:
  - architecture/money.md
  - architecture/proxy-model.md
  - interface/cli.md
---

# Endpoint catalog — platform-grouped operations per provider

The computed cost view uses a `cost.table` fallback as its scalar validated upper bound for
eligibility and compact displays. Runtime charging evaluates the first matching row against request
values plus catalog defaults and freezes that settlement basis. Terminal usage or the recorded table
evidence feeds the shared money settlement function; provider variation stays declarative in YAML.

## Authorization metadata

An endpoint can declare `authorization_method`, ordered `authorization_methods`, method-specific
`authorization_paths`, `required_scopes`, `required_resource`, and `token_type`. `_normalize`
keeps these fields on the internal row and exposes them on endpoint detail only when present.
Marketplace resolution uses them for preflight and grant selection. Instagram is the first user;
its 32-row audit is in [instagram-oauth](instagram-oauth.md).

Meta's published reference is not available as a machine-readable OpenAPI document. Reviewed
Instagram `input` and authorization contracts are therefore curated catalog data, and ingestion
carries them forward instead of erasing them on a later scrape.
Instagram is also parameter-multiplexed: profile lookup and business discovery intentionally share
`GET /{ig_user_id}`; the required `fields=business_discovery...` value selects the latter operation.

## Why

The marketplace registry (`oauth_providers.py`) catalogs *credentials*: how to connect a provider.
It says nothing about what you can DO once connected — which endpoints exist, what they cost, what
they return. Agents guess paths from external docs and burn paid calls. This layer answers that,
and it runs through the team's OWN keys: every call is proxied, governed and audited.

The catalog adds that operations layer:

- **platform** (tiktok, instagram, google, web, …) → the marketplace grouping axis: click TikTok,
  see every provider + endpoint that serves TikTok data.
- **capability** (`tiktok.user.profile`) → the same operation across providers, so a user can
  compare TikHub vs JustOneAPI for one job, and a future router can fail over between them.
- **verified example responses** → captured during live testing, because docs show request params
  but choosing an API comes down to what actually comes back.

Crustdata and Aviato support both BYOK and treg's platform-key tier. Their catalog costs stay in the
vendors' native credits; `fx.yaml` converts the actual replacement rates treg pays ($0.30 per
Crustdata credit from the configured 500-for-$150 auto-top-up, $0.01 per Aviato credit from the
configured 1,000-for-$10 recharge and paid receipt). Every paid row therefore has a computable USD
price and is platform-eligible when the deployment keys and allow-list are set.

Their core catalogs use only existing marketplace platforms. Crustdata has eight live-verified,
single-call operations: five on Company data and three on People & contact data. Batch routes are
omitted because they can create unexpectedly large jobs and costs; sales-enabled routes that the
connected account cannot verify are also omitted. Its generic web search and page fetch are not placed on the `web` platform because that
marketplace card currently means backlinks, authority and domain metrics. Aviato has 21 curated
operations: nine on Company data, seven on People & contact data, and five on LinkedIn social. Both
Aviato people-search forms remain: the POST route exposes the full DSL, while the GET route is a
separate simple-query workflow.

Bulk behavior stays inside the faithful relay. Crustdata batch operations are not catalogued.
Aviato company and person bulk enrichment are synchronous JSON calls. No provider-specific
buffering, callback receiver, or proxy branch is added. Crustdata's required
`x-api-version: 2025-11-01` header remains provider metadata and is bound on every BYOK and
platform-key call.

Variable prices use the existing reserve→settle path. Crustdata reserves the documented maximum
for the requested record count and settles the exact `X-Credits-Used` response header. Aviato's
preview calls reserve zero; observed email/rescrape add-ons are declared in each endpoint's generic
`cost.modifiers` map and derived from request flags; synchronous bulk
calls reserve per lookup and settle per returned successful record. Simple people search reserves
the documented one-credit-per-result enrichment add-on but settles its observed 0.25-credit base.
A lower price needs repeat balance evidence because Aviato does not return the exact call charge.
That evidence showed that company single and bulk rescrape, person single rescrape, and person bulk
email riders are not billed, although the authenticated price page lists them. Person single email
and person bulk rescrape riders are billed. A `reserve_only: true` modifier keeps each documented
but unbilled rider in the temporary hold. `settle: modifiers` then uses only the measured modifiers
for the final charge. This protects treg from a documented maximum without overcharging the caller.

A `cost.modifiers` rule names a parameter location (`query`, `body`, or `lookups`), a match rule
(`truthy` or `present`), and exactly one credit effect: make the call free, add fixed credits, or add
credits per requested result. The validator rejects any other shape. This keeps vendor numbers in
catalog YAML while the billing code reads the rules without provider-specific credit constants.
An optional `cost.settle: base` keeps documented riders in the reserve but settles the successful
call at the catalog base when repeat live evidence proves that the provider neither bills nor
delivers those riders.

A verification stamp proves the request shape, response shape, and paid behavior that the evidence
actually observed. A placeholder path value or a free miss does not prove a paid hit. Such rows keep
the documented price and say which paid behavior remains unobserved. Captured examples use public
records and omit private identities or content when counts are enough to prove the response shape.

Path placeholders are substituted by the marketplace caller. Raw values are percent-encoded; a value
that already contains a valid `%HH` escape is kept verbatim so callers can safely reuse encoded resource
names returned by an upstream API. An invalid/literal `%` is still encoded as `%25`. Search Console's
`siteUrl` examples deliberately use the raw `sc-domain:example.com` form to demonstrate the default path.
Google Tag Manager is the opposite case: its `parent`/`path` values describe a hierarchy rather than
one opaque identifier, so the curated catalog exposes atomic account/container/workspace/version ids.
`catalog_ingest.google_flat_path_params` makes the generated GTM input schema use the same atomic
placeholders already present in Discovery's `flatPath`; no slash-delimited resource name is passed
through one placeholder and accidentally encoded as `%2F`.

## Where things live

```
src/treg/catalog/
  capabilities.yaml        # the shared capability taxonomy (the cross-provider join key)
  aliases.yaml             # query word -> catalog words (search-time vocabulary bridge)
  fx.yaml                  # currency -> USD rates + per-PROVIDER credit rates (see "Cost" below)
  <service>.yaml           # CORE tier — hand-curated; <service> = OAuthProvider.service
  <service>.extended.yaml  # EXTENDED tier — machine-generated full endpoint surface
  examples/<endpoint-id>.json  # truncated, scrubbed real responses captured at verify time
scripts/
  catalog_drift.py            # path+method drift against providers' public OpenAPI documents
  catalog_validate.py         # schema + referential checks (run in CI / after any edit)
  catalog_verify.py           # live-tests CORE endpoints with a real credential; writes examples/
  catalog_verify_extended.py  # the same for the extended tier, in bulk, under a spend cap
  catalog_ingest.py           # bulk-generates the extended tier from provider specs
  catalog_cost_provenance.py  # backfills cost units + provenance; re-run after any re-ingest
src/treg/routers/catalog.py    # open Catalog JSON routes, attached in legacy registration order
```

Data files are YAML (curation-friendly) and loaded through `catalog_store`. The open JSON handlers
live in `routers.catalog`; `api.py` attaches their router at the original position so the specific
Catalog API paths continue to precede the later `/catalog/{slug}` page route.

## Two tiers

Curation and coverage pull in opposite directions: an agent needs to know that TikHub *can* read
Zhihu answers (breadth), and separately needs one endpoint per job that is known to work and known
to cost $0.001 (depth). The catalog carries both, in separate files, distinguished by `tier`.

| | `tier: core` — `<service>.yaml` | `tier: extended` — `<service>.extended.yaml` |
|---|---|---|
| written by | a human, one endpoint at a time | `scripts/catalog_ingest.py`, from the provider's spec |
| size | ~10–15 per provider | every route the provider exposes (hundreds to ~1400) |
| `capability` | required — the cross-provider join key | absent; nothing is mapped to the taxonomy yet |
| `input` / `test_request` | required, hand-written | generated, when the provider documents parameters |
| `verified` + `example_response` | expected | where the generated `test_request` passed a live call |
| example size | ~10 KB, arrays → 2 items | ~2 KB, arrays → 1 item (shape, not fidelity) |
| `cost` | required | present when the provider publishes per-route prices |

The extended tier originally carried no `input`, `test_request` or verification at all — the tiers
split on *curated vs. generated*, and it read as if it also split on *tested vs. untested*. It does
not, and it should not: a provider that documents its parameters (with example values, as TikHub
does) gives us everything needed to generate a test request and make the call. What stays exclusive
to core is the part a machine cannot do — mapping the endpoint to a capability, choosing the test
target deliberately, and a full-fidelity example. See "bulk-verifying the extended tier" below.

**An ingested price is a claim we will charge on, so a generated `free` is a bug, not a default.**
`x.extended.yaml` shipped 168 routes priced `free` off a plan-tier model X had already abolished,
while the proxy — which skips a `free` block, its `usd` being falsy — billed the provider fallback:
the catalog published $0 and the balance moved $0.10. Where the upstream bills *treg* (an
`oauth_billed` provider, [auth-secrets](auth-secrets.md)), the generator must therefore price every
route it emits, and a test walks the provider asserting the published price equals the reserved one.

The second half of that lesson cost a review round: **the fix for a blanket price is not a smaller
blanket.** The first repair priced all 74 X writes at $0.015 — the post-creation rate — when X
publishes a row per ACTION, and creating a list is $0.010, managing one $0.005, deleting an
interaction $0.010. Read the rate card and transcribe it (`catalog_ingest.X_RATES` is the card,
`X_ROUTE_RATES` the route→row mapping, and each entry's note names the row it was priced from);
where the mapping is a judgement call, `confidence: inferred` says so and takes the dearer reading,
because treg pays the difference. Watch for **conditional** rates in particular: X's $0.001 "owned
read" applies only when the caller owns the developer app, which on a registry connect is treg —
quoting it for our members under-billed the very calls we are charged the most for.

Core wins on collision: `catalog_ingest.py` drops any `(method, path)` the provider's core file
already curates, so an endpoint appears exactly once across both tiers. Promoting an extended entry
means moving it into the core file and completing it (steps 3–8 below) — not editing it in place;
extended files are regenerated wholesale and hand edits are lost.

OpenRouter video models are the deliberate exception to path-only collision detection: every model
uses `POST /videos`, so `core_body_models` skips only the fixed `body.model` values already curated
in core. Its OpenRouter and Replicate generation ingesters emit no capability guesses and explicitly
set `domain: models`; this keeps every coverage row as a standalone model. Their
`carry_verification(..., carry_capability=False)` migration keeps verification evidence and
reviewed names/kinds without resurrecting old inferred capability tags.
The core AIGC generation rows pin `domain: models` too and carry PER-MODEL capabilities
(`video-gen.hailuo.from_text`, proposed in their provider files) rather than the job-level
`video-gen.from_text` family. Generation models are not interchangeable - a merged row comparing
Hailuo with Wan or Seedance is a false comparison - so the job-level capabilities are deliberately
memberless, reserved for hand-picked models (see capabilities.yaml). Both AI generation pages
therefore render as ONE flat model wall; the same model reachable over several routes (MiniMax
direct, OpenRouter, Replicate all serve Hailuo) sits adjacent under model-led names, which is the
comparison that actually means something. The per-model capability is the join key that lets those
routes merge onto one row if that comparison is later curated.

## Schema

### capabilities.yaml

```yaml
capabilities:
  tiktok.user.profile: "Public profile of a TikTok user (followers, bio, stats)"
  web.backlinks.summary: "Aggregate backlink profile of a domain or URL"
platforms:
  tiktok: "TikTok"
  web: "The web at large (backlinks, authority, traffic)"
  video-gen: {label: "Video generation", category: "AI generation"}
  image-gen: {label: "Image generation", category: "AI generation"}
```

Rules:
- A capability id is dot-delimited, lowercase; the FIRST segment is its platform slug.
- Ids name the *job*, not the provider's endpoint ("get user profile", not "fetch_user_profile_v2").
- Adding a capability = adding it here. Provider files may carry `proposed_capabilities:` (same
  mapping shape) when curation discovers a job the taxonomy lacks; the reviewer merges them into
  this file. The validator accepts a capability that is either global or proposed in the same file.
- Under `AI generation`, platform means the generated-media modality rather than a system that owns
  the data. The frozen vocabulary is `video-gen.from_text`, `video-gen.from_image`,
  `video-gen.task.status`, `image-gen.from_text`, and `image-gen.edit`; text-to-video and
  image-to-video stay separate because their required inputs and prices differ.

### `<service>.yaml`

```yaml
provider: tikhub                # must equal OAuthProvider.service in oauth_providers.py
source:
  docs: https://docs.tikhub.io/
  openapi: https://api.tikhub.io/openapi.json   # null when the provider has no spec
  curated: 2026-07-28
limits: "10 requests/second per key"   # optional, provider-level: the rate/quota model in one line
pricing_url: https://…                 # optional: where CURRENT prices live (values in cost blocks age)
endpoints:
  - id: tikhub.tiktok.user.profile   # unique; convention: <provider>.<capability>
    capability: tiktok.user.profile  # must exist in capabilities.yaml (or proposed_capabilities)
    platform: tiktok                 # must equal the capability's first segment
    domain: user                     # optional: the platform page's section. One lowercase word.
                                     #   Omit it and the loader derives one — capability's middle
                                     #   segment, else a path keyword, else the path's grouping
                                     #   segment, else "other". Set it only to override a bad guess.
    scope: any_account               # any_account (scrapers) | own_account (first-party OAuth)
    kind: data                       # optional; data (DEFAULT) | action | account | utility.
                                     #   what the endpoint IS — see "Kind" below. Absent ⇒ data.
    method: GET
    path: /api/v1/tiktok/web/fetch_user_profile   # relative to the provider's base_url
    name: "Get user profile"        # optional short DISPLAY title (≤60 chars). Set it when the
                                    #   summary is doc-prose too long for a row heading; clients
                                    #   fall back to `summary` when absent.
    summary: "Public TikTok profile by username"  # the provider's own description, kept VERBATIM —
                                    #   `name` is ours to word, `summary` is theirs
    input:                           # split by location — mirrors treg's binding model
      queryArrayEncoding: json      # optional default for array query params: json | comma |
                                    # repeated (the compatibility default).
      queryParams:
        uniqueId: {type: string, required: false, note: "username from the profile URL", example: "tiktok"}
        secUid:   {type: string, required: false}
      note: "one of uniqueId | secUid; uniqueId preferred"
      # also allowed: pathParams, body, bodyType (json|form)
    test_request:                    # EXACT params catalog_verify.py sends — must be cheap
      queryParams: {uniqueId: "tiktok"}
    expect:                          # optional; default is "HTTP 2xx"
      json_path: code                # dotted path into the response JSON
      equals: 200                    #   (for providers that answer HTTP 200 even on failure)
    cost:
      type: per_success              # per_call | per_result | per_success | free | quota_rows
      value: 0.0015
      currency: USD
      note: "charged on 2xx only; errors free"
    verified: 2026-07-28             # date of the last PASSING catalog_verify.py run; absent = unverified
    example_response: examples/tikhub.tiktok.user.profile.json   # written by catalog_verify.py
    docs_url: https://docs.tikhub.io/…
```

### Async descriptors

`catalog_store._normalize` sets `cache: forbidden` for `image-gen` and `video-gen` endpoints,
including synchronous generation, task/result utilities and generated extended rows. Their `kind`
is unchanged. These requests must reach the provider, not replay shared-account task ids or media
from an identical prompt. Other platforms retain their declared/default cache policy.

An asynchronous submission endpoint may carry an `async:` descriptor. A provider file may put the
same block at top level as a default for every endpoint in that file; an endpoint block **replaces
it whole** (`effective_async_descriptor`): a descriptor is one protocol, and a protocol that differs
in one axis differs in poll target, status vocabulary and result location together (MiniMax v2
against v1), so a field-wise merge only produced descriptors nobody had written down. `catalog_store`
serves the effective descriptor on the normalized endpoint. An explicit endpoint `async: false` opts
a utility or synchronous endpoint out of the provider default; absence means inherit.

**Poll mode in practice.** Every listed provider polls a static catalog id (`poll.endpoint`), which
the CLI reaches through `/call/<id>` on any credential tier. Replicate offers both `urls.get` and the
stable `GET /v1/predictions/{id}`; the static form is listed (`replicate.predictions.get`) because a
`--await` that polled the absolute URL through `/call/https://…` was refused for a team on treg's
key - that path resolves only a team's own tool (sample run, 2026-09-02). The dynamic-URL mode
(`poll.url_from` + `url_hosts`) stays in the schema, validator and worker for a provider that offers
nothing else (BFL), but today it works only for BYOK teams; serving it on the platform key needs a
host-allow-listed relay that is not built. Do not document it as available.

**Envelope errors.** A submission endpoint may carry `expect` (the provider-wide or per-endpoint
success rule already used by settle); `application.call.service._submission_accepted` gates
deferral on it, so MiniMax's HTTP-200-with-`base_resp.status_code: 2013` releases at once instead of
becoming a task nobody can poll. The synchronous MiniMax image endpoint carries the same rule;
otherwise an invalid prompt or output count would be charged at the image table or fallback price.
OpenRouter's terminal failure set includes `failed`, `cancelled`, and `expired`; all three release
the hold as soon as the status endpoint reports them.

```yaml
async:
  id_from: task_id
  poll:
    endpoint: minimax.video-gen.task.status
    param: {in: queryParams, name: task_id}
    # Alternative mode:
    # url_from: polling_url
    # url_hosts: [api.example.com]
  status:
    path: task.status
    success: [succeeded]
    failure: [failed, cancelled]
  result:
    path: task.content.url
    # Alternative mode:
    # fetch: provider.video-gen.result.retrieve
    # fetch_param: {in: pathParams, name: video_id, value_from: id}
    ttl_note: 9h
  interval: 10
```

A `cost.table` also prices out as a range: at load time `_table_floor` computes the cheapest row
(a `times` row at its field's declared `min`) into `cost.table_min`, and `cost_view` exposes it as
`usd_min` beside `usd`, which stays the validated ceiling (what reserve and eligibility read). Every
price surface - the wall, `treg catalog search`, the dashboard, `/access` - shows `$low-$high` for a
table rather than the worst case alone.

The validator checks the effective descriptor. Dotted JSON paths are syntactically valid; success and
failure are non-empty, disjoint lists; `interval` is positive; poll has exactly one of `endpoint`
or `url_from`; result has exactly one of `path` or `fetch`; every descriptor block rejects unknown
keys. Status values are compared after string coercion on both sides; a missing or unrecognized value
means still in progress, in both the CLI awaiter and the settlement worker. Static poll/fetch ids must
be same-provider GET utility endpoints. Their mapping is explicit:
poll `param` is exactly `{in, name}`, while result `fetch_param` is exactly `{in, name, value_from}`
so a terminal field such as MiniMax's `file_id` is not confused with the utility request parameter.
The named path/query input must exist on the target endpoint. Body-mode polling is deliberately
outside the frozen contract because no surveyed provider uses it and the generic client could not
faithfully execute it. Dynamic URLs require a non-empty `url_hosts` allow-list. Any endpoint with
`async:` must use `cost.type: per_success`. The descriptor is metadata
beside the faithful relay: it never changes provider-native parameters or response bodies. The call
router serializes the effective descriptor into `X-Treg-Async` before the response stream starts;
it does not inspect or buffer the upstream body.

Older async pairs that settle on their existing request paths use `resource_ownership` alongside
the deferred-settlement design. `produces` maps response JSON paths to provider-local resource
kinds; `requires` binds a path/query parameter to one of those kinds. On treg's shared key, a 2xx
producer records the opaque id for the caller org, and a consumer is refused before relay unless the
same org owns that provider/kind/id tuple. This covers Apify run/dataset ids, Bright Data snapshot
ids, and CompanyEnrich bulk job ids without changing their billing behavior. The validator requires
declared parameters and exact non-empty `{kind, path}` / `{kind, param}` shapes. BYOK does not use
this metadata because the provider account itself belongs to the caller.
Formal descriptors also materialize their poll/fetch ids under endpoint-namespaced resource kinds;
their utility rows declare matching `requires` rules. The frozen `AsyncTaskRecord` remains the
compatibility authority for tasks created before the resource table existed, while the explicit
utility rule prevents a later catalog edit from silently turning a protected endpoint fail-open.
The catalog test additionally rejects platform-eligible task/status/result object reads that have a
required id but omit this metadata, so new legacy-style pairs cannot rely on a reviewer noticing the
boundary by hand.
Generated legacy task consumers for which no trustworthy producer→id chain is represented are
explicitly `platform_blocked` instead: Akta request status, TikHub's captions-result route, and the
DataForSEO on-page/SERP task consumers remain callable with BYOK but never receive treg's shared key.
`carry_verification` preserves that reviewed block across re-ingestion just like a verification
stamp; silently regenerating it away would reopen the tenant boundary.

MiniMax's curated Hailuo routes intentionally use the v1 three-step protocol: submit with
`POST /v1/video_generation`, poll `GET /v1/query/video_generation` with a query-string task id and
the terminal values `Success`/`Fail`, then pass the returned `file_id` to
`GET /v1/files/retrieve`. The v2 generation path serves the H3 family and is not a protocol upgrade
for the Hailuo models in this listing.

OpenRouter ingest reads `/api/v1/videos/models`, emits one extended row per model on the shared
`POST /videos` route, and converts duration-based `pricing_skus` into price tables with
`rate_card_api` provenance. It converts `cents_per_*` units to USD, maps resolution/audio dimensions,
orders narrower conditions first, and collapses indistinguishable mode SKUs to the highest rate.
Token, image-input, reference-image, and megapixel-second SKUs preserve the live rate card but stay
explicitly unknown/BYOK-only because one bounded `times` field cannot safely describe them.
Two verified Wan 3.0 480p/2s calls each quoted $0.10 from `pricing_skus` but reported
`usage.cost: 0.2125`; rate-card rows are therefore `documented`, not observed-cost `verified`.
Replicate ingest joins the official text-to-image, text-to-video, and image-to-video collections;
each generated row takes its request fields from `latest_version.openapi_schema`. Its generated
prices are explicitly unknown, while the curated core rows carry per-model page provenance. Both
ingesters sort their inputs and produce byte-identical output when upstream data is unchanged.

Utility capability names still describe the utility's actual job. OpenRouter model discovery uses
the file-local proposed `video-gen.models.list`; OpenRouter and MiniMax content retrieval use the
proposed `video-gen.result.retrieve`; only polling uses the frozen `video-gen.task.status`. These
rows remain hidden management plumbing because `kind: utility`, and proposed capabilities avoid
expanding the global generation vocabulary merely to satisfy the core-row capability requirement.

### `<service>.extended.yaml`

Generated — never hand-edited. Re-run `uv run python scripts/catalog_ingest.py <service>` instead.

```yaml
provider: tikhub                  # same rule as core: equals OAuthProvider.service
source:
  method: openapi + provider rate card   # how the entries below were derived
  ingested: 2026-07-28                   # date of the generating run
  spec_urls:                             # every upstream the run read, so it is reproducible
    - https://api.tikhub.io/openapi.json
endpoints:
  - id: tikhub.x.zhihu-web-fetch-answer-comments   # <service>.x.<path-slugified>; the `.x.`
    tier: extended                                 #   infix keeps extended ids out of the
    platform: zhihu                                #   `<provider>.<capability>` namespace
    method: GET
    path: /api/v1/zhihu/web/fetch_answer_comments
    name: "Zhihu answer comments"   # optional, same meaning as core; the ingesters harvest it
                                    #   where the spec offers a human title distinct from the
                                    #   description (TikHub's Apifox op names, Just One API's
                                    #   per-op summary / info.title, DataForSEO's operationId).
                                    #   Carried across re-ingests by id; providers may also carry
                                    #   reviewed `capability` mappings when coverage policy permits.
    summary: "Get comments of a Zhihu answer"
    kind: data                      # optional; data (DEFAULT) | action | account | utility (see "Kind")
    cost: {type: per_success, value: 0.001, currency: USD}   # optional
    docs_url: https://docs.…                                 # optional
    input:                          # generated from the provider's parameter docs
      queryParams:
        answer_id: {type: string, required: true, note: "Answer id", example: "1913...”}
    test_request:                   # generated: documented example values, page sizes clamped
      queryParams: {answer_id: "1913…", limit: 5}
    verified: 2026-07-28                                     # a real call passed
    example_response: examples/tikhub.x.zhihu-web-fetch-answer-comments.json
```

Rules:
- Required: `id`, `platform`, `method`, `path`, `summary`. `platform` must exist in
  `capabilities.yaml` — that is what puts the endpoint on a marketplace shelf.
- `capability` is normally ABSENT (extended entries are unmapped). AIGC generation coverage forbids
  inferred mappings entirely: comparison membership is curated in core, and extended rows use the
  explicit `models` domain. If another extended file has a reviewed mapping, the validator holds it
  to the full core rules so promotion by hand cannot silently drift.
- `cost` is optional, because several providers price per API family rather than per route. When
  present it must still be a real cost model (`cost.type` from the same enum as core).
- `input` / `test_request` appear when the provider publishes enough parameter documentation to
  generate them; both are machine-written and are rewritten on the next ingest.
- Query arrays carry an explicit wire encoding when the provider does not accept repeated keys:
  `input.queryArrayEncoding` sets the endpoint-wide format. `catalog_store.query_values()` is shared
  by MCP request assembly and `call_template()`, so the structured schema and paste-ready command
  cannot disagree. Complete
  `name=value` arguments are shell-quoted with `shlex.quote` after canonical boolean/JSON encoding.
  Endpoint declarations are only valid when every array parameter shares a wire format. Meta Ad
  Library's array parameters all use JSON; undeclared endpoints retain repeated keys. Pinterest's
  mixed convention remains a documented catalog gap until a live connection can verify a separate
  per-parameter extension.
- `verified` + `example_response` mean a live call was made and passed, and carry exactly the same
  weight as in core — the validator applies one rule to both tiers: verified ⇒ a `test_request` to
  re-verify with and an `example_response` file that exists.
- Exactly one of `verified`, `unverified`, `untestable` or `skipped` should be present on an entry
  that has been through the pipeline:
  - `untestable: <reason>` — set at INGEST: no test request could be generated (route absent from
    the provider's docs, or a required parameter only the caller can supply, e.g. their own
    platform cookie). No call was made and none is possible with a bare key.
  - `unverified: http 404 …` — set at VERIFY: the call was made and failed, with the status code
    and the provider's message. This is a finding, not a gap: `402` means the route needs a paid
    plan tier, a family-wide run of `404`s means the provider's upstream scraper is broken.
  - `skipped: <reason>` — set at VERIFY: a usable test request exists and the call was deliberately
    not made, to conserve a paid balance. The reason names the sibling endpoint that WAS verified,
    or why one call costs too much. See "the fourth state" below — these clear with money, not
    investigation, which is what separates them from the two above.

  `catalog_validate.py` ENFORCES this: an extended entry that has been through the pipeline (it
  has a `test_request`, or any of the four keys) must claim exactly one of them, non-empty. Two at
  once is a contradiction; a key present but empty is the failure that motivated the rule — a
  re-run overwrote an endpoint's result record, dropped the reason string, and stamped an empty
  state, which every other check happily passed. A never-verified entry straight out of ingest has
  neither a request nor a state key and is left alone.
- Ids are unique across the WHOLE catalog, both tiers, all providers.
- Two optional fields exist only in this tier, both added for the first-party OAuth providers:
  - `host: <fqdn>` — this route is NOT on the provider's `base_url`, and its `path` is relative to
    the named host instead. Google splits one product across sibling `*.googleapis.com` services
    (GA4 reporting vs GA4 admin; six separate My Business services) while an `OAuthProvider` names
    one host. The same OAuth token calls them all, so the endpoints are real and worth listing —
    but the auto-provisioned tool is bound to `base_url`, so calling one needs a second tool bound
    to that host. Absence of `host` means "callable through the provisioned tool".
  - `scope_gap: <one line>` — the credential treg's OAuth app obtains CANNOT call this, and this is
    the scope that is missing. These are listed rather than dropped on purpose: the set of gaps is
    the answer to "which scopes should we add to the registered app", and it is only visible if the
    endpoints stay in the file. `scope_gap` present ⇒ expect 403 until the app is widened.

### Kind — the browse surface vs. the plumbing

`kind` says what an endpoint IS, so the marketplace can lead with the useful surface and tuck the
provider's own machinery out of the way. It is optional in BOTH tiers; absent reads as `data`.

| `kind` | what it is | examples | browse |
|---|---|---|---|
| `data` (default) | fetch / scrape / enrich a resource | get user profile, backlink summary, SERP | shown |
| `action` | a meaningful WRITE on the connected user's OWN account | post a video, reply, update an ad budget, upload | shown |
| `account` | the provider's own list/webhook/saved-search/credit CRUD | create/delete a lead-list, manage webhooks | hidden |
| `utility` | helpers with no data of their own | token/x-bogus generators, enum & location listings, decrypt/encrypt, device register | hidden |

`data` + `action` are the **browse surface**; `account` + `utility` are **management endpoints**.
Three things follow, and they are the whole point of the field:

- **The platform census counts data + action only.** `GET /catalog/platforms` reports each shelf's
  `endpoints` / `capabilities` / `verified` and its "from …" price over the browse surface — a
  management endpoint is real inventory but it is not what a tile advertises, so it never inflates
  those numbers (nor the marketplace tile counts the dashboard renders from them).
- **The default platform view drops them.** `GET /catalog/platforms/<slug>` returns only the
  browse surface in `capabilities` / `extended` / `domains`, plus a `hidden_count`. Pass
  `?include_hidden=1` to get the WHOLE surface back — every endpoint carries `kind`, so a client
  can fold the plumbing behind its own control. The dashboard does exactly this: it requests
  `include_hidden`, renders data/action in the ledger, and files account/utility behind a small
  per-section "N management endpoints" expander (the same show-more gesture as the platform tiles).
- **`kind` is a reviewed judgement, carried across re-ingests.** Like `capability` and `name`, an
  extended entry's `kind` is set by review, not derived from the spec, so `catalog_ingest.py`'s
  `carry_verification` re-attaches it by id — regenerating the file must not reset it to `data`.

`catalog_validate.py` only checks the value when present: a stated `kind` must be one of the four.

### Naming — `name` is the search surface we own

`summary` is the provider's text, verbatim; `name` is OURS, and since 2026-08-20 it is searched
(same weight as summary). That makes it the one per-endpoint field where curation may put the
words agents type. The formula: **job + the input the caller must hold + top output facets**, ≤60
characters, and it must read as a natural title — it is the row heading on every surface.

    Linkedin: get company profile (web_v2)   ->  LinkedIn company profile by URL or slug — headcount, industry
    Get user profile                         ->  TikTok user profile by username — followers, bio, stats

The rules (applied catalog-wide in the 2026-08-20 rewrite; every new provider follows them):

1. Name the JOB in task words — never the vendor's operation title or version codes.
2. Say the INPUT ("by name", "by domain", "by ASIN", "by LinkedIn URL"). Agents search by what
   they hold; only the caller knows its inputs — that doctrine applies to naming too.
3. Say the top OUTPUTS when people search by them ("headcount", "reviews", "hiring signal").
4. One concept, one word, catalog-wide: always "postings", never sometimes "vacancies";
   `aliases.yaml` covers the agent's side, our side must be consistent.
5. Prefer the longer word form — "postings" contains "posting"; substring matching never works
   backward.
6. No dead words: "API", "data", "get", "fetch", "endpoint" are soft tokens worth nothing.
7. No stuffing. If it does not read as a title, it is wrong. Overflow vocabulary belongs in the
   capability description (weight 3, shared by the group) or `aliases.yaml`, never in the name.
   Worked case (2026-08-27): "find instagram influencers by niche…" returned ZERO results because
   influencers.club's name/summary said only "creators" — fixed by naming the job in the endpoint
   (`…influencers by niche & size`), carrying the facet words (country, followers, engagement,
   Instagram/TikTok/YouTube) in the `creators.search` capability description, and aliasing
   `influencer(s)/kol(s)/microinfluencer(s) → creators` and `ig → instagram`. Long natural-language
   queries still require every rare word to appear somewhere; the `near:` hint tells the agent
   which words to drop.
8. TRUTH over vocabulary: derive the name only from the row's own summary, path and input fields.
   A name claiming an output the endpoint does not return is a lie an agent will spend money on.

### Cost — the file keeps the billing unit, the server computes USD

A `cost` block stays in whatever unit the PROVIDER bills in; that is the number that stays correct
when a rate moves. `cost.usd` is added at SERVE time by `Catalog.cost_view` from `fx.yaml`, so a
rate refresh re-prices the whole catalog without touching a provider file. Clients (dashboard cards,
`treg catalog search`, `treg catalog get`) lead with `usd` because a column is only comparable in
one unit, and fall back to the native amount when `usd` is null.

The full block:

```yaml
cost:
  type: per_result        # per_call | per_result | per_success | free | quota_rows
  value: 2.00             # non-null unless confidence: unknown
  currency: USD           # USD | CNY | credit | unit
  per: 1000               # the quantity `value` covers (default 1)
  unit: row               # what `per` counts — or, under `currency: unit`, the provider's meter
  source: docs            # rate_card_api | docs | observed | vendor_email | inferred
  source_url: https://…   # the exact rate card / pricing page (or rate-card endpoint)
  checked: 2026-07-28     # when the PRICE was confirmed — not when the route was called
  confidence: documented  # verified | documented | inferred | unknown
  note: "…"               # free text: the half of the charge the schema cannot hold, caveats, traps
```

For finite AIGC matrices, linear rates, and usage-settled generation, `value` is replaced by an
ordered first-match `table` plus an explicit fallback upper bound:

```yaml
cost:
  type: per_success
  table:
    - {when: {body.model: Model-A, body.resolution: 512P, body.duration: 6}, value: 0.3}
    - {when: {body.model: Model-B, body.resolution: 768P}, value: 0.13, times: body.duration}
  fallback: {value: 2.0, note: "most expensive supported combination"}
  currency: USD
  settle: table                 # or usage
  # usage: {path: usage.cost, unit: usd}
  source: docs
  source_url: https://example.com/pricing
  checked: 2026-09-01
  confidence: documented
```

Rows match in file order. `when` is a subset comparison: every named field must equal the request
value after input defaults are applied, using exact forms. References are location-qualified dotted
paths (`body.model`, `body.input.num_outputs`, `queryParams.mode`) so query/body collisions cannot
silently price the wrong field. Every `when` field must be required or declare `default` in `input`.
`times` multiplies by one numeric request field with a positive `max`. Narrow rows must precede broad
ones; the validator rejects a later condition shadowed by an earlier subset, duplicate conditions,
unknown row/fallback keys, non-finite values, values outside input enum/min/max, and simultaneous
`cost.value` plus `cost.table`. `fallback` is a hand-written, explained global upper bound, checked
against every row's maximum computable price. A `times` value outside the field's declared range
(or non-finite, or non-positive when no minimum is declared) matches no row and prices at the
fallback, so a request cannot reserve zero or bill past the ceiling. With `settle: table`, the
matched row is reserved and settled (fallback when unmatched). With `settle: usage`, the matched
row is reserved as the rate-card estimate and the terminal `usage.path` figure settles, which may
exceed the reserve (OpenRouter's unpublished minimums); `settle: usage` therefore requires an async
descriptor, exactly a dotted `usage.path` and a supported `usage.unit`, and `settle: table` rejects
a stray usage block. The money fragment describes the settlement itself.

`value` + `currency` + `per` answer *how much*; `type` + `unit` answer *per what*; `source` +
`source_url` + `checked` + `confidence` answer *says who, and how sure*. All four questions have to
have an answer before treg will spend its OWN money on an endpoint (see "platform-eligible" below),
which is the whole reason the provenance keys exist.

**`per` and `unit`.** Read a block as "`value` `currency` per `per` `unit`". SpyFu bills a CPM, so
`value: 2.00, per: 1000, unit: row` — and `cost_view` divides, serving `usd: 0.002` per row. Hunter
charges 1 credit per 10 emails (`per: 10, unit: record`), Akta 1.5 credits per 50 reviews. Without
`per`, every one of those had to be either wrong or rounded into prose.

**Three kinds of denomination convert, and they convert differently:**

- **A real currency** (`currency: USD`, `CNY`) uses `fx.yaml`'s `rates_to_usd`, keyed by currency.
- **`currency: credit`** is NOT a currency. A credit is a PROVIDER-SCOPED unit — one scrapecreators
  credit and one lusha credit have nothing to do with each other — so it converts with the rate for
  the endpoint's provider from `fx.yaml`'s `credit_rates_usd` block, keyed by service. That is why
  `cost_view(cost, provider)` takes the provider: the same `value: 1, currency: credit` is worth
  $0.00188 on scrapecreators and $0.1248 on lusha.
- **`currency: unit`** is the provider's own METER: Semrush's "API units", Majestic's three
  independent allowances, Moz's row quota. `unit` names which meter, and the rate comes from
  `fx.yaml`'s `unit_rates_usd[provider][unit]`. A provider can spend several meters at once —
  Majestic's analysis / retrieval / index-item units no more convert into each other than two
  providers' credits do, so each gets its own row. Before this existed, Moz's `quota_rows` blocks
  carried no `currency` at all, defaulted to USD, and served every Moz route as costing $1.00.

A `credit_rates_usd` entry may carry **`kind: treg_shared_plan`**: a rate TREG SET for a flat-fee
provider (a subscription with a rate limit or unlimited calls), where no per-call vendor price can
exist. The credit is then "one call on treg's shared plan" and the machinery is unchanged — the
honesty lives in the entry: the basis must start with "treg shared-plan rate", name the vendor fee,
and state the break-even volume, and `fee_usd_month` must be present as data (the validator's
`check_fx` enforces all of it). The rate is reviewed monthly against `reconcile.shared_plan_recovery`
and edited by hand. The full ladder: docs/SHARED-PLAN-PRICING-PLAN.md; the billing side (429 never
billable, the recovery report): architecture/money.md.

A second treg-set kind, **`kind: treg_trial`**, prices a provider at exactly **$0** with a
`trial_calls_per_team_day` allowance as data beside the zero: a capped taste served on treg's own
FREE-tier key. The allowance is what makes $0 honest — at zero the price gives no brake, so the cap
is the congestion control (`api._enforce_trial_allowance`, per team per UTC day, successes only,
fail-closed). `cost_view` attaches the allowance to every $0 it serves, because a bare $0.00 reads
as unlimited. The validator refuses a non-zero "trial" and a zero with no allowance.

Each `credit_rates_usd` / `unit_rates_usd` entry carries `usd` plus the `basis`/`source`/`checked` that justify it —
the cheapest PUBLICLY listed tier (plan price ÷ credits included), so the served figure is an upper
bound on real spend, never an under-estimate. `usd: null` is a deliberate state, not a gap: the
provider publishes no per-credit price (sales-negotiated like Crunchbase, or not
credit-priced at all like BrightData). Those endpoints keep `cost.usd = null` and display natively
("3 credits/success"), because a guessed dollar figure is worse than an honest credit count. Both
blocks are hand-maintained and must stay ABOVE `rates_to_usd:` — `catalog_fx_update.py` rewrites the
file from the text before that key and discards anything below it.

#### Provenance — `confidence` is a claim about the PRICE, not about the route

`verified: 2026-07-28` on an endpoint says the route answered. `cost.confidence: verified` says the
money figure was confirmed. They are independent, and conflating them is how a guess gets spent:

| `confidence` | what earns it |
|---|---|
| `verified` | observed being billed on a real call (`source: observed`), or read from the provider's own live rate card (`source: rate_card_api` — TikHub's `get_all_endpoints_info`, DataForSEO's `/appendix/user_data`, ScrapeCreators' `credits_charged` in its OpenAPI) |
| `documented` | transcribed from the provider's docs or pricing page |
| `inferred` | the figure is a floor or the top of a published range — a base fee with a per-row half on top ("1 credit base + 1 per ad"), a spread ("1–9 credits", "$0.50–$5.00 per 1,000"). The recorded number is not the whole charge, and the note says what else applies |
| `unknown` | no figure is published anywhere citable. `value` MUST be null and `note` MUST say why |

Rules the validator enforces: `value: null` and `confidence: unknown` appear together or not at all;
a `verified`/`documented` price names its `source_url` (`source: observed` is exempt — its evidence
is the captured example response, not a page that may have moved); every priced entry carries
`checked`, and CI WARNS past 90 days. A file whose header says `UNVERIFIED` caps its prices at
`documented`: nothing in it has been called, so no price in it can have been seen being charged.

Free is spelled exactly one way — `type: free, value: 0, currency: USD, unit: call` — and needs no
provenance, because 0 does not move and there is nothing to re-check. It was previously written
three incompatible ways across 661 endpoints, which left `cost.usd` null on most of them:
indistinguishable, downstream, from "price unknown".

`scripts/catalog_cost_provenance.py` owns the mapping from what the repo knows about a provider's
pricing to these keys, and is re-runnable — the extended tier is regenerated wholesale, so
provenance typed by hand into a generated file would not survive the next `catalog_ingest.py`.

#### Platform-eligible — when treg may spend its OWN key on a call

`Catalog.platform_eligible(endpoint)` is the single predicate behind prepaid/platform-key access
(tier 4 of the credential ladder in `api.py`). One implementation, so the API, the validator and
the proxy cannot drift. It requires ALL of:

- `cost_view(...)["usd"]` is not None — the charge is machine-computable;
- `cost.confidence` is `verified` OR `documented` (policy widened 2026-07-31: a rate the provider
  itself publishes is billable; `verified` stays the gold standard the drift reports police, and
  `inferred`/`unknown` stay refused — a guess is not a rate);
- `scope != own_account` and `kind != account` — the provider's own bookkeeping is never worth
  spending on, and an own-account route needs the caller's own credential by definition.

The live-called `verified:` stamp is no longer required (same 2026-07-31 change): a broken route
fails unbilled under `per_success`/`per_result` billing, providers that report in-band settle at 0,
and the fail-closed daily platform cap bounds whatever remains — coverage beats caution now that
the reserve/settle machinery is proven. Eligibility alone still spends nothing: the provider must
ALSO be keyed and allow-listed (`platform_key_for`).

The doctrine is asymmetric on purpose: **a missing or unknown price reads as "refuse", never as
free.** An endpoint with no `cost` block at all is therefore not platform-eligible without anything
having to be written out for it, which is why the extended tier's unpriced routes need no
annotation. Where an endpoint carries only `observed_cost` (DataForSEO prices per API family, not
per route), `_effective_cost` synthesizes the block with `source: observed, confidence: verified`
and `checked` = the verify date: a figure the provider itself reported charging is the strongest
provenance the catalog has.

### Core-wins dedup compares NORMALISED paths — except on Graph

A hand-curated core file and a machine-readable spec never agree on placeholder spelling: core says
`/v1beta/properties/{property_id}:runReport`, Google's discovery document says `{property}`. A naive
`(method, path)` comparison therefore misses, and the endpoint ships in both tiers — that is the
DataForSEO `/v3` bug below, in its other form. The Google and X ingesters compare with every
`{...}` collapsed to `{}`, so the two spellings match.

Meta is the exception and uses exact comparison, because on the Graph API the node id IS the first
path segment: `/{post_id}/insights` and `/{page_id}/insights` differ only by the placeholder name
and are genuinely different endpoints. Normalising there would silently drop post insights because
the core file curates page insights.

## Process — adding / curating a provider

Do these steps in order; each has a hard success criterion.

1. **Ingest.** If the provider publishes OpenAPI (`/openapi.json`), fetch it and list candidate
   operations from there — do not hand-transcribe paths (that is how typos ship). Otherwise work
   from the official docs and record `source.openapi: null`.
2. **Select.** Curate, don't mirror: pick the ~8–15 endpoints an agent would actually reach for,
   and ALWAYS include the endpoints matching capabilities other providers already implement —
   overlap is the point (comparison + failover). Skip exotic ops.
3. **Map.** Assign each endpoint a capability from `capabilities.yaml`. Missing job → add it under
   `proposed_capabilities:` in your provider file, don't edit the shared taxonomy in parallel work.
4. **Describe.** Fill `input` from the spec/docs: param names, types, which are required, where
   they ride (path/query/body). Copy real constraints ("one of A|B") into `note`.
5. **Cost.** Record the provider's price model per endpoint from their pricing page — with its
   provenance (`source`, `source_url`, `checked`, `confidence`) and its unit (`per`, `unit`), per
   "Cost" above. `quota_rows` is for row-quota APIs (Moz). Unknown exact value → `value: null` +
   `confidence: unknown` + a `note` saying why. If the provider exposes its rate card as an
   endpoint, prefer it over the pricing page and record it as `source: rate_card_api`: it is
   re-checkable, which is what lets treg serve the route on its own key.
6. **Test-request.** Give every endpoint a `test_request` that is CHEAP (smallest limit, one item,
   public well-known target — e.g. user "tiktok", domain "moz.com"). This is what verification and
   future health checks replay, so it must not burn meaningful credits.
   ⚠️ Quota trap (learned live, Moz 2026-07-28): never probe an endpoint with an empty body/params
   "expecting a free validation error" — an endpoint with NO required params answers with its FULL
   default result set and bills for it (Moz's global_top_* ate an entire 50-row period quota in two
   calls). Always pass an explicit smallest limit, and on row-quota APIs check the usage endpoint
   before and after the first call.
7. **Verify + capture.** Run `scripts/catalog_verify.py <service>.yaml` with the credential in the
   `TREG_CATALOG_CRED` env var. It calls every endpoint's `test_request`, checks `expect`, writes
   the truncated example response to `examples/`, and prints PASS/FAIL per endpoint. Stamp
   `verified: <today>` ONLY on endpoints that passed — documented ≠ verified; docs lie.
8. **Scrub.** Read every captured example: replace anything personal that is not the public test
   target's own public data. The account-info endpoints of YOUR OWN key (quota, balance) must have
   emails/ids masked before commit.
9. **Validate.** `scripts/catalog_validate.py` must exit 0: schema shape, unique ids, capability
   and platform referential integrity, example files exist for verified endpoints, provider exists
   in `oauth_providers.py`.

Success criteria for a provider PR: validator exits 0; every endpoint either carries a `verified`
date + example file or an explicit comment why it could not be live-tested; no credential value
appears anywhere in the diff.

## Process — bulk-ingesting the extended tier

```
uv run python scripts/catalog_ingest.py tikhub          # one provider
uv run python scripts/catalog_ingest.py all --refresh   # every provider, re-downloading the specs
uv run python scripts/catalog_validate.py               # must exit 0
```

The script owns `<service>.extended.yaml` end to end: it fetches the provider's spec, maps every
route to a platform, drops what the core file already covers, and rewrites the file. Downloads are
cached under `~/.cache/treg-catalog-ingest` (override `TREG_INGEST_CACHE`); `--refresh` re-fetches.
Output is deterministic — a re-run with unchanged upstreams produces a byte-identical file, so a
diff always means the provider changed.

Adding a provider means adding an `ingest_<service>()` function and registering it in `INGESTERS`.
Three rules it must honour:

- **Never probe with a real call.** Discovering an HTTP method by sending a GET is how you get
  billed 1400 times (see the quota trap above). TikHub's methods come from an `OPTIONS` request,
  which Starlette answers `405 + allow:` before the handler — and therefore the meter — runs.
- **The published spec outranks the probe** (`resolve_method`). A wrong method is not a cosmetic
  error: treg *enforces* the recorded verb, so the endpoint becomes uncallable from both sides at
  once — POST refused here ("… is GET"), GET refused upstream (405). The probe is weaker than it
  looks: a preflight answering with a method *list* walks its preference order and comes out `GET`
  whatever the handler takes. So when the OpenAPI declares exactly one method, that wins; probe and
  docs are the fallback for routes the spec doesn't describe.
- **The verb and the parameter POSITION are one decision, from one document.** TikHub's Apifox docs
  list every TikTok-Ads parameter under `parameters.query` while its OpenAPI declares the same route
  POST-with-a-JSON-body. Taking the verb from one and the position from the other yields a POST
  carrying its arguments in the query string — still uncallable, just differently. When the spec
  declares a JSON body and the docs gave us none, the documented "query" parameters ARE that body.

### Catalog rot is a category of bug, and it is not the ingester's fault

The 2026-08-17 TikTok-Ads breakage was first written up here as an ingester defect. It was not, and
the correction matters more than the original claim. Those twelve routes really were `GET` when
ingested: TikHub's July spec says `get`, and the captured `example_response` is a **real billed 200
from a GET on 2026-07-27**. TikHub moved them to POST some time after. The catalog did not mis-read
the provider — it went stale, and at the time **nothing re-checked a provider's spec for drift**.

That reframed the fix. Preferring the spec over the probe is a genuine hardening, but it only helps
*at re-ingest time*, and only if the cached spec was refreshed — the cache under
`~/.cache/treg-catalog-ingest` is what an unqualified `catalog_ingest.py <provider>` reads, so a
re-run against a months-old cache faithfully reproduces months-old truth. A `verified:` stamp is
evidence about the day it was written and nothing after it.

`scripts/catalog_drift.py` now closes that gap without making a paid API call: it discovers public
OpenAPI documents from each provider file's source provenance, downloads the document with no
credential, and compares every checked-in `(path, method)`. Plain JSON is preferred; the same
`salvage_json_map` used by the ingester recovers a complete `paths` map from a truncated document,
and YAML OpenAPI is accepted too. An unmarked missing path, method change, or marked route that has
reappeared exits non-zero. Known absent marked rows are reported as `acknowledged`, not drift. The
daily `catalog-drift.yml` workflow currently runs TikHub—the provider with demonstrated production
rot—and the script remains provider-general for every catalog file that cites a public OpenAPI URL.

### Retired and broken endpoints are tombstones, not offers

Provider rot must not turn an id an agent cached yesterday into either a bare provider 404 or an
unexplained registry 404. Keep the row and add:

```yaml
status: retired                 # or broken
status_note: why it is gone and what changed
superseded_by: provider.live-id # optional; only when the operation is genuinely equivalent
```

`catalog_store._parse` always retains the normalised row in `by_id`, so direct endpoint inspection
can return its story, but excludes it from `endpoints`, the source for search, browse, capability
counts and platform eligibility. On a direct endpoint-id call, `_resolve_marketplace_call` raises an
actionable 410 before choosing or loading any credential; the pre-relay audit class is `retired`.
`/catalog/endpoints/{id}/access` applies the same gate. This is catalog fallback only: an org tool
whose exact name matches the retired id resolves first and remains callable, and URL passthrough
never enters catalog lookup.

The validator treats the marker as a contract: only `retired` and `broken` are valid; every marker
needs a non-empty note; `status_note` and `superseded_by` cannot float without `status`; and a
successor must be a different, existing, live catalog id. A marked id is therefore an explanation,
not an alias chain or a route treg will still spend against.

### `platform_blocked:` — works upstream, but not on treg's plan

A third state sits between "offer" and "tombstone": the route works and the price is real, but
treg's own subscription cannot serve it — Akta answers every alternative-data call (jobs, posts,
website-traffic, employee-reviews, headcount-trends, product-reviews) on the shared key with a
free 403 "Your current subscription does not include access to this endpoint". Marking those
`status: broken` would be a lie (a caller's OWN key on a bigger plan serves them fine) and leaving
them unmarked sold them as platform offers — a customer ran a whole evaluation lane into that wall
of 403s before learning the gate existed. `platform_blocked: <reason>` keeps the row in discovery
but makes `platform_eligible()` refuse it, and the reason rides on the served row so every surface
can say "bring your own key" *before* the call instead of relaying the 403 after it.

- **Platform is the system the data is ABOUT**, not the API family it lives under: DataForSEO's
  `/v3/merchant/amazon/products/live/advanced` is `amazon`, not `merchant`. Anything not tied to
  one system is `web`. Every new slug goes into `capabilities.yaml`'s `platforms` in the same
  change — the script exits non-zero if a generated platform is unknown, which is the guard.
- **Normalise slugs across providers.** Just One API calls it `douyin-tiktok-china` and TikHub
  calls it `douyin`; if both don't land on `douyin`, the marketplace shelf splits in two and the
  cross-provider comparison the catalog exists for silently stops working.

### The first-party OAuth wave (2026-07-28; Google Tag Manager added 2026-08-27)

The scraper providers sell breadth and their extended tier reads as a menu. The nine providers
where treg owns the OAuth app are the opposite question — *what can this one connected account
actually do?* — and their sources differ per provider:

| service | source | entries | scope gaps |
|---|---|---|---|
| google-search-console | searchconsole v1 discovery | 7 | 0 |
| google-analytics | analyticsdata + analyticsadmin v1beta discovery | 63 (55 on the admin host) | 32 |
| google-tag-manager | tagmanager v2 discovery | 98 | 8 |
| google-business-profile | six My Business discovery docs + 7 hand-listed legacy v4 routes | 60 (45 off-host) | n/a |
| youtube | youtube v3 discovery + the published quota-cost table | 76 | 2 |
| google-ads | the GAQL resource reference — one entry per queryable resource | 42 | 0 |
| x | X's own v2 OpenAPI | 168 | 91 |
| facebook / instagram / meta-ads | hand-curated from the Graph HTML reference | 26 / 22 / 34 | 6 / 2 / 8 |

Three things generalise from it:

- **Google publishes a Discovery document for every API** at
  `https://<service>.googleapis.com/$discovery/rest?version=<v>` — httpMethod, flatPath, a
  description, the full typed parameter list with required flags, and the OAuth scopes each method
  accepts. It is the same class of source as an OpenAPI spec and should always be preferred to the
  HTML reference. Scopes are ALTERNATIVES (holding any one suffices), so coverage is an
  intersection, not a subset. The My Business documents are the exception that declares no scopes
  at all, which is why that provider has no computable gaps.
- **Google Tag Manager keeps risky administration outside the grant.** Its core catalog presents an
  audit → workspace edit → version/publish workflow across cumulative `read`/`write`/`manage` tiers.
  The generated catalog still lists methods requiring container deletion or account/user management,
  but marks all eight with `scope_gap`; those three scopes are intentionally never requested.
- **Google Ads is a resource list, not a route list.** One endpoint (`googleAds:searchStream`)
  answers every read and what varies is the GAQL `FROM` clause, so the unit of coverage is the
  queryable resource. Forty entries share a path and differ in `input.note` and `docs_url`.
- **No test_request anywhere in this wave.** Every route needs a property id, a customer id or a
  Page id that belongs to the connected business and that no spec can supply. They are verified by
  replay against a live connection (`--via-treg`), not by a generated blind call.
- **Instagram Messaging is deliberately core-curated.** Conversation listing and message sending
  carry the Page-token/IGSID/window constraints and complete Try-form inputs in `instagram.yaml`;
  conversation listing targets the linked Facebook Page id (`/{page_id}/conversations` with
  `platform=instagram`), and replies use that Page's `/{page_id}/messages` edge—not the Instagram
  account id used by profile/media routes. The send route remains explicitly unverified so no
  catalog sweep can deliver a real DM. The Instagram generator omits these two messaging routes;
  they exist only in core. Meta's exact `(method, path)` core-wins dedup still protects all other
  generated routes whose placeholder names carry different Graph object semantics.

## Process — bulk-verifying the extended tier

```
TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify_extended.py tikhub --dry-run
TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify_extended.py tikhub --budget 1.80
uv run python scripts/catalog_validate.py            # must exit 0
```

`--dry-run` prints the queue and what it would cost at list price; nothing is called. The real run
goes CHEAPEST FIRST and stops before any call that would push the run past `--budget`, so a
half-finished run has verified the cheap majority rather than an arbitrary slice. Results are
written back into the yaml after every run and a re-run skips what already carries `verified`,
which makes an interrupted run resumable instead of a repeat bill.

Three things to know before pointing it at a new provider:

- **A missing `cost` reads as free, and silently disables `--budget`.** DataForSEO publishes prices
  per API family, so not one of its 216 extended entries carries a `cost` block — which made the
  spend cap inert: a run queued the whole platform at an estimated $0.000 and still spent real
  money, with only the after-the-fact balance readback noticing. The fix is `observed_cost`: the
  charge the provider states in its own response (`tasks.0.cost`), written onto the endpoint at
  verify time and used to budget the next run. It is the better number regardless — measured, not
  transcribed from a price list — and summing it gives a defensible run total, which balance
  arithmetic cannot because it cannot separate our calls from anything else using the same key.
  DataForSEO's full sweep, summed this way: $4.85521 across 177 endpoints.
- **`observed_time` is measured, not read.** The wall-clock seconds WE waited for the response,
  recorded on the endpoint next to `observed_cost`. Two reasons it is not lifted out of the body:
  only DataForSEO reports its own duration, and TikHub's `time` field is a TIMESTAMP
  ("2026-07-27 23:27:48"), so an extractor trusting the field name would write a date into a
  numeric column. It is also the number that matters — `CALL_TIMEOUT` applies to OUR client. Worth
  having because a timeout is recorded as the endpoint's verdict, and the same DataForSEO route can
  swing wildly: `merchant/amazon/sellers/live/advanced` answered in 9s and 105s on two identical
  calls, `products/live/advanced` in 26s and 55s. Under the old 60s ceiling both were coin flips
  that would have written "unverified" onto a healthy route on some runs and not others. Elapsed
  time predicts nothing about price, either: a 0.04s call cost 4x a 26s one.
- **Cost accounting assumes the provider bills per success.** The run's spend is the sum of the
  prices of the calls that returned 2xx. If a provider bills per *call*, that is wrong in the
  optimistic direction — check the balance delta the script prints against its own estimate before
  trusting a large run. It reads the balance before and after for exactly this reason.
- **The parameter source has to give example VALUES, not just names.** A generated test request
  that invents an id verifies nothing: it produces a 404 that looks like a broken endpoint. If the
  provider documents parameters without examples, the honest output is `untestable`, not a guess.
  (For TikHub, the values come from `sampleValue` in their Apifox docs API — their own demo values.)
- **Examples are trimmed to ~2 KB, arrays to one item.** At 1385 endpoints, core's 10 KB cap would
  add ~14 MB of JSON. An extended example is there to show the response SHAPE.
- **Check for a PER-ROUTE rate limit, not just the account-wide one.** TikHub allows 10 req/s on
  the account but only 1 req/s on any single route. A global pacer does nothing about that — it
  spaces consecutive requests across *different* routes — while a retry by definition hits the same
  route again. Retrying after 0.5s therefore guarantees a 429, and the 429 lands in the file as
  though the endpoint had failed: 66 endpoints on the first full run carried a rate-limit verdict
  that said nothing about the endpoint. Any same-route retry has to wait out that window
  (`PER_ROUTE_GAP`), and 429 must count as retryable rather than as an answer.

⚠️ Read the recorded failures before believing them. A `unverified:` line is evidence about one
call at one moment, and the failure modes that look identical in a summary count are not: a 400
that repeats is a verdict, a 400 that passes on the third try is a flaky upstream (TikHub's
LinkedIn family), and a 429 is usually our own fault. Grouping the failures by status code and by
platform family, then re-running one family, is what separates them — pass rates per platform in
the same run ranged from 8% to 100%, and the low ones were mostly not the provider's fault.

**A fix landing mid-sweep leaves the un-noticed batches wrong.** DataForSEO's 8-batch sweep ran
across the moment the `/v3/v3` URL bug (see below) was fixed. The `web` batch failed loudly at 100%
and was re-run after the fix; the `amazon` batch had failed the same way, nobody re-ran it, and its
pre-fix results merged into the file as 7 endpoints marked `unverified: http 404` — which then read
as a retired Amazon route family. All 7 passed on a re-run, first try, for $0.075. Nothing was ever
wrong with them.

Two signals identified it, and both are worth checking before believing any block of failures:
- **The failures aligned exactly with a batch boundary.** `amazon` was the only platform in the
  file with a single `unverified`, and it held 100% of what that batch touched. Endpoint problems
  do not respect our batching; tooling problems do.
- **Siblings verified by a DIFFERENT code path passed.** `dataforseo_labs/amazon/ranked_keywords`
  and `merchant/amazon/asin` were green in the same two families, verified earlier by
  `catalog_verify.py` rather than the bulk runner. A family cannot be both retired and working, so
  the disagreement was between our two callers, not about the endpoints.

The general rule: after fixing a bug that could have produced failures, re-run **every** batch that
ran before the fix, not just the one whose failure you noticed. The loud batch is the one you
already know about; the quiet ones are what ship a false verdict into the catalog.

**How many passes, and when to stop.** On TikHub, LinkedIn went 8% → 27% → 67% → 90% verified over
four passes with no change other than being asked again — 43 of 48 endpoints that a single pass
called broken. Conversion per pass is the stopping signal, not a pass count: 672, +30, +16, +14,
+2. A pass that converts ~2 is convergence, and what remains after it is genuinely broken (for
tikhub, 107 of the final 115 failures are the provider's own "Request failed. Please retry." after
six attempts each). Raising `--retry-attempts` is the cheapest lever available on a flaky provider
and costs nothing but wall-clock under per-success billing.

Just One API shows the same curve from its far end, and what a *confirmed* verdict costs to
establish. Its 13 failures were one uniform error, `code 301 COLLECT FAILED`, clustered in whole
families (Kuaishou, Taobao, JD) — the exact shape that ought to mean "our fault". They survived 3
retries inside a call, then 4 runs, then a serial pass hours later, then a sixth with retry depth
raised 3 → 6: the last two passes converted one endpoint each, for ¥0.35. Same decay, further
along, so its 11 survivors are evidenced verdicts rather than impatience. The rule is therefore not
"retry until it works" but **retry until the result stops changing**.

Two things generalise from that. The one endpoint that flipped was LinkedIn — the family that is
also flaky through TikHub, a different vendor entirely. That is the scraped platform defending
itself, not the API vendor, so expect it from anyone scraping LinkedIn, and treat two LinkedIn
scrapers as one point of failure rather than a redundant pair. And retrying is only free under
`per_success` billing (both social providers); on a `per_call` provider like DataForSEO each retry
and each extra pass is a purchase, so that budget belongs in the plan rather than in a loop.

One caveat on reading `per_success` as "bad input is free": the provider decides what counts as
success. TikHub answers some invalid inputs (a bogus channel id) with HTTP **200**, the error nested
in the body, and "this request will incur a charge" — so the platform meter bills it, faithfully to
what TikHub charges us. When TikHub uses a real 4xx it says "You won't be charged" and the meter
releases the hold. Verified live 2026-07-30.

Then read a sample of the captured examples for PII before committing, as with core — bulk capture
does not remove the scrub step, it just means sampling per platform family rather than reading all
of them.

### `skipped:` — the fourth state, for a call that was affordable but not made

`verified` / `unverified` / `untestable` above cover *passed*, *called and failed*, and *no call is
possible*. Verifying two paid providers against nearly-empty accounts surfaced a fourth case they
cannot express: the test request exists, the call would very likely pass, and it was deliberately
NOT made because the balance was needed elsewhere. Calling that `untestable` is a lie about the
endpoint, and `unverified` is a lie about the provider — it invents a failure that never happened.

```yaml
  skipped: family verified via dataforseo.x.backlinks-summary-live; the DataForSEO account held
    $0.739 on 2026-07-28 and $0.58 of it was spent verifying one endpoint per API family
```

The reason must say what to do about it, which in practice is one of: **the sibling that WAS
verified** (whole-family skips — 155 of DataForSEO's 216, 6 of Just One API's WeChat endpoints at
¥1.0–1.5/call), or **why one call is too expensive to justify** (DataForSEO's `llm_responses`
routes exceed the $0.15/call ceiling). A `skipped` entry needs no re-investigation — only money —
so a top-up plus a re-run clears them in bulk, while `unverified` and `untestable` need a human.

The state distribution is itself the report. Just One API: 227 verified, 21 unverified (11 of them
the provider's own `code 301 COLLECT FAILED` after six passes, 5 `NO PERMISSION` — an account fact,
not an endpoint fact), 6 untestable, 6 skipped. DataForSEO after its top-up and full sweep: 177
verified, 0 unverified, 39 untestable, 0 skipped — nothing in its extended surface is broken, and
every remaining gap is structural (23 routes whose spec ships no example body, 16 on_page routes
needing an async crawl id).

### Two provider-specific traps

- **Chained ids.** Most detail endpoints need an id no spec can supply. The working order is:
  call the search/list endpoints first, harvest ids out of their responses by normalised field
  name (`aweme_id` fills `awemeId`), then call the detail endpoints, then repeat — Just One API's
  WeChat Channels comments need an `objectId` that only exists after search → `convert-export-id`.
  35 of 260 endpoints were verified only because of that second and third pass.
- **A query token is not always a token.** Just One API's POST routes take
  `application/x-www-form-urlencoded` and read the credential from the FORM BODY; leaving it in
  the query string as well makes them fail with a misleading `TOKEN INVALID/UNACTIVATE`.
  `catalog_verify.py --extended` moves it for `bodyType: form` entries.

### DataForSEO paths carry `/v3`, its base_url ends in `/v3`

`catalog_ingest.py` writes DataForSEO's extended paths exactly as the spec spells them
(`/v3/serp/google/organic/live/regular`) while `OAuthProvider.base_url` already ends in `/v3` and
the core file's paths are relative to it (`/serp/google/organic/live/regular`). Two consequences:
`catalog_verify.py --extended` strips a leading duplicate of the base_url path before calling, and
**the ingester's core-wins dedup silently misses**, because it compares `(method, path)` across the
two spellings — every DataForSEO route curated in core is also present in the extended file under
a different id. Fixing that belongs in `catalog_ingest.py` and needs a regeneration.

## Choosing between providers (`domain/catalog/stats.py`)

307 capabilities are served by more than one provider, and prices inside one capability differ by up
to **261×**. So "which provider" is a real decision, made on every call.

**The agent makes it, not treg** — see `docs/CAPABILITY-CHOICE-PLAN.md` for the measurement behind
that. Two reasons, and the second is the load-bearing one. Providers of the same capability take
*different requests* (only 5 of 171 match exactly), so a router would need a canonical schema treg
does not have; and they sometimes ask a different QUESTION entirely — `hunter.people.email.find`
wants a domain and a name, `leadmagic.x.b2b-profile-email` wants a LinkedIn URL. **Only the caller
knows which inputs it holds.** A router picking on price would choose the second for someone holding
a name, and fail. Routing would also have been the first feature to break the founding rule that treg
relays rather than models.

What treg owes instead is the half only treg can supply, because only treg sees every call from every
tenant: `endpoint_stats.observed()` aggregates **success rate, p50/p95 latency, last-answered and
sample size** per endpoint from `CallRecord` — which has recorded `endpoint_id`, `status_code` and
`duration_ms` since the marketplace shipped and was never read. It rides on
`/catalog/endpoints/{id}`, attached to the endpoint **and every sibling**, because the choice is made
on that page and an agent will not make a second round-trip to compare reliability.

The aggregate is authoritative but no longer request-time. `stats.EndpointObservationReader` is the
narrow domain port, and bootstrap supplies `CachedEndpointObservationReader` around a
`PostgresEndpointObservationReader`. Entries are keyed by endpoint id. They are fresh for five
minutes; from five through thirty minutes HTTP and MCP search serve the old value immediately and
start a refresh; after thirty minutes they publish no observation until a refresh succeeds. A cold
process therefore answers the first requests without reliability weighting instead of making either
Catalog entry point wait for Postgres. The API shape does not change: `observed` is `null` when no
acceptable entry exists.

Refresh is process-level singleflight. Concurrent misses join one shared Task, duplicate endpoint ids
already in flight are not queued again, and the Task batches the requested ids. Its
`PostgresEndpointObservationReader` opens an independent session only around `stats.observed()` and
closes it as soon as the two queries finish. HTTP `/catalog/search`, both MCP catalog-search tools,
routed planning in `application.call.route.build_plan`, and the prose pages that print observed stats
(`/use-cases/*`, `/workflows` and `/workflows/*`) receive the same reader instance from bootstrap, so
their request paths have no observation DB dependency, check out zero connections, and join the same
refresh Task. A refresh failure keeps stale
entries, backs off before retry, and never changes the Catalog response status; a failure with no
cached entry is honest emptiness. The adapter exposes entry-level `fresh`, `stale`, and `miss`
counters plus `refresh` and `refresh_failure` counts. Its invalidation story is the two TTLs: deploys
and process restarts begin cold, and no cross-instance correctness depends on the cache.

Five rules worth keeping:

- **A 4xx never counts against the provider.** It usually means the caller sent bad parameters;
  counting it would let one agent's mistake make a healthy endpoint look broken to everyone. Only
  2xx versus 5xx decides the rate.
- **405 is the exception, and the rule's own justification is why.** "The caller sent bad
  parameters" cannot apply to a method the caller was never allowed to choose: `/call/` refuses a
  catalog call whose method differs from the recorded one with a 400, *before* relaying. So a 405
  coming back from the provider says the RECORDED METHOD is wrong — a stale contract, which is the
  one thing this module exists to surface — and it counts as decided against the endpoint. Without
  it, the seven straight 405s on `tikhub.x.tiktok-ads-search-ads` sat in the excluded bucket and the
  WORKS column read `— (7)`: indistinguishable from an endpoint nobody had tried. That is the half
  of the 2026-08-17 report that survived two rounds of review — fixing `LAST OK` stopped the row
  claiming success, but only this makes it say *failure*.
- **A treg refusal is not evidence about the endpoint.** Rows with `refused_by` set (a paywall 402,
  a daily-cap 429 — see the data-model fragment) never reached the provider; they are excluded even
  from `samples`, or a burst of refused calls dresses itself up as traffic. The 2026-08-12 Hunter
  incident — 309 refusals next to 488 real calls — is why.
- **`miss` semantics ride on the endpoint.** Some providers answer "asked and answered: no result"
  with an error status (PDL 404s a person it has no record of; Hunter's combined-find does the
  same). Endpoints with evidenced miss behaviour carry a `miss: {status, means}` block in their
  YAML, surfaced through `endpoint_view` — so an agent reads "404 = no match, don't retry" instead
  of treating an expected empty answer as a failure. Only annotate what the wire has demonstrated.
  **The router reads the same block** (`route._miss_status`): a child answering the declared
  4xx is a MISS — the waterfall goes on and a fully-missed call ends as a 200 miss, never
  `route_failed`. Before 2026-09-04 only PDL carried the block; the annotated set (aviato, hunter,
  leadmagic, findymail, companyenrich, thecompaniesapi, fiber-ai, scrapecreators linkedin) came
  from 30 days of prod children answering 404 with a "not found" body, and the router treated each
  as a rejected request: 1,824 `phone.find` parents were 502 in that window, 768 of them with no
  failure but an aviato 404 (voice-ai-outbound's GT report). Only a 4xx is honoured — a
  `status: 200` block (tikhub) is agent documentation; the adapter's own `miss` predicate decides
  a 2xx. Note a `per_call` provider (companyenrich) still bills the request on its declared miss.
- **Below `MIN_SAMPLES` we publish the count and nothing else.** "100% from two calls" is noise
  dressed as evidence, and on a quiet endpoint a rate could expose one org's activity. The floor
  applies to **decided calls** (2xx + provider-fault failures), not total traffic: four caller 422s
  cannot lift one 200 or 405 into a published rate. Latency has its own floor of successful calls;
  one success is not both a p50 and p95 merely because enough failures made the rate publishable.
- **Sample size is always visible**, so `100% (8)` cannot beat `99% (121)` by looking rounder.
- **"Free" is a price, not a missing one.** `platform_eligible` used to demand
  `confidence in (verified, documented)` for every route, but `confidence` says how much we trust a
  NUMBER we are about to charge — and a free route has no number. Requiring it anyway refused 61
  endpoints across 8 providers (28 of Hunter's 35) as though "costs nothing" meant "we don't know",
  which is the one distinction this file otherwise keeps apart. A `type: free` route is now eligible
  without provenance; a PAID route without provenance is still refused.
- **A claim never wears a measurement's badge.** The `LAST OK` column prints a bare age when a real
  call produced it and a **`✓` age** when it came from the catalog's `verified:` stamp — the same
  discipline `confidence:` already applies to price. The stamp is the cold-start answer: it covers
  1,380 of 1,810 eligible endpoints for free, which is why the column is useful on day one.
- **`last_ok` means the last SUCCESS.** It was `max(created_at)` over every row, success or not, so
  an endpoint that had been called seven times today and failed all seven read `WORKS — (7)` next
  to `LAST OK: today` — which is how `tikhub.x.tiktok-ads-search-ads` passed for a merely new row
  while being uncallable (2026-08-17).
- **Below the floor, the outcome stays unpublished — not even a yes/no.** The 2026-08-17 fix first
  added `any_ok` ("has it EVER answered?") on the argument that a boolean survives any sample size.
  It did not survive the two rules above. On a quiet endpoint it exposed the *outcome* of one
  tenant's one call, which is half of why the floor exists; and because `samples` counts 4xx while
  successes do not, a single caller's malformed 422 published `any_ok: false` and made a healthy
  endpoint look broken to every other tenant — precisely the failure the 4xx rule prevents. It was
  removed. "Never worked" is read off `ok_rate == 0`, which is computed from DECIDED samples only,
  so no volume of caller errors can produce it.

### Search scoring — most words must match, and the rare ones decide

`catalog_store.search` demanded EVERY query token match (AND). Right for the 2–3 word refinement
("tiktok comments" must not return every tiktok endpoint), and fatal for how agents actually query:
the day the SearchMiss log shipped it recorded "company job postings hiring open jobs linkedin" → 0
results while three endpoints matched 6 of the 7 words. The only misses were "linkedin" on rows
shelved under `companies` (the agent names where the data lives, the catalog names what it is), and
"open" on the row shelved under `linkedin`. Since 2026-08-20 a query may miss one token in every
three (1–2 words: all still required), and each matched token scores its field weight times its BM25
idf — "by" matches 558 endpoints and is worth ~nothing, "postings" matches 4 and decides the order.
That asymmetry is also what keeps the miss allowance safe: dropping a rare word costs more score
than dropping filler, so full-match fluff cannot outrank a near-match on substance. Rows matching
the same tokens in the same fields still sum identical floats, so the tie band below keeps working.
Query-side layers close what scoring alone cannot. Function words ("on", "this", "what") and
single-letter tokens ("K&L" tokenizes to k + l, df 2,000+) are dropped before the miss allowance is
computed — they select nothing, but each one raised the number of real words a row had to match.
Tokens matching over `SOFT_DF_SHARE` (25%) of the catalog ("data" 33%, "api" 50%, "get" 40%) are
SOFT: they still add score where they match, but a row is never punished for missing them — a
statistical stopword list no hand list would keep up with. And `aliases.yaml` bridges vocabulary:
substring containment only works in one direction, so "cryptocurrency" never finds the catalog's
"crypto" without the map. A token matches under its own spelling or any curated alias, same field
weight. NOUNS ONLY: aliasing a verb to a commoner verb poisons the key (`lookup: [search, find]`
inflated lookup's match set 27 → 689 endpoints and destroyed its ranking power). The file is
query-side only — it rewrites no provider text, survives every re-ingest, and the validator
(`check_aliases`) rejects entries that could not survive the tokenizer and warns on aliases whose
target occurs nowhere in the catalog. The tokenizer also retains contiguous CJK text, so Chinese
task-phrase aliases are real searchable keys rather than discarded punctuation spans. Alias keys
remain one token, while targets may be lowercase hyphenated phrases because matching tests the
target string directly against catalog text. This makes `t2v` → `text-to-video` selective. The
original AIGC aliases mapped model names and Chinese task phrases to `video`/`image`: live
`treg catalog search` expanded Hailuo/Seedance/t2v to 521 endpoints and Flux to 172, including
YouTube and unrelated image utilities. Model-family aliases were removed once real endpoint text
contained those names; compact and Chinese task terms now target only `text-to-video` and/or
`image-to-video`. A post-change CLI run returned 10 Hailuo, 11 Seedance, and 14 Flux matches; the
task aliases returned 24 for t2v, 39 for i2v, 21 for the Chinese text-to-video query, and 35 for the
Chinese image-to-video query, with generation models at the top instead of unrelated utilities.
The SearchMiss log is its feed: a zero-result query whose
words name an existing endpoint in different vocabulary is one row here.

A query token that IS a platform slug ("tiktok", "linkedin") is the caller's hard filter, but idf
prices it low — half the catalog serves the big platforms — so rows matching a rarer facet word
("followers") outranked rows matching the asked-for platform. Platform-slug tokens therefore score
DOUBLE where they match; rows matching the same tokens in the same fields still sum identical
floats, so the tie band survives.

A zero-result answer surfaces its `near_misses` — the rows that just missed the admission gate,
with the exact words each one matched and missed ("apollo.companies.jobs matches job, hiring,
signal; misses law, firm"). The matcher had already computed this; discarding it and answering
with prose was the least useful thing the data allowed. Served structured over MCP (`near`), in
the HTTP route's `near` + a hint line, and as "almost:" lines in the CLI — the caller is usually
an LLM, and told exactly what to drop it re-queries correctly on the next call.

`scripts/search_bench.py` is the labeled replay (30 agent-shaped queries): sentence-style hit@8 went
14% → 100% (hit@1 64%, MRR .766) with the 8 short-query regression rows byte-identical. The residue
past this is semantic matching — an embedding model — which the bench so far says is not needed.

### The evidence decides the ORDER, not just the detail page

Token scoring ties by the dozen — all 24 `"ad library"` matches score alike — so "which 8 do I show?"
was answered by file order. That returned seven near-duplicate tikhub rows (one of them the
uncallable one above) and cut off `scrapecreators.x.v1-tiktok-ad-library-search`, cheaper and 17 for
17 measured. `catalog_store.rerank()` now settles equal scores over the band `rank_band()` returns, on
buckets rather than a weighted formula (an ok_rate and a price are different units, and a blended
score is one nobody can predict or argue with):

**relevance → measured (good · unknown · poor · never-worked) → core before extended → price**

where the measured bucket comes from `ok_rate` alone — `>= 0.9` good, `None` unknown, `0` never
worked, else poor — so a demotion always rests on calls the provider actually decided.

**The band takes the tie group whole.** A cut made *inside* a group of equally relevant rows is the
arbitrary cut, and reranking a slice that already dropped the best-measured row cannot put it back —
so `rank_band()` keeps taking while the score stays equal to the last row kept. That group is 17 rows
for "ad library" and 24 for "email", but 523 for the bare word "tiktok", and this is an OPEN route:
taking every group whole would put a 523-id `IN` clause behind every search. So it is bounded at
`RERANK_BAND` (250) and **says when it truncated** — `ranking_note` over MCP, a hint on the HTTP
route — because a bounded cut that announces itself is the thing this fix set out to build, and a
silent one is what it set out to remove.

Two orderings there are deliberate. Evidence outranks curation, because a core row that has never
answered is not the better suggestion. Curation outranks **price**, because `core` is the hand-picked
route and `extended` the bulk-ingested long tail — letting a tenth of a cent promote the tail made
`"tiktok comments"` lead with douyin danmaku. Unmeasured sits above measured-poor: a new endpoint is
an unknown, not a suspect.

Team policy sits on top: `CapabilityPin` (see [data-model](data-model.md)) lets an org fix a
capability to one provider, enforced in `_resolve_marketplace_call` before anything is reserved.

Its boundary, verified rather than assumed: a pin gates the **catalog id**, which is the only route
to treg's own key — so it cannot be side-stepped to spend our money (a URL-passthrough call resolves
against the org's OWN tools and 404s without one). A team holding its own key for another provider
can still call that provider by URL; that is their credential and their bill, and `DenyRule` —
host-scoped, applied to every shape of call — is the tool for blocking it.

### Routed groups in discovery — a search page is a list of JOBS (2026-08-28)

Three rules, all in `group_routed` / `search`, shared by `/catalog/search`, MCP `catalog_search`
and the CLI so the three surfaces cannot disagree:

- **A matched child brings its routed parent.** `find leads` matched `leadsforge.*` on the
  provider's NAME; the row an agent should see first for that job — `treg.people.search`, where
  treg chooses among every provider — contained no word of the query. `search` now adds
  `treg.<capability>` at the best child's score whenever a child matched. The token-filter tests
  exempt these pulled-in rows: their own text need not contain the query.
- **Vocabulary before ranking.** The same query first ranked `people.email.find` above
  `people.search` because `find` is a token of the former's capability NAME (weight 3) and only of
  the latter's summary (weight 2). The fix was to say in `capabilities.yaml` what the job is —
  `people.search` is "lead lists and prospects (sales leads)" — not to bend the scorer; `aliases.yaml`
  then only needs `lead → leads`, `prospect → leads, prospects`.
- **A group shows its best `MAX_ROUTED_CHILDREN` (5) children.** One capability's 24 providers had
  eaten the whole 25-row page. The parent is stamped `children_hidden`; the CLI prints
  `+ N more providers — treg catalog get <parent>`, MCP says so in `routed`. To keep the page full
  after collapsing, search ranks a band of 4× the page (≤ 100) and cuts to `limit` AFTER grouping.

## Routing — first-party routed endpoints (`treg.<capability>`)

The one place treg **models** an upstream API, and the explicit opt-in where the caller asks treg
to choose (`docs/CAPABILITY-ROUTING-PLAN.md`). Everything else in the catalog stays verbatim relay.

- **Contracts** — `contracts.yaml`: per capability, one-of *identity* variants (structural keys,
  never provider names — `{full_name, domain}`, `{first_name, last_name, domain}`,
  `{linkedin_url}`), `derive` rules so the two name shapes match the same adapters, a small
  *output* core (`email` required; `confidence`, names, `verified` optional) and `miss` in
  canonical terms. `raw` — the winning provider's body — is always returned and never documented
  as stable.
- **Adapters** — `adapters.yaml`, one per endpoint: `accepts` (identity variants), `in` (contract
  field → `queryParams.x` / `body.x`), `const` (fixed provider params), `out` (core field →
  expression over the body), `miss`. The expression language (`domain/catalog/routing/paths.py`)
  is deliberately tiny: dotted paths with `[i]` (root `[0]`, `.` = the whole body), `coalesce`,
  `/ N`, `==`/`!=` against literals, and named transforms (`split_first`, `split_last`, `join`,
  `has_type`, `len`, `list`, `obj`, `fmt`, `csv`, `lower`/`upper`, `at_least`, `linkedin_handle`/
  `linkedin_url`, `email_domain`, `host`, `dfs_location`, `seranking_source`, `tca_filter`).
  `in_expr` builds provider params from expressions (URL-array bodies, DSL objects); `test_identity`
  states the fixture's identity when `in` builds a value rather than copying one; `filters` carry
  defaults and are always sent.
- **Verified at load, or absent** — `routing/contracts.py::verify`: `in` must reproduce the
  endpoint's own `test_request` and `out` must fill every required core field from its
  `example_response` (an example that is itself a miss passes with the hit half unverified).
  A failing adapter is not a candidate; the endpoint is still callable via `/call/` exactly as
  before. `tests/test_routing.py` pins that every shipped adapter passes.
- **The generated row** — `routing/synthetic.py`: every capability with ≥ 2 verified children gets
  `treg.<capability>` (`provider: treg`, `kind: routed`, `POST /<capability>`, `input` = the
  contract, `cost` = the children's range, `routed_children`). Never hand-written; not in any
  provider file. `catalog_get` on it returns the contract and the ranked **plan** (the quote) —
  nothing is reserved.
- **Ranking** — `routing/plan.py`: own keys (tier 2) first at cost 0; then
  `expected_cost_per_hit = cost_at(request) × P(billed) / P(hit)` where `cost_at` prices *this*
  request at its requested size (per-result × limit, credit-with-minimum rounded up) and `P(hit)`
  is the measured hit rate when ≥ 20 decided samples exist, else `ok_rate`, else 1.0 (flagged
  `unmeasured`). `build_plan` reads that evidence through bootstrap's shared process cache; cold or
  unavailable observations degrade to unmeasured ranking while the cache refreshes off the request
  path. `X-Treg-Route-Prefer` / `-Exclude` override; exhausted providers (capacity view)
  and providers with no key on the deployment are dropped and named in `dropped` (`needs {…}`
  says which identity variant a dropped child wanted).
- **Execution** — `application/call/route.py`, entered from `service._execute_call` when the
  resolved catalog row is `kind: routed`. Each attempt is a **full child `execute_call`** on a
  `CallContext` whose `call_ref` is `{parent}:r{n}` — its hold id, ladder (tiers 1/2/4/overflow),
  reserve, relay, settle, audit row and cancellation compensation are the ordinary ones. Vendor
  4xx (not 402/408/429) = usually the caller's fault, but scrapers answer 400 for their own outages
  (tikhub, live 2026-08-28), so the waterfall goes on ONLY to candidates that bill nothing for a
  rejected request — per_success, free, the org's own key, or per_call ≤ 1¢ (`CHEAP_RETRY_MICRO`)
  — never the same provider again, within the error bound; if every one rejects it, the caller
  gets `route_caller_fault` naming each attempt. A 4xx the endpoint's YAML declares as its
  "no result" status (`miss: {status: 404}`, see "`miss` semantics ride on the endpoint") is a
  MISS instead, not a fault. Our 5xx/503/429 or a vendor 5xx/429/402 = error →
  next candidate, at most two extra, only for idempotent contracts. A treg-side
  `tool_access_denied`, `policy_denied`, or `capability_pinned` refusal is local to that child and
  follows the same error fallback. A platform child's vendor 401/403 also falls back because it
  indicates treg's provider credential, not the routed caller's request. Balance and spend-cap
  refusals remain terminal because another provider cannot change the org-wide decision. A 2xx
  response whose body lacks a REQUIRED core field is a MISS, not a hit (dataforseo's `result: null`
  under a 20000 envelope). A
  MISS tries the next candidate — the waterfall is ON by default (decided
  2026-08-28: the endpoint's job is to find the thing, and misses on the per-success children are
  free); `X-Treg-Route-Waterfall: 0` stops at the first miss. Every attempt is settled at its real
  price and `X-Treg-Route-Max-Cost` (default $1) bounds the sum before each reserve (a candidate
  that would breach it is `skipped`). Response: `{output, raw, _treg: {served_by, provider, tier,
  outcome, tried[], charged_micro}}`, `X-Treg-Served-By`, `X-Treg-Providers-Tried`,
  `X-Treg-Route-Outcome`, `X-Treg-Cost-Micro` = the sum, one `X-Treg-Call-Id`. The parent owns
  the idempotency label (a success, or a terminal failure after a paid child, replays without
  touching a provider) and writes one audit row
  (`credential_tier: routed`) beside the children's.
- **Hit rate** — `CallRecord.hit` (nullable, alembic `0009`, last column) is the adapter's verdict
  written at settle; `stats.observed` publishes `hit_rate`/`hit_samples` (floor 20) and, for
  per-success endpoints, reads historical rows too (a 2xx with `cost_observed_micro == 0` is a miss).
  The plan, `catalog_get` and the CLI's HIT column read it; a registered tool (tier 1) or stored key
  (tier 2) for a provider ranks first at cost 0.
- **R0 done (2026-08-28)**: the top-traffic untagged `.x.` endpoints carry capabilities now
  (`google.serp.maps/news/local/ai_mode`, `google.keywords.trends` — each dataforseo + serpapi —
  plus `companies.jobs.search`, `companies.domain.find`, `amazon.product.sellers/variants`,
  `tiktok.video.captions`); untagged platform traffic fell from 12% to 1.4%, and 202 capabilities
  with 2+ eligible providers cover 88% of calls.
- **Ranking, specificity (2026-08-29)**: among candidates of the same tier, one that USES more of the
  keys the caller actually sent outranks a cheaper one that uses fewer — `{company_domain, title}`
  goes to a title-aware search, not a free domain-only one that would answer the whole company.
  Only caller-supplied keys count (`rank(given=…)`), never keys reached through `derive`. Price
  decides among equals.
- **Ranking, dropped filters (2026-08-29)**: a candidate whose adapter cannot express a filter the
  caller SENT ranks below every candidate that can — `len(candidate.ignored)` sits in `rank()`'s key
  between specificity and price. It answers a LOOSER question, and a non-empty answer to the looser
  question still passes `adapter.miss`, so cheapness alone must never buy it. Found live: a
  `people.search` for `{q, title, location: "London, United Kingdom", country: GB}` went to the
  cheapest child, which mapped neither geo filter, and returned people in Bengaluru and San
  Francisco — reported as a hit, $0.0025, no signal to the caller. `ignored_filters()` is pure and
  computed at PLANNING time (`routing/plan.py`), so the ranking and the per-attempt report read the
  same set. The provider stays reachable: it still wins when nothing better is callable, and price
  still decides among candidates that ignore equally much.
  **Coverage caveat**: of 16 `people.search` children, only icypeas maps geo today, so the rule
  currently floats one provider. lusha, crustdata, companyenrich and leadmagic all filter on
  location upstream — their adapters just do not map it. Until they do, the rule is doing more work
  than it should have to.
- **A contract that cannot say what the brief says (2026-08-29)**: `people.search` exposed only
  `{q, company_domain, title, full_name}` + `{country, location, limit}`, while icypeas natively
  filters on `keyword`, `skills`, `pastJobTitle`, `school`, `languages` and
  `totalYearsOfExperience`. And `q` is IDENTITY, so when icypeas matched the `{title}` variant the
  free text was never sent — a routed search for "backend developers in London with microservices"
  reached the provider as `title + location`, with the requirement dropped. Every failing bench
  query had this shape ("football scouting analysts" → `title="Football Analyst"`, 15 rows, 0
  qualified). `keywords` is now a FILTER (filters always travel; identity does not) mapped to
  icypeas `query.keyword.include`, leadsforge's keyword field, and folded into exa's semantic query.
  Measured on the bench's 30 recruiting briefs: **55.4 → 69.2 overall** (nDCG@10 51.2 → 64.3,
  coverage 49.7 → 68.1), failing queries 8 → 2.
  A `titles` list filter was tried at the same time and **REVERTED**: paired over the same 30
  queries it cost −0.068 ± 0.022 (95% CI [−0.112, −0.024]). Broader title variants ("Software
  Engineer" for a backend brief) buy recall the metric does not want and lose precision.
- **min_results, and why it is bounded (2026-08-29)**: `X-Treg-Route-Min-Results: N` records a hit
  with fewer than N rows as `weak` and keeps going, returning the fullest answer seen. It is what
  the hand-written bench policy did (`if len(rows) < 3 -> semantic fallback`) and the routed path
  could not express. Unbounded it is ruinous on LOOKUP briefs, whose honest answer IS one person:
  nothing ever clears the bar, so every call pays the whole ladder — the bench's deterministic set
  went **$1.76 → $22.35 over 28 queries, 12.7x**, for answers that were already right. Bounded at
  `MAX_WEAK_FALLBACKS = 2`, mirroring the error fallback, the same set costs ~$0.39 — cheaper than
  the baseline — and recruiting keeps its gain (it never needed more than one fall-through).
  Pair it with `X-Treg-Route-Max-Cost` on any capability where thin answers are normal.
- **Routed parity with a hand-written policy (2026-08-29)**: after the two changes above, paired
  over the same 30 recruiting briefs against the 08-27 hand-written icypeas policy, the routed path
  is indistinguishable — nDCG@10 −2.40 (95% CI [−7.12, +2.33]), utility −0.80 ([−3.10, +1.51]),
  qualified/query −0.53 ([−1.84, +0.77]) — and ahead of the published Lessie 68.2, Exa 64.7 and
  Claude Code 50.5. The remaining differences are agent-side, not routing: the hand-written policy
  post-filtered rows on location and over-fetched (`size: 20`, trimmed to 15).
- **The answer says what it ignored (2026-08-29)**: `ignored_filters` was on `_treg.tried[]` only,
  which no caller reads. It is now also on `_treg` itself for the child that served and on an
  `X-Treg-Ignored-Filters` response header, so an agent can post-filter, or say why the rows are
  wrong, without walking the attempt list. **Opt-in refusal (2026-09-04)**: `X-Treg-Route-Strict-Filters: 1`
  drops every candidate that cannot express a sent filter at planning time (listed in `dropped`
  with `strict: true` and what the adapter takes instead) and answers `route_no_candidate` 422,
  unbilled, when none is left — a 503 stays reserved for capacity/key drops. Off by default: the
  ignored-but-billed call (`{full_name, country: GT}` → New York, voice-ai-outbound 2026-09-03) is
  the documented behaviour, and the fix for that case was to give the candidate the filter.
- **Lusha is the sixth phone rung (2026-09-04)**: `lusha.people.phone.find` — the phone-only view
  of search-and-enrich — accepts every phone.find identity and ranks last on price (6 credits a
  hit; a miss free, a matched-but-no-number profile the 1-credit search, all settled from
  `billing.creditsCharged`). Added for LatAm coverage after a Guatemala test found 7 in 44 across
  the other five. Apollo cannot join: its phone reveal is webhook-only, never inline.
- **people.\* sweep (2026-08-29)**: people.search 6 → 16 children (aviato dsl/simple, companyenrich
  scroll, crustdata, fiber-ai, leadsforge, leadmagic search + role-finder, findymail employees +
  domain — the last retagged from email.find, it returns a list), people.enrich 9 → 14 (aviato bulk,
  fiber-ai, tomba profile/combined, hunter combined-find), people.email.find 9 → 11 (fiber-ai turbo,
  leadmagic personal), identity.resolve 3 → 4 (findymail reverse-email); five examples captured live.
  Still out: apollo/coresignal people.search (no fixture; apollo's `person_titles[]` needs a
  bracket-safe target), crustdata/diffbot people.enrich (truncated examples), the `*.bulk` jobs
  (async), hunter multi-domain (masked rows).
- **Filters reach providers, or say they did not (2026-08-29)**: `country` (ISO code) becomes a name
  through `country_name` (`catalog/countries.json`, 249 rows generated from pycountry) for providers
  that filter on a location NAME (icypeas); `location` is a free-text pass-through ("London, United
  Kingdom", "Europe") for the same providers; a filter the caller sent that an adapter never mentions
  is listed on the attempt as `ignored_filters` — silently unapplied was the worst outcome (the bench
  had post-filtered in the agent because of it). Bench re-run, recruiting 30: same icypeas rows as the
  hand-written policy, one automatic fall-through, region briefs rescued by the pass-through.
- **Routed DISCOVERY is a runtime switch (2026-08-29)**: `TREG_ROUTED_DISCOVERY=off` (default `on`)
  stops search leading with `treg.<capability>` and stops a routed parent riding in when a child
  matches — the endpoints stay callable, priced and reachable by id, and `catalog get`/`POST /call/`
  are untouched. Off also HIDES routed rows from search results, not merely ungroups them: a routed
  row matches a keyword query on its own summary, so leaving it in would steer by the back door.
  One choke point (`group_routed`) serves both callers (`mcp.py`, `routers/catalog.py`); MCP also
  narrows its rank band back to `limit` when off, since the widening exists only so groups can
  collapse. Same dashboard-flip shape as `platform_providers` and `TREG_OVERFLOW_MODE` — no
  redeploy. It covers every surface that steers, not just search: the platform BROWSE view
  (`/catalog/platforms/{slug}`, which sorts the routed parent to the top of its capability group)
  drops routed rows too, and `/skill.md` and `/llms.txt` strip their routed section — a deployment
  that hides the row from search must not keep TEACHING agents to call it, or the docs and the
  catalog disagree and the agent believes the docs. The section is delimited in those two files by
  `<!--routed-->…<!--/routed-->`; the markers are stripped either way, and the unrelated overflow /
  `provider_capacity_unavailable` guidance in the same paragraphs is kept (it came from the capacity
  work, not from routing — which is also why a `git revert` of #242 would be the wrong instrument). It exists because "does the router answer well" and "should every agent be led to it by
  default" are separate questions: the bench answered the first (55.4 → 69.2 on recruiting, parity
  with a hand-written policy), and only traffic can answer the second.
- **creators.search: routed, then UNROUTED (2026-08-31)**: the contract was added because
  influencersclub filters on `location` / `keywords_in_bio` / `number_of_followers` and returns that
  data inline while `exa.creators.search` returns a URL and a title, so an agent picking blind chose
  exa and then verified follower counts by hand. Measured, the contract made the bench category
  WORSE: influencer 54.4 → 49.3, queries answered 29 → 27, and the profile-verification calls it was
  meant to remove went UP (131 → 155). The cost fell 42% ($10.96 → $6.34), which is the only part
  that held. Best explanation, same shape as the reverted `titles` filter: sending the follower band
  and location as HARD filters over-constrains, and a metric that pads to K=15 pays for volume — five
  exact matches score below fifteen loose ones. Reverted so production matches the submitted bench
  data. If it returns, the filters should be opt-in rather than always-sent, and measured first.
- **What is not routed on purpose**: `*.bulk` endpoints (a routed call is one subject, one answer),
  and providers whose rows are teasers — hunter multi-domain (masked, no names, ignores limit),
  apollo people.search (free, but last names obfuscated: a search→reveal CHAIN, which mode C of the
  bench showed rescues hard B2B briefs and which the router does not do yet). `catalog get` lists
  them under ALSO with the rest of the same-job endpoints that have no adapter; the search page's
  "+N more" points there. The routed row's example body and `/access` dry-run use the identity
  variant MOST children accept, and the dry-run tries every variant before saying "unservable".
  That dry-run is ONE identity shape, so its drops are mostly "this adapter takes another identity",
  not "your team cannot reach this provider" — `/access` used to label them "not available here",
  which read as a missing key and sent a reader hunting for one (2026-08-29: aviato, callable on
  treg's platform key and serving live calls, was listed as unavailable). It now names the shape and
  gives each drop its own `why`.
- **Coverage (2026-08-28)**: 74 routed capabilities = 80.9% of 30-day platform calls (88% was the
  routable ceiling). The per-capability ledger — what shipped with which children, what is 🚫 and
  why (one usable vendor, async task-post engines, identity-less feeds), and the 49 zero-traffic
  rows still open — is `docs/CAPABILITY-EXPANSION.md` (git-excluded, Jason's working doc).
- **Not built** (plan R4): "prefer routed" in the agent files after a shadow week; a proper
  `kind: filters` / `Location` layer for the DSL/SQL providers (aviato dsl and pdl sql ride `obj`/
  `fmt` today; crustdata/diffbot/coresignal/apollo do not); own-key-dry → treg-key fallback.

## Security

PII IS THE HARD RULE. This repo is public, and every captured example ships in it. Three checks
before any example is committed, all learned the hard way:

1. **No named private individuals.** Contact-lookup routes (LinkedIn contact info, people-enrichment
   by email) return a real person's name, personal email and phone. Such an endpoint stays in the
   catalog — the route is real and useful — but it is marked `untestable:` with the reason, carries
   NO `test_request` (so a re-verify cannot silently re-capture it), and no example is stored.
2. **No third-party PII riding along.** Emails and phones turn up inside unrelated payloads — a
   YouTube description, a review body. Sweep every captured example for address-shaped strings and
   mask anything that isn't a business contact.
3. **No first-party identity.** Own-account verification (`mine=true`, your own site in Search
   Console) captures YOUR channel, sitemap and metrics. Point test requests at neutral public
   targets instead, and scrub what you already captured.


Credentials are NEVER written into catalog files, examples, scripts, or docs — the verifier reads
`TREG_CATALOG_CRED` from the environment only. Captured examples are truncated (arrays → 2 items,
long strings clipped, ~10 KB cap) by the verifier, then human-reviewed for PII before commit.

## Pilot providers (first wave)

| service | platform focus | auth (from oauth_providers.py) | overlap group |
|---|---|---|---|
| dataforseo | google, web | Basic (login:password base64) | SEO: web.backlinks.*, web.url.metrics |
| exa (2026-08-27) | web, people, companies | `x-api-key` header; dollar-priced, settles from `costDollars.total` | Search: web.search*, web.contents.get, web.similar, web.answer; Enrichment: people.search, companies.search |
| moz | web | Basic (AccessID:SecretKey base64), POST JSON API | SEO: web.backlinks.*, web.url.metrics |
| tikhub | tiktok (+instagram, youtube, x) | Bearer key | Social: tiktok.* |
| justoneapi | tiktok (+instagram, xiaohongshu, weibo) | `?token=` query param | Social: tiktok.* |

The SEO pair and the social pair each implement the same capabilities on purpose — they are the
first real test that the capability taxonomy supports cross-provider comparison.
