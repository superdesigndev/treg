"""Capability routing — first-party routed endpoints (`treg.<capability>`), docs/CAPABILITY-ROUTING-PLAN.md.

Pure: contracts, adapters and their fixture verification, identity matching, `cost_at`, ranking,
and the generated `treg.*` catalog rows. No I/O beyond reading the two YAML files at catalog load;
the execution loop lives in `application/call/route.py`. This is the ONE place treg models an
upstream API, and every adapter is gated by its fixture round-trip (`adapters.verify`).
"""
