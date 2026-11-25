# Maintainers: reviewing and finishing a listing

## Contents

- 1. Gate on the evidence
- 2. Verify it yourself
- 3. Finish what a vendor cannot (open questions, routing adapters, platform-key slot, modal
  pricing, extended tier, docs)
- Done means
- Choosing providers for a category

The vendor's half is `SKILL.md`. When treg lists a provider itself, a maintainer does that half
first, exactly as a vendor would, and then this one. Deeper references:
`docs/context/architecture/catalog.md` (file shape, provenance, the verify pipeline, PII),
`docs/context/architecture/catalog-schema.md` (the v2 row model, `defaults:`, outcome keys, summary
and note) and `docs/context/architecture/money.md` ("Rows in the v2 spelling").

The vendor-facing surfaces all lead to `SKILL.md`: the hosted page `src/treg/web/vendor-listing.md`
(served at `/vendor-listing`), the dashboard's "List as vendor" prompt (`vendorPromptText` in
`frontend/src/state/agentsComputed.js`), the Getting Started copy line and the enrich arena's
prompt. `docs/VENDORS.md` is the page for the vendor's people: what listing gets them, the
requirements with their reasons, and what happens after the pull request. It points at the hosted
page; the skill never points at `docs/`. Keep them telling one story.

## 1. Gate on the evidence

Every vendor claim is untrusted input. One vendor pull request was malicious (#92); an honest one shipped a
docs-transcribed price 5x under the real charge (#141, claimed 1 credit, metered 5).

- The description must carry what `SKILL.md` step 8 asks: the contact email, the ledger, the endpoint
  map, the invalid-key answer, the misses, the pricing basis. Missing pieces: ask before spending
  review time. A ledger that contradicts its own file goes back unreviewed. Internal listing pull requests meet
  the same bar (#191 shipped prose claims and three stamps on a placeholder URN).
- Diff hygiene: only the registry entry, the catalog file, the logo, an `fx.yaml` credit rate and
  regenerated plugin copies. Data-only changes, no credential values, no `verified:` stamps, no
  committed examples.
- Merge it onto current `main` locally before verifying: catalog pull requests conflict in the shared
  `REGISTRY` tuple within days.

## 2. Verify it yourself

With a key you control, read from the environment only. If a vendor sent a key in chat or in the pull request,
tell them to rotate it.

1. **Invalid key, through treg.** Start the server and `POST /connections/token` with a made-up key
   against the real API, from a throwaway org (`e2e-...@treg.local`, deleted afterwards):
   - `422 "rejected ..."`: correct.
   - `200`: the probe does not check the key. Fix the key-check fields or drop the provider.
   - `404` or `502` in the reason: wrong probe path or host.

   Never ship a key provider you have not watched reject an invalid key.
2. **Every test request**: `TREG_CATALOG_CRED='<key>' uv run --frozen python scripts/catalog_verify.py
   <service> --write`, then reconcile every endpoint's price against the meter. Where a price disagrees,
   fix it from the observed charge (`source: observed`, `confidence: verified`) and tell the vendor
   their docs are stale. Post your own ledger as the review body, one inline comment per issue.
3. **Stamp `verified:`** only on endpoints that passed with a real target that returned real data and
   billed the stated amount:
   - A placeholder path parameter (`urn:li:activity:0000...`, `id: 0`) gets an empty 2xx for 0
     credits. That proves the endpoint exists. Use a real id from a sibling endpoint's answer.
   - A preview or free mode as the test request verifies the free path, not the price. Observe the
     paid path once, or mark the ledger row "hit price unobserved".
   - A miss on a pay-on-success endpoint proves a miss is free, not the hit price.
4. **Misses under the routed parent.** For each find or enrich endpoint, call `treg.<capability>`
   with a target that cannot exist, not only the direct `/call/`. An undeclared 4xx miss is an error
   to the router, and one error child makes the parent 502 (#573: 64% of three days of
   `treg.people.email.find` 502s). When the status alone cannot tell a miss from an error (Prospeo
   answers 400 for both), the declaration needs a body predicate; provider knowledge lives in YAML
   and adapters, never in `route.py`. A slow miss (LimaData's 404, median 10 s) sits late in the
   ladder or under a tighter child timeout.
5. **Settle at or below the hold only on evidence.** Dropping a documented rider because one 1-row
   probe did not charge it under-bills every call. Keep the estimate until a multi-row balance delta
   shows the rider is not billed.
6. **Scrub every example** (the repository is public): no private individuals, no third-party emails
   or phones riding along, no first-party account identity.
7. **Audit the curation** against the endpoint map: the free count and pre-flight endpoints, the
   cheapest operation tiers, deliberate-miss test requests labelled with the hit price observed once.

## 3. Finish what a vendor cannot

**Open questions.** Settle each one the description lists before merging. An endpoint that saves
something in the caller's account needs the durable-object contract (`managed_resource`,
`catalog.md`) or leaves the listing; one that answers before its work is done needs the `async`
declaration.

**Routing adapters.** For each endpoint whose capability has a contract in `contracts.yaml`, add an
adapter to `adapters.yaml` (`accepts`, `in`, `out`, a `max` when the endpoint caps a count; a v2
row's own `miss` rule judges). It is verified at load against the endpoint's `test_request` and its
example, so add it after the examples; `tests/test_routing.py` pins it. Then check the miss under
`treg.<capability>` (step 2.4) and place the provider with the contract's `prefer` or `ask_last`.
The keys are in `catalog.md`, "Adapters".

**Local scanner row.** Add the provider to `src/treg/providers.py` `CATALOG` (its key variable,
`base_url`, auth shape, probe and any `required_headers`), then regenerate the Go CLI's copy with the
command `tests/cli_go/test_upload_parity.py` prints.

**Platform-key slot**, so treg can serve the endpoints on its own key. Every listing aims for it; a
listing without one states why (no self-serve pricing, `own_account` data, sales-gated).

- `src/treg/config.py`: `platform_key_<service>: str = ""` with a one-line comment (auth shape, what
  a top-up buys). A key and secret pair uses `platform_extra_setting`; see Tomba.
- A test asserting `cat.platform_eligible(ep)` for every endpoint in the file (as
  `tests/test_crawl4ai.py` does): every endpoint's provenance is `documented` or `verified` and its price
  computes in USD.
- `fx.yaml` holds the vendor's entry-level public rate, as `SKILL.md` step 5 defines it, with
  `basis` naming that plan and its arithmetic. A flat subscription with no per-unit price gets a
  `kind: treg_shared_plan` rate instead (`catalog.md`).
- A constant header on treg's key: `_platform_bindings` must carry it; assert the platform binding
  list equals the own-key one (`test_forager_platform_bindings_match_the_byok_pair`). A
  binding whose `format` has no `{secret}` must not reach `_secret_renderings`, or the literal value
  joins the redaction set and is masked out of error evidence (#191).
- Hand the env value to Jason out of band, with the `TREG_PLATFORM_PROVIDERS` entry. Enabling it in
  production is an ops decision; the pull request makes it a one-line change.

**Modal pricing** that one scalar cannot express (riders, bulk per record): keep the numbers in the
endpoint (`price.table`, `reserve`, `settle`) and read them generically, never as credit arithmetic in
`api.py` per provider.

**Extended tier**, when the vendor publishes a stable OpenAPI spec with example values: add an
`ingest_<service>()` to `scripts/catalog_ingest.py`, register it in `INGESTERS` and generate
`<service>.extended.yaml`. The core file's `defaults:` apply to the extended rows, so the ingester
writes v2 keys against them (`ingest_tikhub`, `ingest_justoneapi`). Never probe with a real call;
platform is what the data is about. Bulk-verify with `catalog_verify_extended.py --dry-run` first,
then with an explicit `--budget`.

**Docs**: run `bash .agents/skills/tools-registry-context/scripts/drift.sh` and update the fragments
it names in the same commit.

Land your verified version: push to their branch, or open a maintainer branch that supersedes their
pull request and close theirs with credit and the findings. If it waited long, move the new endpoints' `added:`
to the merge day; never change the date of a tool already on `main`.

## Done means

- `catalog_validate.py` exits 0, the suite is green, the invalid-key rejection was watched through
  treg and quoted.
- The description carries the ledger, the endpoint map and the contact email, and no credential.
- Every endpoint has `verified:` and an example backed by a real target and an observed charge, or
  its ledger row says exactly what was not observed.
- Every endpoint is in the v2 spelling. Every find or enrich endpoint with a routing adapter has its miss
  observed and, when it is a 4xx, declared.
- The platform-key slot ships, or the pull request says why not.
- No provider credit arithmetic in `api.py` that the YAML does not state.

## Choosing providers for a category

When treg goes looking rather than a vendor coming to it:

- Self-serve API key first; sales-gated breaks the fast path. OAuth providers are a different path
  (the `add-oauth-provider` skill).
- Distinct value: fill a gap the category lacks, skip near-duplicates.
- Reject with a recorded reason: legal or shutdown risk, deprecated or absorbed products, UI-only
  tools, enterprise-gated APIs.
- When the self-serve field is thin, say so, and fill with an adjacent clean pick rather than a bad
  fit.
- A gateway 504 that still bills (a slow backend behind a proxy cutoff): record it in the file's
  header comment and rerun `catalog_verify.py` for timed-out endpoints rather than widening its timeout.
- A cross-platform provider (one endpoint covering many networks through a body field) needs its own
  platform slug in `capabilities.yaml`; a provider file cannot propose a platform.
