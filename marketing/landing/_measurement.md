# Landing-page measurement contract

`lp_copy_prompt` records a leading interaction; a successful tool call records product use.
The conversion implementation and first-call semantics are documented in
[ads-conversions](../../docs/context/architecture/ads-conversions.md).

Hosted campaign hypotheses, bidding decisions and historical verification records live in the
[private marketing records](https://github.com/superdesigndev/treg-internal/tree/main/docs/marketing).
This public file describes the metric referenced by the shipped page instrumentation only.
