"""Platform vendor-account capacity — the "Know" layer of docs/PROVIDER-CAPACITY-PLAN.md.

What treg's OWN accounts (tier 4) have left, so a caller never inherits our 402. Three pieces:

* `collectors` — the providers' free balance/quota calls (moved from scripts/provider_balances.py).
* `policy`    — `CapacityPolicy` defaults per provider, the import that flags unknowns, and the pure
                latest-state rule (remaining → exhausted / health).
* `sweep`     — the worker command: collect → `CapacitySnapshot` rows → publish latest state to
                ratestore. Observe-only: nothing here alerts or touches the call path.
* `view`      — the in-process latest-state view (ratestore, 60 s TTL) the call path will read
                from step D on. Read-only.

Boundaries: this package is a domain leaf — it may read config and the catalog, and write ONLY
the tables it owns (`capacitypolicy`, `capacitysnapshot`) plus ratestore `capacity:*` keys, and
only from worker-profile commands. It never imports `treg.application`, `treg.routers` or a web
framework (import-linter contract).
"""
