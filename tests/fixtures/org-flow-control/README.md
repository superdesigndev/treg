# Publisher contract fixtures

These synthetic snapshots are the exact serialized payload captured from the reference
publisher at revision `eac57c4c8acfacde14e60abef763b23985aee14f`. `generate.ts` invokes its real
validation and serialization through `publish`, replacing only the connection with an in-memory
recorder. It never connects to Redis or publishes a real rule. Publisher source is not vendored.

The fixtures cover all controls disabled, a fractional-second UTC pause with populated rate and
concurrency fields, and an explicit resume preserving those other controls. Each file has one
trailing newline added for source control.

To reproduce with Bun and the reference publisher's dependencies installed:

```sh
bun tests/fixtures/org-flow-control/generate.ts /path/to/traffic-control-redis.ts
```

The Python contract test reads these captured bytes directly. It does not recreate the publisher
format using the consumer's test helper.
