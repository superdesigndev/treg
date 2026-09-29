# Airscale Privacy-Safe Full treg Listing Design

**Date:** 2026-09-29
**Status:** Approved

## Goal

Submit an Airscale listing with all twelve selected core endpoints without putting a real person's email address, phone number, or LinkedIn profile into the repository or pull-request evidence.

## Considered approaches

1. **Public deliberate misses plus private hit evidence (selected).** Keep all twelve endpoints. The four contact-data routes use harmless public miss fixtures and remain discovery/BYOK-only; a consenting Airscale-controlled identity is used privately once to observe each successful price. No identity-bearing input or response enters GitHub.
2. **Public successful fixtures.** Publish a consenting teammate's real profile, email, and phone in the YAML so maintainers can replay successful calls. This supports stronger shared-key settlement but permanently exposes and repeatedly processes personal test data.
3. **Unverified synthetic misses.** List all twelve with harmless misses but do not observe successful prices. This avoids the private verification calls but violates treg's deliberate-miss evidence requirement and is likely to be rejected.

## Catalog scope

The PR will contain:

1. `airscale.find-people.count`
2. `airscale.find-people`
3. `airscale.find-companies.filter-values`
4. `airscale.find-companies`
5. `airscale.profile`
6. `airscale.company`
7. `airscale.reverse-email`
8. `airscale.email`
9. `airscale.personal-email`
10. `airscale.phone`
11. `airscale.reverse-phone`
12. `airscale.airsearch`

Email, Personal Email, Phone, and Reverse Phone are catalogued as discovery/BYOK routes with deliberate free-miss `test_request` values. They remain `platform_blocked` because a success-only `expect` predicate cannot coexist safely with a public miss fixture, and removing that predicate would make generic shared-key settlement charge misses.

## Fixture and privacy policy

- Search requests use public company, role, country, or industry filters with the smallest supported result limit.
- Company enrichment uses a well-known public company URL.
- Profile extraction uses a public organization URL supported by the route, not a real person's profile.
- Reverse Email uses a reserved `.invalid` address and is explicitly labeled as a deliberate free miss. The successful two-credit price was already observed once; no identity-bearing response or input will be committed or included in the PR description.
- Email, Personal Email, Phone, and Reverse Phone use documentation-synthetic or reserved public miss targets. Each note identifies the deliberate miss and the privately observed successful debit.
- A consenting Airscale-controlled identity may be used for one bounded private verification run. Its profile URL, email addresses, phone number, returned records, workspace identifier, and absolute balance remain outside the repository and PR.
- AirSearch uses a generic public-fact question.
- No credential, absolute account balance, real personal email address, real personal phone number, or real person-profile URL may enter the diff, commit messages, or PR description.

## Verification flow

1. Run every public `test_request` and reconcile its observed miss or success cost against the YAML.
2. Privately run one successful call for Email, Personal Email, Phone, and Reverse Phone using the approved controlled identity, observing the debit for each without retaining identity-bearing inputs or responses.
3. Keep the private hit verification bounded by a fresh explicit spend cap of the documented maximum; stop before any call that could exceed the remaining cap.
4. Record only endpoint, status class, claimed successful price, metered successful price, and date in the public self-verification ledger.
5. Run the live bogus-key probe and record its exact status and scrubbed response body.
6. Run `uv run --frozen python scripts/catalog_validate.py` and the full parallel pytest suite.
7. Rebase onto current upstream `main`, rerun validation and tests, then update PR #730.

## Error and billing handling

- `expect` remains a billing-settlement predicate, not a convenient way to make a deliberate miss pass verification.
- Per-result searches stay platform-blocked where generic settlement cannot count returned rows.
- Reverse Email stays platform-blocked because the free scalar `"not found"` response cannot be classified safely by generic settlement.
- Mixed-price Profile and Company routes remain platform-blocked unless the shared-key resolver can enforce the input-specific price.
- Email, Personal Email, Phone, and Reverse Phone stay platform-blocked in this PR. Their public tests intentionally miss, so they cannot carry success predicates that generic shared-key billing would interpret safely.
- A meter mismatch stops submission until the YAML or live implementation is reconciled.

## Completion criteria

- Exactly twelve privacy-safe core endpoints remain in `airscale.yaml`.
- No private fixture or credential appears in repository history or PR material.
- Each public `test_request` has dated live evidence matching its public ledger row.
- Each deliberate-miss contact endpoint has one privately observed successful debit matching its YAML cost note.
- Catalog validation and the full test suite pass on the rebased branch.
- The PR description includes the contact email, full-surface map, bad-key wire behavior, pricing/billing notes, OpenAPI URL, and self-verification ledger.
