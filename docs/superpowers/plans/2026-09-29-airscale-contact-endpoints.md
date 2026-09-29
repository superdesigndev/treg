# Airscale Contact Endpoints Amendment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Amend treg PR #730 to catalog Professional Email, Personal Email, Phone, and Reverse Phone without publishing a real person's contact data.

**Architecture:** Restore the four endpoints as discovery/BYOK-only rows with harmless public deliberate-miss requests. Keep them platform-blocked and omit success `expect` predicates, then substantiate their successful prices through one separately capped private verification run whose identity-bearing inputs and outputs are never committed.

**Tech Stack:** Python 3.12, pytest, uv, YAML catalog files, treg catalog verifier, Airscale operational billing logs, Git, GitHub CLI.

---

## File map

- Modify `src/treg/catalog/airscale.yaml`: restore four contact endpoints and the Reverse Phone capability proposal; mark all four platform-blocked and deliberate-miss tested.
- Modify `tests/test_catalog_api.py`: require the twelve-endpoint surface and discovery-only billing boundary.
- Modify `docs/superpowers/plans/2026-09-29-airscale-privacy-first-listing.md`: superseded only by this amendment; do not rewrite its historical eight-endpoint execution record.
- Update outside Git `/Users/victordetraz/orca/workspaces/treg-airscale-listing-evidence/2026-09-29-source-matrix.md`: add scrubbed public-miss and private-hit ledger rows.
- Update outside Git `/tmp/airscale-treg-pr-body.md`: change the PR summary, ledger, and surface map from deferred to catalogued.

### Task 1: Pin the twelve-endpoint discovery boundary

**Files:**
- Modify: `tests/test_catalog_api.py`
- Test: `tests/test_catalog_api.py`

- [ ] **Step 1: Extend the blocked endpoint set and remove the deferred assertion**

Add these exact rows to `blocked`:

```python
        "airscale.email": ("POST", "/email"),
        "airscale.personal-email": ("POST", "/personal-email"),
        "airscale.phone": ("POST", "/phone"),
        "airscale.reverse-phone": ("POST", "/reverse-phone"),
```

Delete the `deferred` set and `assert deferred.isdisjoint(cat.by_id)`. Change the endpoint count assertion to:

```python
    assert len(airscale) == len(expected) == 12
```

Add the discovery-only boundary:

```python
    private_hit_endpoints = {
        "airscale.email",
        "airscale.personal-email",
        "airscale.phone",
        "airscale.reverse-phone",
    }
    for endpoint_id in private_hit_endpoints:
        endpoint = cat.by_id[endpoint_id]
        assert endpoint.get("platform_blocked")
        assert "expect" not in endpoint
        assert "deliberate miss" in endpoint["cost"]["note"].lower()
```

- [ ] **Step 2: Run the focused test and verify it fails**

```bash
uv run --frozen pytest -q tests/test_catalog_api.py::test_airscale_platform_eligibility_is_partitioned_by_route
```

Expected: FAIL because the catalog still contains eight Airscale endpoints.

- [ ] **Step 3: Commit the red test**

```bash
git add tests/test_catalog_api.py
git commit -m "test(catalog): require Airscale contact discovery routes"
```

### Task 2: Restore the four contact endpoints safely

**Files:**
- Modify: `src/treg/catalog/airscale.yaml`
- Test: `tests/test_catalog_api.py`

- [ ] **Step 1: Restore the Reverse Phone proposed capability**

Insert before `endpoints:`:

```yaml
proposed_capabilities:
  people.enrich.from_phone: "Enrich a person from a phone number"
```

- [ ] **Step 2: Restore Professional Email as discovery/BYOK-only**

Restore `airscale.email` with its documented input schema and add:

```yaml
    platform_blocked: "The public test is an intentional free miss; generic shared-key settlement cannot safely distinguish it from a billable success without publishing a stable personal fixture."
    test_request: {body: {linkedin_profile_url: "https://www.linkedin.com/in/example-person-000000"}}
    cost: {type: per_success, value: 2, currency: credit, per: 1, unit: call, source: docs, source_url: https://docs.airscale.io/api-reference/email-finder, checked: '2026-09-29', confidence: documented, note: "Two credits when status is success; HTTP 200 status:not_found with email:null is free. The public test is a deliberate miss; the successful price is verified privately without retaining identity-bearing evidence."}
```

Do not add an `expect` predicate.

- [ ] **Step 3: Restore Personal Email as discovery/BYOK-only**

Restore `airscale.personal-email` with its prior input schema and keep `value: null` because the successful debit is provider-dependent. Its cost note must say the public test is a deliberate miss and the exact successful debit is recorded privately. Do not add an `expect` predicate.

- [ ] **Step 4: Restore Phone as discovery/BYOK-only**

Restore `airscale.phone` with the synthetic profile miss, `value: 40`, an explicit `platform_blocked` reason, and no `expect` predicate. State in the cost note that the successful price is verified privately.

- [ ] **Step 5: Restore Reverse Phone as discovery/BYOK-only**

Restore `airscale.reverse-phone` with `+12025550147`, `value: 10`, an explicit `platform_blocked` reason, and no `expect` predicate. State in the cost note that the public test is a deliberate miss and successful price is verified privately.

- [ ] **Step 6: Run the focused test and validator**

```bash
uv run --frozen pytest -q tests/test_catalog_api.py::test_airscale_platform_eligibility_is_partitioned_by_route
uv run --frozen python scripts/catalog_validate.py
```

Expected: focused test passes; validator reports zero errors and warnings.

- [ ] **Step 7: Commit the restored surface**

```bash
git add src/treg/catalog/airscale.yaml
git commit -m "feat(catalog): add Airscale contact discovery routes"
```

### Task 3: Verify the four public deliberate misses

**Files:**
- Verify: `src/treg/catalog/airscale.yaml`
- Record privately: `/Users/victordetraz/orca/workspaces/treg-airscale-listing-evidence/2026-09-29-source-matrix.md`

- [ ] **Step 1: Receive the Airscale key through the one-time FIFO and clear the clipboard immediately**

Use a permission-restricted FIFO under `/tmp`; never print or persist the credential.

- [ ] **Step 2: Run only the four restored public tests without `--write`**

```bash
TREG_CATALOG_CRED="$AIRSCALE_TREG_KEY" uv run python scripts/catalog_verify.py airscale \
  --id airscale.email \
  --id airscale.personal-email \
  --id airscale.phone \
  --id airscale.reverse-phone
```

Expected: four HTTP 200 passes representing free misses. Confirm zero debit from operational billing rows. Do not add `verified:` or example responses.

### Task 4: Observe successful prices privately

**Files:**
- Record privately: `/Users/victordetraz/orca/workspaces/treg-airscale-listing-evidence/2026-09-29-source-matrix.md`

- [ ] **Step 1: Obtain fresh explicit authorization for a maximum of 64 credits**

Maximum documented exposure: Email 2 + Personal Email 12 + Phone 40 + Reverse Phone 10 = 64 credits. Do not reuse the earlier 2.2-credit approval.

- [ ] **Step 2: Obtain a consenting Airscale-controlled fixture privately**

Required private fields are a known-success LinkedIn person URL, professional email, personal email, and mobile phone. Never put values in chat, Git, command history, PR text, or retained raw logs.

- [ ] **Step 3: Call each endpoint once, sequentially, with a balance check or billing-row read after each**

Stop if a call misses, a debit differs from the YAML/docs, or the cumulative debit reaches the approved cap. Do not retry a paid endpoint automatically.

- [ ] **Step 4: Scrub the evidence**

Retain only endpoint ID, HTTP/status class, YAML successful price, metered successful debit, and date. Discard identity-bearing inputs and responses immediately.

- [ ] **Step 5: Reconcile Personal Email's variable price**

Keep YAML `value: null`; state the documented 3–12 range and the exact privately observed debit in the note and PR ledger. Do not pretend one observed provider fixes a universal price.

### Task 5: Regenerate, verify, and update PR #730

**Files:**
- Modify generated provider-count skills only if endpoint changes affect generator output.
- Update outside Git: `/tmp/airscale-treg-pr-body.md`

- [ ] **Step 1: Run affected and full verification**

```bash
uv run --frozen python scripts/catalog_validate.py
uv run --frozen pytest -q tests/test_catalog_api.py tests/test_catalog_validate.py tests/test_routing.py tests/test_key_providers.py tests/test_oauth_providers_m3.py
uv run --with pytest-xdist pytest -n auto -q
git diff upstream/main...HEAD --check
```

Expected: validator clean and all tests pass.

- [ ] **Step 2: Update the PR body**

Change the summary to twelve endpoints. Mark Email, Personal Email, Phone, and Reverse Phone as catalogued discovery/BYOK routes. Add one public-miss row and one scrubbed private successful-price row for each without any fixture value, raw response, absolute balance, or workspace identifier.

- [ ] **Step 3: Push and update the open PR**

```bash
git push origin feat/airscale-listing-pr
gh pr edit 730 --repo superdesigndev/treg --body-file /tmp/airscale-treg-pr-body.md
gh pr view 730 --repo superdesigndev/treg --json url,state,mergeable,statusCheckRollup
```

Expected: PR #730 remains open and mergeable with the twelve-endpoint description.
