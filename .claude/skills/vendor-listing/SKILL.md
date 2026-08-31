---
name: vendor-listing
description: >
  Onboard a vendor who wants their API listed in the treg catalog. Use whenever someone asks
  "how do we get listed on treg", a vendor sends their API details, or a listing PR/issue needs
  review. Walks the whole pipeline: eligibility gate → registry entry → platform-key slot → logo
  → tests → LIVE bogus-key test → core catalog YAML → verify → scrub → validate → evidence
  ledger in the PR. Every listing ships tier-4 wiring and a per-endpoint verification table.
  The vendor-facing doc this skill implements is docs/VENDORS.md.
---

# Vendor listing — add a provider to the catalog

A listing has **three parts**, and all three ship in the same PR:

1. **Registry entry** (`src/treg/oauth_providers.py`) — how a team connects a credential for the
   provider, and how treg verifies that credential is real.
2. **Core catalog file** (`src/treg/catalog/<service>.yaml`) — what an agent can *do*: 8–15
   curated endpoints with capability mapping, inputs, cost + provenance, and verified examples.
3. **Platform-key slot** (`config.py` + `render.yaml` + `fx.yaml`) — so treg can serve the
   endpoints on its own key (tier 4). **Always aim for this.** A listing that is BYOK-only is the
   exception and needs a stated reason (no self-serve pricing, `own_account` data, sales-gated).

And every listing PR carries a **verification evidence ledger** (Step 7b). No ledger, no merge.

Deep references (read before non-trivial work; do not duplicate them here):
- `docs/context/guides/expanding-a-category.md` — the add-a-provider playbook, verify toolbox, traps
- `docs/context/architecture/catalog.md` — catalog schema, cost provenance, verify pipeline, PII rules
- `docs/VENDORS.md` — what we told the vendor to prepare (their checklist)
- `src/treg/web/vendor-listing.md` — the HOSTED instructions (served at `/vendor-listing`) that a
  vendor's own coding agent follows to raise a listing PR; the dashboard's "List as vendor" modal
  (connections view, `vendorAsk` in `index.html`) hands vendors a prompt pointing at it. Keep the
  three vendor-facing surfaces (doc, hosted page, modal prompt) telling one story.

## Step 0 — intake: collect the vendor facts

Before touching code, you need ALL of these. If the vendor's submission is missing any, ask —
do not guess ("unconfirmed" beats a wrong path shipped):

- **A contact email for the vendor's team** — required in the PR/issue description. It is how a
  test credential gets arranged for live verification; without it the listing stalls at step 7.
- `service` id (lowercase slug), display name, one-line summary (what an agent can DO)
- `base_url` (exact API root)
- Auth: where the key rides (header name + format, or query param name). Key **in the URL path is
  not supported** — decline or defer.
- A **free or near-free probe endpoint** where a valid key returns 2xx and an invalid key does NOT
  — plus the exact bad-key behavior (status code, or the JSON field that signals invalid)
- Pricing page URL, per-endpoint prices, and the billing model (`per_call` / `per_success` /
  `per_result` / credits / quota). Machine-readable rate-card endpoint if they have one.
- Docs URL; OpenAPI spec URL if published
- The 8–15 endpoints they consider their core surface, with example parameter *values*
- A test credential (or credits grant) for verification — read it from env only, never write it
  into any file

## Step 1 — eligibility gate

Reject decisively, with a recorded reason, when:
- The key **cannot be validated** (API returns success for garbage keys) — e.g. ScrapeCreators
- Key rides in the **URL path** (`/v3/{key}/…`) — injectors do header/query only
- **Sales-gated** signup (no self-serve key breaks the fast path)
- Legal/shutdown risk, or deprecated/absorbed products

## Step 2 — registry entry

Add an `OAuthProvider(auth_kind="key", …)` in `oauth_providers.py` and append it to `REGISTRY`.
Model it on `HUNTER` (a clean key provider). Pick the verify fields from the toolbox table in
`expanding-a-category.md` (`token_header`/`token_format`, `token_location="query"`+`token_param`,
`probe_url`, `probe_method`+`probe_json`, `token_verify_field`, `token_ok_field`+`token_ok_value`,
`token_reject_field`, `probe_reject_statuses`, …). Prefer a header over a query key so the secret
never lands in a logged URL. Set `category` (add to `CATEGORY_ORDER` only if genuinely new),
`summary`, `base_url`, `docs_url`, `probe_path`, and `setup_url`/`setup_steps` so a user can find
their key.

**Provider-required constant headers** (Crustdata's `x-api-version: 2025-11-01`): declare them in
`required_headers=(("name", "value"),)` on the `OAuthProvider` and in the `providers.py` CATALOG
row — never in the proxy. They become constant-format bindings. Trap (PR #191): a binding whose
`format` has no `{secret}` must NOT be fed to `_secret_renderings` — otherwise the literal
value (a date!) joins the redaction set and gets masked out of error evidence. Check the
constant does not appear in `_secret_renderings`' output, and add a test.

## Step 2b — platform-key slot (do this for every listing)

The tier-4 wiring is part of the listing, not a follow-up:

- `src/treg/config.py`: `platform_key_<service>: str = ""` with a one-line comment (auth shape,
  what a top-up buys). Pairs (key+secret) use `platform_extra_setting`; see Tomba.
- `render.yaml`: `- key: TREG_PLATFORM_KEY_<SERVICE>` + `sync: false` + a comment. **No value.**
- `src/treg/catalog/fx.yaml` `credit_rates_usd`: the USD-per-credit treg actually pays, with
  `basis` naming the real top-up/receipt (not the pricing page's headline tier), `source`, `checked`.
- A test asserting `cat.platform_eligible(ep)` for every endpoint in the file (see
  `test_crustdata_and_aviato_catalogs_are_platform_priced`); every cost must therefore be
  `confidence: documented|verified` with a computable USD figure.
- If the platform key also needs a constant header, `_platform_bindings` must carry it — assert
  the tier-4 binding list equals BYOK's (see `test_crustdata_platform_key_keeps_the_required_version_header`).
- Hand the **env value** to Jason out-of-band (a file, never the PR, never chat if avoidable) with
  the `TREG_PLATFORM_PROVIDERS` allow-list entry. Setting it in Render is his ops decision; the PR
  just makes it possible.

Modal pricing (preview/rescrape/email riders, bulk-per-record) that one scalar can't express: keep
the numbers in the YAML cost block (`note` today; a `modifiers` block if you add one) and read
them generically — do not hardcode credit arithmetic in `api.py` per provider (#191 debt).

## Step 3 — logo

`src/treg/web/logos/<service>.svg` — a **neutral lettermark**, not the real brand mark.
`test_every_provider_has_a_logo` fails without it.

## Step 4 — tests

- Add the id to `test_every_provider_is_registered` (test_oauth_providers_m3)
- Add it to the offerable loop in `test_key_providers`

## Step 5 — LIVE bogus-key test (load-bearing; never skip)

Start the server, `POST /connections/token` with a **garbage key** against the real API:
- `422 "rejected …"` → correct. Ship it.
- `200` → the probe does not validate the key → fix the verify fields or drop the provider.
- `404`/`502` in the reason → wrong probe path/host → fix `base_url`/`probe_path`.

**Never ship a key provider you haven't watched reject a bogus key.** Use a throwaway org
(`e2e-…@treg.local`) and delete it after. Watch for the known traps: trailing-slash 307 (put the
slash in `probe_path`), 200-with-error-body (read a body field), CSV/text responses.

## Step 6 — core catalog YAML

`src/treg/catalog/<service>.yaml`, following the schema in `catalog.md`. In order:

1. **Ingest** from OpenAPI if published (never hand-transcribe paths); else from docs with
   `source.openapi: null`.
2. **Select** ~8–15 endpoints; ALWAYS include ones matching capabilities other providers already
   implement (overlap enables comparison).
3. **Map** each to a capability from `capabilities.yaml`; missing jobs go under
   `proposed_capabilities:` in the provider file, not straight into the shared taxonomy.
4. **Describe** `input` (param names, types, required, location; constraints into `note`).
5. **Cost** with full provenance: `type/value/currency/per/unit` + `source/source_url/checked/
   confidence`. Unknown price → `value: null` + `confidence: unknown` + a note. Prefer a
   rate-card endpoint (`source: rate_card_api`) over a pricing page.
6. **test_request** per endpoint — CHEAP: smallest limit, one item, public well-known target.
   ⚠️ Never probe with empty params "expecting a validation error": a no-required-params endpoint
   returns its full default result set and bills for it (the Moz quota trap).

## Step 7 — verify, scrub, validate

```bash
TREG_CATALOG_CRED='<secret>' uv run --frozen python scripts/catalog_verify.py <service>.yaml
uv run --frozen python scripts/catalog_validate.py    # must exit 0
uv run --frozen python -m pytest -q
```

- Stamp `verified:` only on endpoints that PASSED **with a real target that returned real data and
  billed the documented amount**. Docs lie; documented ≠ verified. Three stamp traps from #191:
  - **Placeholder path params** (`urn:li:activity:0000…`, `id: 0`): providers return an empty
    2xx for 0 credits. That proves the route exists, nothing else. Use a real id harvested from a
    sibling endpoint's response and observe the charge once.
  - **Preview / free modes** (`preview=true`) as the test_request: the free path is verified, the
    paid price is not. Either observe the paid path once, or write "hit price unobserved" in the
    cost `note` — never let the stamp imply the price was confirmed.
  - **A miss on a per_success route** (`{"phones":[]}`, 0 credits) proves miss=free, not the
    hit price. Say so in the note.
- **Settle ≤ reserve only on evidence.** If the code settles a modal price below its reserve
  (e.g. drops a documented per-result rider because one 1-row probe didn't charge it), that is an
  unproven assumption that under-bills treg on every call (#141 inverse). Settle at the estimate
  until a multi-row balance delta shows the rider is not billed.
- **Scrub every captured example** (this repo is public): no named private individuals
  (contact-lookup routes get `untestable:` + no test_request + no example), no third-party
  emails/phones riding along, no first-party account identity.
- No credential value anywhere in the diff.

## Step 7b — the verification evidence ledger (required in every listing PR)

"Live-called all N tools" in prose is not evidence. The PR description carries **one table row per
endpoint**, produced from your own run, so a reviewer can tell at a glance which stamps rest on an
observation and which don't:

```
| endpoint | http | test target | credits observed | catalog price | matches? | evidence |
|---|---|---|---|---|---|---|
| svc.companies.search | 200 | stripe.com, limit 1 | 0.03 | per_result 0.03 | ✅ | `x-credits-used: 0.03` header |
| svc.people.phone     | 200 | real profile | 0 (miss) | per_success 8 | ⚠️ hit unobserved | balance 945.75→945.75 |
| svc.post.reactions   | 200 | urn:…7496332962049933312 | 2 | per_success 2 | ✅ | balance 937.75→935.75 |
```

Rules for the ledger:
- **`evidence` names the meter**: a charge header/field, a rate-card endpoint, or a balance delta
  (before → after). "Docs say" is not a meter. Arithmetic across a batch is fine if the batch total
  reconciles to the cent — state the reconciliation (`87.25 documented − 25 preview − 6 placeholder
  − 8 miss = 48.25 observed ✓`).
- **`matches?` is honest**: ✅ observed = catalog; ⚠️ partial (miss/preview/placeholder only —
  say what was not observed); ❌ mismatch (then the YAML must already be corrected to the
  observed value with `source: observed`).
- Also record: the **bogus-key probe** (status + quoted body), balance before/after per provider,
  `catalog_validate.py` and `build_plugin.py --check` output, the pytest count, and the date.
- Prices that were observed get `source: observed / confidence: verified`; the rest stay
  `documented`. The ledger and the YAML must agree — a ledger that contradicts its own YAML
  bounces the PR.

## Step 8 — optional extended tier

If the vendor publishes a stable OpenAPI spec with example parameter values, add an
`ingest_<service>()` to `scripts/catalog_ingest.py`, register it in `INGESTERS`, and generate
`<service>.extended.yaml`. Rules: never probe with a real call; platform = what the data is
ABOUT; normalise platform slugs across providers. Bulk-verify with
`catalog_verify_extended.py --dry-run` first, then with an explicit `--budget`.

## Reviewing a vendor-RAISED PR (they wrote the files; you verify)

The same pipeline, entered from the other end. Every vendor claim is **untrusted input** — one
vendor PR was outright malicious (#92), and an honest one shipped a docs-transcribed price 5×
under the real charge (#141, GitHub→LinkedIn: claimed 1 credit, metered 5). The order:

1. **Gate on the required PR evidence** (per `docs/VENDORS.md` items 8–9): the Step 7b ledger
   (per-endpoint status + claimed vs metered cost + meter evidence, dated) and the full-surface
   map. Missing → ask for it before spending review time. A ledger that contradicts its own YAML
   bounces the PR unreviewed. This applies to **internal** listing PRs too (#191 shipped with
   prose claims and three stamps on a placeholder URN).
2. **Diff hygiene first**: expected files only (registry entry, catalog YAML, logo, two test
   lists, fx row), data-only changes, no credential values, no `verified:` stamps or committed
   examples (those are yours to add).
3. **Merge it onto current main locally** before verifying — catalog PRs staleness-conflict in
   the shared test lists and REGISTRY tuple within days.
4. **Independently verify with a key YOU control** (steps 5–7 above): watch the bogus-key
   rejection yourself and quote the real wire body, run `catalog_verify.py` over every
   test_request, and reconcile every cost block against the meter (charge field / rate-card
   endpoint / balance delta) — the vendor's ledger is a cross-check, never the source of truth.
   Where a price disagrees, fix it from the observed charge (`source: observed`,
   `confidence: verified`) and tell the vendor their docs are stale. Post your own Step 7b table
   as the review body and one inline comment per issue, anchored to the YAML/API line.
   Read the keys from env only; if they arrived in chat, say they should be rotated.
5. **Audit the curation against their surface map**: are the free count/pre-flight routes and
   cheapest operation tiers in? Deliberate-miss test_requests labeled, with the hit price
   observed once? per_success semantics actually observed (a miss settling at 0)?
6. **Finish the maintainer half they can't**: front-door counts (llms.txt, skill.md, README) +
   `scripts/build_plugin.py`, docs drift, and the tier-4 key slot (Step 2b — not optional; if
   their prices can't support it, record why). Land your verified version (a maintainer branch
   superseding their PR is fine); close their PR with credit and the findings.

## Step 9 — done means

- Validator exits 0; suite green; bogus-key rejection observed live and quoted
- The PR description carries the Step 7b evidence ledger (one row per endpoint, meter named,
  balances before/after, reconciliation), and the vendor's contact email — no credential value
  anywhere
- Every endpoint carries `verified:` + example backed by a real target and an observed charge, or
  its cost `note` states exactly what was not observed (hit price / rider / placeholder)
- Platform-key slot shipped: `config.py` setting, `render.yaml` key (no value), `fx.yaml` rate
  from a real top-up, `platform_eligible` test for the whole file; env value handed to Jason
  out-of-band with the `TREG_PLATFORM_PROVIDERS` entry. Enabling in Render is his call, not
  automatic — but the PR must make it a one-line ops change.
- Any settle-below-reserve logic is backed by a multi-row balance delta, and no provider credit
  arithmetic is hardcoded in `api.py` that the YAML doesn't also state
- Docs synced: run `bash .claude/skills/tools-registry-context/scripts/drift.sh`, update touched
  fragments in the same commit
