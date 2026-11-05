---
title: Route rules - a published provider order per segment of a routed capability
status: building
sources:
  - src/treg/domain/catalog/routing/rules.py
  - src/treg/application/call/route_rules.py
  - src/treg/application/call/route.py
  - src/treg/application/call_events.py
  - src/treg/application/call/service.py
  - src/treg/application/call/types.py
  - src/treg/infra/upstream/relay.py
  - src/treg/infra/kv.py
  - src/treg/config.py
  - src/treg/models.py
  - src/treg/alembic/versions/0096_route_rule_version.py
  - tests/test_route_rules.py
related:
  - architecture/catalog.md
  - architecture/call-events.md
  - architecture/org-flow-control.md
---

# Route rules - a published provider order per segment of a routed capability

A routed call's default provider order comes from its contract (`prefer`, `prefer_when`,
`prefer_by_domain_suffix` in [catalog.md](catalog.md)). Route rules add one more source an
operator changes without a deploy: a rule set published to the shared Redis store, read by every
worker. A rule says which providers a routed capability asks first for one segment of its calls.
It only reorders: every other candidate still follows in the contract's order, and the waterfall
falls through to them as before.

## Where it applies

`build_plan` (`application/call/route.py`) looks up a rule only when the caller sent no
`X-Treg-Route-Prefer`; the header still replaces everything. The rule's `order` goes first, then
the contract's own default order without the providers already named. Credential tier still
sorts before any order: a team's own key is asked first whatever a rule says.

The segment is what the call event records for the call, computed by the same function
(`call_events.segment`): `input_type` (the identity variant, `domain+full_name`), and the
`input_meta` keys `tld` and `domain_class`, plus any contract filter declared
`analytics: value`. A rule is evaluated on exactly the values analysis groups calls by, so a
measured segment and a routed segment cannot disagree. `tld` is the last label of the company
domain, so a French company on `.com` is not matched, as with `prefer_by_domain_suffix`.

When several rules match, the one matching the most keys wins, then the earliest listed.

## Publication contract

The private admin publishes; this reader consumes. Key `treg:route-rules:v1`, the complete
envelope:

```json
{
  "version": 3,
  "config": {
    "schema_version": 1,
    "enabled": true,
    "rules": [
      {"id": "find-fr", "capability": "people.email.find", "match": {"tld": "fr"},
       "order": ["provider-a", "provider-b"], "reason": "optional, at most 500 characters"}
    ]
  }
}
```

- `id`, `capability` and providers are lowercase names; ids are unique; `order` lists distinct
  providers; `match` maps segment keys to non-empty exact strings. No wildcards or negation.
- At most 50 rules per capability, 200 in all, 32 KB.
- `enabled: false` publishes "no rules" as a version, the kill switch.
- On read, every rule must be able to take effect: it names a routed contract; segment keys and
  values that contract's calls can record (`call_events.segment_checks`, built on the same
  `segment` the event uses: `input_type` a variant some call is recorded as, so one an earlier
  variant always shadows is refused; `domain_class` one of its four words; `tld` a lower-cased last
  label, an internationalized one in its own script; no `tld` for a capability with no domain or
  email in its identity); and providers among its routed endpoint's children whose adapter accepts
  the widest call recorded as the matched variant, after the contract's derivations (a
  name-and-domain call may also carry a LinkedIn URL, so a LinkedIn finder can serve it; a
  name-and-domain finder cannot serve a `linkedin_url` rule). Unknown fields, duplicate keys and
  wrong types are rejected.

| Refresh result | Plan behavior |
| --- | --- |
| Healthy missing key | No rules. |
| Valid set at a higher version | Replaces the snapshot. |
| Redis unavailable, timeout or a read that raises | Keep the last valid snapshot; none means no rules. |
| Invalid JSON, schema or a rule the catalog does not serve | Keep the last valid snapshot. |
| Lower version, or different content at the same version | Keep the last valid snapshot. |

Unlike [org admission](org-flow-control.md), a failure keeps the last rules instead of bypassing
them: a rule only orders candidates, so a stale one costs ordering, never correctness.

## Reading

```sh
TREG_ROUTE_RULES_ENABLED=false   # default: the YAML order, no Redis read
TREG_ROUTE_RULES_REFRESH_S=30    # 1-300
# Enabling requires a Redis TREG_KV_URL, the same database the publisher writes.
```

With the flag off a plan computes no segment and reads nothing. One snapshot per worker. A plan
never waits for Redis: it uses the snapshot the worker holds, and
the first plan past the refresh interval starts one background read bounded at 200 ms, which
concurrent plans share. A cold worker plans in the YAML order until that first read lands. An
unchanged value (and catalog) is not parsed or checked again. Events: `route_rules_version_observed` when a worker adopts a
version, and `route_rules_config_unavailable` (rate-limited, with `suppressed_since_last_log`) when
it keeps the previous one. They never carry rule content.

## What each call records

Every routed parent that reached a plan carries these separate facts in its event's `input_meta`
(one refused before planning, a 422 for a header the contract does not support, carries none)
([call-events.md](call-events.md)); its children do not, analysis joins them by `parent_ref`:

- `route_order`: which order the first provider actually reached came from: `header`
  (`X-Treg-Route-Prefer`), `default` (the contract's own order would have called it then too),
  `rule`, or `quality` (`X-Treg-Route-Quality` moved it). Absent when no provider was reached,
  whatever the header or rule.
- `route_retry_order`: the same for the first later call the contract's own order would not have
  made next: `rule`, or `quality` (`X-Treg-Route-Quality`, or `X-Treg-Route-Decisive` ordering the
  providers left after an undecided answer). Absent when every later call is the one the contract's
  order would have made, with the rule on or off.

Both are judged attempt by attempt against the providers actually reached. A child has reached
its provider only when its request passed every local check and left, or when that provider's
archived answer was served (`CallContext.reached_provider`). The relay signals the send through
its `ON_SEND` context variable, called after its own address check and credential binding and
immediately before `client.send`; the service sets it around the relay call. A refusal anywhere
before that (cost ceiling, allowance, rate limit, the team's policy or tool access, money
admission, the relay's address check) does not count, whatever its error kind. Each order is read from its
first candidate not yet passed over, and every earlier attempt counts as passed over, reached or
not. So a rule whose pick was refused, skipped, excluded or already first is `default`.
- `route_rules_version`: the published set this worker held, whether or not a rule matched; `0`
  when a read found none published. Absent when the feature is off or the worker has not read yet,
  so "no rule under version 8" and "not read" stay distinct.
- `route_rule`: the id of the rule that matched, even when a header or quality ordering overrode it.

## Storage

`routeruleversion` holds every saved rule set: `version` (the primary key), the complete
`config`, `reason`, `created_by`, `review_status` (`draft`, `approved`, `rejected`) with its
reviewer, time and note, and `publish_status` (`unpublished`, `pending`, `published`, `failed`)
with its publisher and time. A check constraint refuses publishing a version that is not
approved. The private admin is the table's only writer; the call path never reads it. A revert
saves an earlier `config` as a new version.

Disabling is a publication of `enabled: false`, or `TREG_ROUTE_RULES_ENABLED=false` through a
normal deploy. Restore the published key from the table's latest published version before
re-enabling after Redis data loss; a worker cannot tell an older valid backup from the current set.
