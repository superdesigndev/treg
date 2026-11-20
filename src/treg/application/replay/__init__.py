"""Replay recorded exchanges through the billing, outcome and routing code of one checkout.

A replay is a pure function of one corpus record and one checkout: it never opens a network
connection, a database session or a ledger transaction, and never calls a provider. The differ
compares two replays of the same corpus and prints counts, field names and amounts only.
See docs/context/architecture/replay.md.
"""
