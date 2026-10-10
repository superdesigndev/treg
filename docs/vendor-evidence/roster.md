# Full public REST surface

Fetched https://api.ugcroster.com/v1/openapi.json on 2026-10-04. 74 operations across 45 paths. Exactly 3 operations are appropriate for a directory-data listing; adding 8–15 would invent scope. Seek a curation exception for this small API.

| Operation | Published summary | Decision |
|---|---|---|
| GET `/creators/search` | Search the creator directory | Catalogued: read-only data key. |
| GET `/creators/{pk}` | Get one creator profile | Catalogued: read-only data key. |
| GET `/brand` | Get your brand profile | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/brand` | Update your brand profile | Excluded: brand/account workflow; not directory-data scope. |
| GET `/campaigns` | List campaigns | Excluded: brand/account workflow; not directory-data scope. |
| POST `/campaigns` | Create a campaign | Excluded: brand/account workflow; not directory-data scope. |
| GET `/campaigns/{id}` | Get a campaign with applicants and hires | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/campaigns/{id}` | Update a campaign | Excluded: brand/account workflow; not directory-data scope. |
| DELETE `/campaigns/{id}` | Archive a campaign | Excluded: brand/account workflow; not directory-data scope. |
| GET `/campaigns/{id}/applications` | List a campaign's applications | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/applications/{id}` | Approve, reject, or hire an applicant | Excluded: brand/account workflow; not directory-data scope. |
| GET `/roster` | List your roster | Excluded: brand/account workflow; not directory-data scope. |
| POST `/roster` | Add a creator by email | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/roster` | Update a roster entry | Excluded: brand/account workflow; not directory-data scope. |
| POST `/roster/add` | Add a creator by social handle | Excluded: brand/account workflow; not directory-data scope. |
| GET `/briefs` | List briefs | Excluded: brand/account workflow; not directory-data scope. |
| POST `/briefs` | Create a brief (optionally create-and-send) | Excluded: brand/account workflow; not directory-data scope. |
| GET `/briefs/{id}` | Get a brief | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/briefs/{id}` | Update a brief | Excluded: brand/account workflow; not directory-data scope. |
| POST `/briefs/{id}` | Send a brief | Excluded: brand/account workflow; not directory-data scope. |
| GET `/briefs/{id}/submission-links` | List submission links for a brief | Excluded: brand/account workflow; not directory-data scope. |
| POST `/briefs/{id}/submission-links` | Mint public draft-submission links | Excluded: brand/account workflow; not directory-data scope. |
| GET `/contracts` | List contracts | Excluded: brand/account workflow; not directory-data scope. |
| POST `/contracts` | Create a contract (draft) | Excluded: brand/account workflow; not directory-data scope. |
| GET `/contracts/{id}` | Get a contract | Excluded: brand/account workflow; not directory-data scope. |
| POST `/contracts/{id}` | Send a contract for signature | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/contracts/{id}` | Update a draft contract | Excluded: brand/account workflow; not directory-data scope. |
| DELETE `/contracts/{id}` | Delete a contract | Excluded: brand/account workflow; not directory-data scope. |
| GET `/deliverables` | List deliverables | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/deliverables/{id}` | Review a submission | Excluded: brand/account workflow; not directory-data scope. |
| POST `/deliverables/request` | Request videos from creators | Excluded: brand/account workflow; not directory-data scope. |
| GET `/messages` | List conversations | Excluded: brand/account workflow; not directory-data scope. |
| POST `/messages` | Send a message | Excluded: brand/account workflow; not directory-data scope. |
| GET `/messages/{id}` | Read a thread | Excluded: brand/account workflow; not directory-data scope. |
| GET `/content` | List tracked posts | Excluded: brand/account workflow; not directory-data scope. |
| POST `/content` | Track a post | Excluded: brand/account workflow; not directory-data scope. |
| GET `/content/{id}` | Get a tracked post | Excluded: brand/account workflow; not directory-data scope. |
| POST `/content/{id}` | Refresh a post (synchronous) | Excluded: brand/account workflow; not directory-data scope. |
| DELETE `/content/{id}` | Stop tracking a post | Excluded: brand/account workflow; not directory-data scope. |
| POST `/content/import` | Bulk import posts by handle | Excluded: brand/account workflow; not directory-data scope. |
| GET `/content/report` | Aggregate content report | Excluded: brand/account workflow; not directory-data scope. |
| GET `/affiliates` | Affiliate overview | Excluded: brand/account workflow; not directory-data scope. |
| GET `/affiliates/links` | List affiliate links and codes | Excluded: brand/account workflow; not directory-data scope. |
| POST `/affiliates/links` | Create an affiliate link or discount code | Excluded: brand/account workflow; not directory-data scope. |
| POST `/affiliates/links/bulk` | Auto-generate affiliate codes for roster members | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/affiliates/links/{id}` | Update an affiliate link | Excluded: brand/account workflow; not directory-data scope. |
| DELETE `/affiliates/links/{id}` | Deactivate an affiliate link (soft delete) | Excluded: brand/account workflow; not directory-data scope. |
| GET `/affiliates/performance` | Affiliate performance (filterable) | Excluded: brand/account workflow; not directory-data scope. |
| GET `/commissions` | List commission events | Excluded: brand/account workflow; not directory-data scope. |
| POST `/commissions` | Review commission events | Excluded: brand/account workflow; not directory-data scope. |
| GET `/affiliates/commissions` | List commissions (affiliate-commissions ledger) | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/affiliates/commissions/{id}` | Update one commission event | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/affiliates/commissions/bulk` | Bulk update commission events | Excluded: brand/account workflow; not directory-data scope. |
| GET `/payouts` | List payouts | Excluded: brand/account workflow; not directory-data scope. |
| POST `/payouts` | Record a payout | Excluded: brand/account workflow; not directory-data scope. |
| GET `/payouts/{id}` | Get a payout | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/payouts/{id}` | Update a payout | Excluded: brand/account workflow; not directory-data scope. |
| GET `/shipments` | List shipments | Excluded: brand/account workflow; not directory-data scope. |
| POST `/shipments` | Create a shipment | Excluded: brand/account workflow; not directory-data scope. |
| GET `/shipments/{id}` | Get a shipment | Excluded: brand/account workflow; not directory-data scope. |
| PATCH `/shipments/{id}` | Update a shipment | Excluded: brand/account workflow; not directory-data scope. |
| GET `/analytics` | Account analytics overview | Excluded: brand/account workflow; not directory-data scope. |
| GET `/webhooks` | List webhook subscriptions | Excluded: brand/account workflow; not directory-data scope. |
| POST `/webhooks` | Subscribe to events | Excluded: brand/account workflow; not directory-data scope. |
| DELETE `/webhooks/{id}` | Delete a webhook subscription | Excluded: brand/account workflow; not directory-data scope. |
| GET `/usage` | Current-period usage for the calling key | Catalogued: read-only data key. |
| GET `/affiliates/payouts` | List payouts | Excluded: brand/account workflow; not directory-data scope. |
| POST `/affiliates/payouts` | Record a payout | Excluded: brand/account workflow; not directory-data scope. |
| GET `/affiliates/jobs/{id}` | Inspect durable affiliate provisioning | Excluded: brand/account workflow; not directory-data scope. |
| POST `/affiliates/jobs/{id}` | Retry incomplete affiliate provisioning | Excluded: brand/account workflow; not directory-data scope. |
| GET `/affiliates/automation` | List opt-in roster enrollment rules | Excluded: brand/account workflow; not directory-data scope. |
| POST `/affiliates/automation` | Enable or disable automatic roster offer creation | Excluded: brand/account workflow; not directory-data scope. |
| GET `/affiliates/orders` | List verified store orders and unmatched attribution | Excluded: brand/account workflow; not directory-data scope. |
| GET `/affiliates/integrations` | Discover store IDs and readiness for affiliate offers | Excluded: brand/account workflow; not directory-data scope. |

Transport/discovery surfaces: `/v1/mcp` exposes wrappers, not additional data operations; `/v1/openapi.json` is public documentation. `/capabilities.json` is live (HTTP 200, JSON verified October 4, 2026) and documents public credit costs; it is not evidence of negotiated platform procurement. There is no documented free query-count/preview endpoint. `/usage` is the cheapest free preflight. Do not ingest brand routes into a data-key extended tier.

## Submission status

Contact: support@ugcroster.com (public support contact on Roster's Terms/Privacy pages).

Three operations are intentionally curated: the complete data-key REST surface. Brand operations require different credentials and must not be ingested into a data-key extended tier. This requests a small-surface exception to the usual 8–15 endpoints and reuses influencersclub's existing `creators.profile` capability proposal.

Public prepaid access is available without a monthly subscription: $25/5,000 credits, $125/25,000 or $500/100,000; manual purchases by default, 60 requests/minute. Optional auto-reload requires explicit console authorization and a monthly spending limit; it is off by default. Existing subscribers retain their current terms. This retail offer is not a Treg purchase receipt.

Platform procurement is pending. An empty `platform_key_roster` setting enables later wiring without committing a credential. No `fx.yaml` receipt, funded platform account, USD replacement cost or platform-eligibility claim has been fabricated. Deployment configuration moved to the maintainer's private repo; activation and private credential handoff remain maintainer operations.

## Validation on October 4, 2026

- Catalog validator: 3 endpoints, 0 errors, 1 shared-capability promotion warning.
- Focused Roster and key-provider tests: 29 passed.
- Live Treg `/connections/token` against Roster with deliberate garbage credential: 422, `{"detail":"Roster Creator Data rejected that token (Invalid or revoked API key)"}`. This was a local temporary test org, not a production Treg account.
- `build_plugin.py --check`: five stale mirrored skills in the unchanged upstream checkout; no generated skills changed by this patch.
- Full test suite attempted, stopped after 210 passed and two unrelated HTML/SPA failures (`test_adsconv.py`, missing capture tags for `/catalog` and `/search`, `/app` returns 503 before SPA assets are built). No full-suite success is claimed.
- No maintainer `verified:` stamps or creator response examples included.

Independent maintainer verification with a dedicated credential remains required.

## Vendor self-verification ledger

Measured 2026-10-04T21:07:43.306Z on an isolated, temporary internal Roster data-key account; not a paying customer account. Key revoked after run. Credits were metered through production; no Stripe charge or procurement receipt is claimed. Raw creator records, profile pk, contacts and credentials were not retained in this evidence.

| Operation / target | HTTP | Claimed credits | Observed credits | Meter evidence (`/usage` credits.used) |
|---|---:|---:|---:|---|
| GET /usage | 200 | 0 | Free pre/post check | Status required to be 200 by verifier; paid-call deltas below reconcile |
| Search q=fitness, page=1, limit=100 | 200 | 10 | 10 | 0 → 10 |
| Search q=beauty, page=1, limit=100 | 200 | 10 | 10 | 10 → 20 |
| Search q=skincare, page=1, limit=100 | 200 | 10 | 10 | 20 → 30 |
| Search q=gaming, page=1, limit=100 | 200 | 10 | 10 | 30 → 40 |
| Search q=fashion, page=1, limit=100 | 200 | 10 | 10 | 40 → 50 |
| Real-hit profile, pk from preceding search | 200 | 1 | 1 | 50 → 51 |

Total: 5 × 10 + 1 = **51 metered credits**. Search yielded 500 rows across these fixed queries (421 unique IDs); these are not random samples or directory-wide quality estimates. No sort parameter was sent. The catalog's smaller `limit=1` cheap fixture has not been run separately. No deliberate-miss profile charge observed. Independent maintainer verification and stable safe profile fixture remain pending.
