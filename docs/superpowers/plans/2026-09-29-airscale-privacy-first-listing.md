# Airscale Privacy-First Listing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the Airscale core catalog to eight privacy-safe endpoints, verify their contracts and metered costs, and open the treg vendor-listing pull request without publishing credentials or personal fixtures.

**Architecture:** Keep the existing Airscale provider registry, logo, capabilities, and detailed catalog contracts. Narrow only the curated endpoint surface and its Airscale-specific tests, then separate deterministic repository verification from credentialed live verification and PR publication.

**Tech Stack:** Python 3.12, pytest, uv, YAML catalog files, treg catalog validation scripts, Git, GitHub CLI.

---

## File map

- Modify `src/treg/catalog/airscale.yaml`: retain exactly eight privacy-safe endpoints, remove the contact-fixture endpoints, remove the now-unused Reverse Phone proposed capability, and use a public organization target for Profile.
- Modify `tests/test_catalog_api.py`: define the exact retained endpoint set, its platform-eligibility partition, and privacy/deferred-route assertions.
- Modify `tests/test_routing.py`: retain only settlement coverage for catalogued Airscale per-success routes.
- Create outside Git history `/tmp/airscale-treg-pr-body.md`: hold the final scrubbed PR description for `gh pr create`.

### Task 1: Pin the eight-endpoint privacy boundary with a failing test

**Files:**
- Modify: `tests/test_catalog_api.py:532-564`
- Test: `tests/test_catalog_api.py`

- [ ] **Step 1: Replace the twelve-route expectation with the approved eight-route contract**

Use this exact test body:

```python
def test_airscale_platform_eligibility_is_partitioned_by_route():
    cat = cs.load()
    eligible = {
        "airscale.find-people.count": ("POST", "/find-people/count"),
        "airscale.find-companies.filter-values": ("GET", "/find-companies/filter-values"),
        "airscale.airsearch": ("POST", "/airsearch"),
    }
    blocked = {
        "airscale.find-people": ("POST", "/find-people"),
        "airscale.find-companies": ("POST", "/find-companies"),
        "airscale.profile": ("POST", "/profile"),
        "airscale.company": ("POST", "/company"),
        "airscale.reverse-email": ("POST", "/reverse-email"),
    }
    deferred = {
        "airscale.email",
        "airscale.personal-email",
        "airscale.phone",
        "airscale.reverse-phone",
    }
    airscale = [ep for ep in cat.endpoints if ep["provider"] == "airscale"]
    expected = eligible | blocked
    assert len(airscale) == len(expected) == 8
    assert {ep["id"] for ep in airscale} == set(expected)
    assert deferred.isdisjoint(cat.by_id)
    for endpoint_id, (method, path) in eligible.items():
        endpoint = cat.by_id[endpoint_id]
        assert (endpoint["method"], endpoint["path"]) == (method, path)
        assert cat.platform_eligible(endpoint), endpoint_id
    for endpoint_id, (method, path) in blocked.items():
        endpoint = cat.by_id[endpoint_id]
        assert (endpoint["method"], endpoint["path"]) == (method, path)
        assert not cat.platform_eligible(endpoint), endpoint_id
        assert endpoint.get("platform_blocked"), endpoint_id

    profile_target = cat.by_id["airscale.profile"]["test_request"]["body"]["linkedin_profile_url"]
    assert "/company/" in profile_target or "/school/" in profile_target
    assert cat.by_id["airscale.reverse-email"]["test_request"] == {
        "body": {"email": "nobody@airscale.invalid"},
    }
```

- [ ] **Step 2: Run the focused test and verify the expected failure**

Run:

```bash
uv run --frozen pytest -q tests/test_catalog_api.py::test_airscale_platform_eligibility_is_partitioned_by_route
```

Expected: FAIL because the catalog still contains twelve Airscale endpoints and Profile still uses a synthetic person URL.

- [ ] **Step 3: Commit the red test**

```bash
git add tests/test_catalog_api.py
git commit -m "test(catalog): pin privacy-first Airscale surface"
```

### Task 2: Remove personal-fixture endpoints and make Profile privacy-safe

**Files:**
- Modify: `src/treg/catalog/airscale.yaml`
- Test: `tests/test_catalog_api.py`

- [ ] **Step 1: Remove the unused proposed capability**

Delete this mapping because Reverse Phone is no longer catalogued:

```yaml
proposed_capabilities:
  people.enrich.from_phone: "Enrich a person from a phone number"
```

- [ ] **Step 2: Change Profile's test target and evidence note to the public 0.5-credit family**

Keep the input schema unchanged, but replace these fields in `airscale.profile`:

```yaml
    test_request: {body: {linkedin_profile_url: "https://www.linkedin.com/company/stripe/"}}
    miss: {status: 404, means: "No profile was found for the submitted LinkedIn URL; no credits are charged."}
    cost: {type: per_success, value: 0.5, currency: credit, per: 1, unit: call, source: docs, source_url: https://docs.airscale.io/api-reference/extract-people-profile, checked: '2026-09-29', confidence: documented, note: "A successful /company/ or /school/ lookup costs 0.5 credit; /in/ costs 1. Failed lookup is free. The test uses a well-known public company URL and exercises the cheapest supported family."}
```

The route remains `platform_blocked` because its accepted URL families have different prices.

- [ ] **Step 3: Delete the four complete endpoint mappings**

Remove the YAML mappings whose `id` fields are exactly:

```yaml
- id: airscale.email
- id: airscale.personal-email
- id: airscale.phone
- id: airscale.reverse-phone
```

Do not modify the retained `airscale.reverse-email` mapping or its reserved `.invalid` test address.

- [ ] **Step 4: Run the focused contract test**

Run:

```bash
uv run --frozen pytest -q tests/test_catalog_api.py::test_airscale_platform_eligibility_is_partitioned_by_route
```

Expected: `1 passed`.

- [ ] **Step 5: Commit the catalog cut**

```bash
git add src/treg/catalog/airscale.yaml
git commit -m "fix(catalog): keep Airscale fixtures privacy-safe"
```

### Task 3: Remove settlement tests for deferred routes

**Files:**
- Modify: `tests/test_routing.py:1263-1288`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Replace the multi-route parameterized test with AirSearch-only coverage**

Use this exact test:

```python
def test_airscale_airsearch_per_success_settlement_uses_documented_response_status():
    from test_marketplace_call import _mk
    from treg.application.call import settle as A

    endpoint_id = "airscale.airsearch"
    endpoint = catalog_store.load().by_id[endpoint_id]
    assert endpoint["cost"]["type"] == "per_success"
    assert endpoint["cost"]["value"] == 1
    assert catalog_store.load().adapters.get(endpoint_id) is None
    mk = _mk("airscale", endpoint_id=endpoint_id, cost_type="per_success")
    assert A._platform_billable(200, "per_success")
    assert A._observed_cost_micro(
        mk, b'{"status":"success","response":"Paris"}',
    ) is None
    for miss in (b'{"status":"not_found"}', b'{"status":"timeout"}'):
        assert A._observed_cost_micro(mk, miss) == 0
    assert not A._platform_billable(504, "per_success")
```

- [ ] **Step 2: Run the Airscale-focused tests**

Run:

```bash
uv run --frozen pytest -q \
  tests/test_catalog_api.py::test_airscale_platform_eligibility_is_partitioned_by_route \
  tests/test_catalog_validate.py::test_airscale_catalog_pagination_accepts_bounded_json_integers_only \
  tests/test_routing.py::test_airscale_airsearch_per_success_settlement_uses_documented_response_status \
  tests/test_key_providers.py::test_airscale_key_is_offerable_without_deployment_credentials \
  tests/test_oauth_providers_m3.py::test_airscale_is_registered
```

Expected: `6 passed` because the pagination test has two parameter cases.

- [ ] **Step 3: Commit the settlement-test cut**

```bash
git add tests/test_catalog_api.py tests/test_routing.py
git commit -m "test(catalog): cover retained Airscale routes"
```

### Task 4: Run all no-spend repository verification

**Files:**
- Verify: `src/treg/catalog/airscale.yaml`
- Verify: repository test suite

- [ ] **Step 1: Validate the complete catalog**

Run:

```bash
uv run --frozen python scripts/catalog_validate.py
```

Expected: exit code 0 with zero errors and zero warnings.

- [ ] **Step 2: Check the diff for secrets and personal fixtures**

Run:

```bash
git diff upstream/main...HEAD --check
git diff upstream/main...HEAD -- . ':!docs/superpowers' | rg -n \
  'Bearer [A-Za-z0-9]|api[_-]?key[=:][[:space:]]*[^<{]|@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\+[1-9][0-9]{7,}'
```

Expected: `git diff --check` exits 0. Review any regex match; only documentation examples such as the reserved `.invalid` address may remain, and no credential or real personal fixture may appear.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
uv run --with pytest-xdist pytest -n auto -q
```

Expected: all tests pass.

### Task 5: Perform explicitly capped live verification

**Files:**
- Read: `src/treg/catalog/airscale.yaml`
- Record privately: `/Users/victordetraz/orca/workspaces/treg-airscale-listing-evidence/2026-09-29-source-matrix.md`

- [ ] **Step 1: Obtain a fresh spend authorization before any credentialed run**

State the maximum possible new debit for the eight test requests and obtain the user's explicit cap. Do not reuse an earlier cap and do not run the verifier until the cap covers the computed maximum.

- [ ] **Step 2: Enter the credential without putting it in a file, command argument, clipboard, diff, or shell history**

Start a TTY shell and use hidden input:

```zsh
read -s 'AIRSCALE_TREG_KEY?Airscale API key: '
print
```

- [ ] **Step 3: Run the bogus-key probe separately**

POST `{}` to the configured `/credits` probe using a generated garbage Bearer value. Record only the date, status, and scrubbed response body. Confirm that the response is distinguishable from a valid-key response.

- [ ] **Step 4: Run every retained `test_request` and reconcile the meter**

Run:

```zsh
TREG_CATALOG_CRED="$AIRSCALE_TREG_KEY" uv run python scripts/catalog_verify.py airscale.yaml
```

For each of the eight endpoints, record HTTP status, YAML-claimed cost, actual balance delta or returned charge, and date. Stop immediately if cumulative new spend reaches the approved cap or a meter delta disagrees with YAML.

- [ ] **Step 5: Clear the in-memory credential**

```zsh
unset AIRSCALE_TREG_KEY
```

Do not add `verified:` fields or example responses; treg maintainers reserve those changes for their independent verification.

### Task 6: Rebase, reverify, and open the PR

**Files:**
- Create outside repository: `/tmp/airscale-treg-pr-body.md`
- Verify: entire branch

- [ ] **Step 1: Fetch and rebase onto current upstream main**

```bash
git fetch upstream main
git rebase upstream/main
```

Expected: clean rebase. Resolve only Airscale-listing conflicts; preserve unrelated upstream changes.

- [ ] **Step 2: Rerun post-rebase verification**

```bash
uv run --frozen python scripts/catalog_validate.py
uv run --with pytest-xdist pytest -n auto -q
git status --short
```

Expected: validation and tests pass; status is clean.

- [ ] **Step 3: Build the scrubbed PR description**

Create `/tmp/airscale-treg-pr-body.md` with these fully populated sections from the live ledger:

```markdown
## Airscale vendor listing

Contact: support@airscale.io

### Eligibility and probe

- Self-serve API keys: available without a sales call.
- Authentication: `Authorization: Bearer <key>` header.
- Probe: `POST /credits` with `{}`.
- Bad-key observation: include the exact dated HTTP status and scrubbed body from Task 5.
- Pricing: https://airscale.io/pricing — credit-based, with free misses where documented.
- Docs: https://docs.airscale.io/api-reference/api-overview
- OpenAPI: https://docs.airscale.io/openapi.json

### Self-verification ledger

Include one dated row for each of the eight endpoint IDs, with HTTP status, YAML cost, and observed meter cost. Do not include an absolute balance, request credential, or identity-bearing response.

### Full-surface map

Mark the eight retained routes as catalogued. Mark Email, Personal Email, Phone, and Reverse Phone as deferred because controlled consenting personal fixtures are required. List every other documented Airscale operation and its existing catalogued/excluded reason from the private source matrix.

### Notes

- Reverse Email uses a reserved `.invalid` deliberate miss; successful price was observed separately and no identity-bearing material is retained.
- No `verified:` stamps or example responses are included; maintainers will add those after independent verification.
```

- [ ] **Step 4: Push the branch and open the PR**

```bash
git push --set-upstream origin feat/airscale-listing-pr
gh pr create \
  --repo superdesigndev/treg \
  --base main \
  --head feat/airscale-listing-pr \
  --title "feat(catalog): add Airscale" \
  --body-file /tmp/airscale-treg-pr-body.md
```

Expected: GitHub returns the new PR URL.

- [ ] **Step 5: Inspect the public PR and report the final evidence layers**

```bash
gh pr view --repo superdesigndev/treg --json url,title,body,headRefName,baseRefName,statusCheckRollup
```

Confirm the body contains the contact, ledger, surface map, probe behavior, pricing, and OpenAPI URL, and contains no key, absolute balance, personal email, phone number, or person-profile fixture.
