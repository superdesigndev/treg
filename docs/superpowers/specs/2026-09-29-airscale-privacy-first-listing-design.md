# Airscale Privacy-First treg Listing Design

**Date:** 2026-09-29
**Status:** Approved

## Goal

Submit an initial Airscale listing with eight useful endpoints that can be verified without putting a real person's email address, phone number, or LinkedIn profile into the repository or pull-request evidence.

## Considered approaches

1. **Privacy-first eight-endpoint listing (selected).** Keep public search, public organization enrichment, a reserved-address Reverse Email miss, and AirSearch. Defer contact-data endpoints until Airscale can provide controlled fixtures privately. This minimizes disclosure and still meets treg's eight-endpoint minimum.
2. **Full twelve-endpoint listing.** Keep Email, Personal Email, Phone, and Reverse Phone, using a consenting teammate's real fixtures. This provides broader coverage but requires processing and documenting personal test inputs.
3. **Synthetic misses for all twelve endpoints.** This avoids personal fixtures but does not satisfy treg's requirement to observe the successful price of deliberate-miss, pay-on-success operations and would likely be rejected.

## Catalog scope

The initial PR will contain:

1. `airscale.find-people.count`
2. `airscale.find-people`
3. `airscale.find-companies.filter-values`
4. `airscale.find-companies`
5. `airscale.profile`
6. `airscale.company`
7. `airscale.reverse-email`
8. `airscale.airsearch`

The initial PR will remove and defer:

- `airscale.email`
- `airscale.personal-email`
- `airscale.phone`
- `airscale.reverse-phone`

Those four routes remain documented in the PR's full-surface map as excluded because compliant live verification needs controlled, consenting personal fixtures and observed successful charges.

## Fixture and privacy policy

- Search requests use public company, role, country, or industry filters with the smallest supported result limit.
- Company enrichment uses a well-known public company URL.
- Profile extraction uses a public organization URL supported by the route, not a real person's profile.
- Reverse Email uses a reserved `.invalid` address and is explicitly labeled as a deliberate free miss. The successful two-credit price was already observed once; no identity-bearing response or input will be committed or included in the PR description.
- AirSearch uses a generic public-fact question.
- No credential, absolute account balance, personal email address, personal phone number, or person-profile URL may enter the diff, commit messages, or PR description.

## Verification flow

1. Reconcile every retained endpoint's `test_request`, expected response, and YAML cost against a live call and balance delta or other meter evidence.
2. Keep paid verification bounded by a fresh explicit spend cap; do not run additional paid calls merely to broaden evidence.
3. Record only status, claimed cost, metered cost, and date in the public self-verification ledger.
4. Run the live bogus-key probe and record its exact status and scrubbed response body.
5. Run `uv run --frozen python scripts/catalog_validate.py` and the full parallel pytest suite.
6. Rebase onto current upstream `main`, rerun validation and tests, then open the PR with `Contact: support@airscale.io`.

## Error and billing handling

- `expect` remains a billing-settlement predicate, not a convenient way to make a deliberate miss pass verification.
- Per-result searches stay platform-blocked where generic settlement cannot count returned rows.
- Reverse Email stays platform-blocked because the free scalar `"not found"` response cannot be classified safely by generic settlement.
- Mixed-price Profile and Company routes remain platform-blocked unless the shared-key resolver can enforce the input-specific price.
- A meter mismatch stops submission until the YAML or live implementation is reconciled.

## Completion criteria

- Exactly eight privacy-safe core endpoints remain in `airscale.yaml`.
- No private fixture or credential appears in repository history or PR material.
- Each retained `test_request` has dated live evidence matching its public ledger row.
- Catalog validation and the full test suite pass on the rebased branch.
- The PR description includes the contact email, full-surface map, bad-key wire behavior, pricing/billing notes, OpenAPI URL, and self-verification ledger.
