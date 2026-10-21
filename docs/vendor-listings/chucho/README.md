# Chucho public Discover listing proposal

Contact: hello@chucho.ai

Chucho exposes published vacation rentals and boutique hotels through a free,
anonymous discovery API. Search, public facts, photos and direct host destinations
are available in English and Spanish. The proposed base URL is
`https://chucho.ai/api/public/v1`.

- [Agent guide](https://chucho.ai/agents.md)
- [OpenAPI](https://chucho.ai/openapi.json)
- [Authentication and provisioning boundaries](https://chucho.ai/auth.md)
- [MCP server card](https://chucho.ai/.well-known/mcp/server-card.json)

## Why this is a draft

The keyed-provider requirements in [VENDORS.md](../../VENDORS.md) do not fit
anonymous discovery. Public reads require no key and do not reject a supplied
garbage key. Organization API keys protect a separate private registry; they must
not be presented as credentials for searching the public catalog. There is also
no published API rate card, per-call meter or API pricing page. The proposed
zero-dollar cost is marked `inferred`, pending acceptance of the vendor's free-use
policy; anonymous success alone is not proof of a metered zero charge.

Treg already supports `platform_auth: anonymous` for verified free GET endpoints,
but its provider registry describes credential connections. This proposal does
not invent a key, an OAuth flow, a successful key probe or a verification stamp.
The files in this directory are deliberately outside the catalog loader. They
are review material and do not yet make Chucho discoverable or callable in Treg.

## Proposed catalog

[chucho.yaml](chucho.yaml) describes five distinct public GET operations from the
published OpenAPI contract: search, filter/coverage preflight, property details,
host booking links and enquiry-form inspection. Five is the useful GET surface;
duplicating operations to reach the vendor guide's usual eight-to-fifteen range
would not add capabilities. REST equivalents avoid cataloguing the MCP JSON-RPC
envelope as an individual task.

The proposed `travel` platform is in [taxonomy.yaml](taxonomy.yaml); the five
capabilities are under `proposed_capabilities` in `chucho.yaml`. Existing
Tripadvisor and Booking.com capabilities describe those platforms' data rather
than a direct-host accommodation catalog, so those platform-specific identifiers
are not reused. The PR description identifies adjacent endpoints for reviewers
considering a shared accommodation capability.

Search input preserves the current OpenAPI parameters. `countries` is a
comma-separated wire string; `semanticPreferences` is a JSON-encoded wire string.
These encodings differ, so the proposal does not declare a shared array encoding.
The REST filters endpoint has fewer parameters than MCP `get_filters`; its
catalog entry describes only its REST contract. Search pages have a fixed size
of 24; there is no invented `limit` or `pageSize` parameter.

The live MCP server also advertises `get_stay_areas` and
`search_nearby_properties`. Those MCP-only operations are outside this proposal
and are recorded in the PR surface map; no REST equivalents are invented.

The PR description contains the dated live-call ledger, exact bogus-key response,
and the full-surface inclusion/exclusion map. No example responses or maintainer
verification stamps are committed. The supplied public test IDs must be refreshed
if a host unpublishes a listing or disables its enquiry form.

## Before activation

1. Agree on an anonymous-only provider representation and the free-use/pricing
   provenance with the contact above. Any required registry mechanism belongs in
   a separate change, following the catalog-data rule in `AGENTS.md`.
2. Run independent live verification of every `test_request`. Maintainers add
   their own scrubbed examples and `verified` stamps. Do not manufacture a
   credential-verification test for an anonymous API.
3. Register the approved provider/base URL and neutral lettermark, add the
   `travel` platform, and move `chucho.yaml` into `src/treg/catalog/`. Apply
   accepted cost provenance and `platform_auth: anonymous` to verified free GET
   rows, and set each `added` date to its actual catalog admission date.
4. Run catalog validation, the added-date check, plugin checks and the test suite.
   Update the front-door catalog counts through the existing generation workflow.
   Enabling `chucho` in `TREG_PLATFORM_PROVIDERS` remains a deployment decision;
   no platform key or credit conversion is needed for these anonymous reads.

Quote and estimate POST operations are outside the current anonymous-GET contract.
Enquiry preparation/submission require a separate review of guest contact details,
explicit approval and idempotency. This proposal does not activate those writes.
