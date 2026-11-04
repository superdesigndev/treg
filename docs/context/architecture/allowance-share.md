---
title: Team shares of an allowance treg's app holds with a provider
status: built, off until TREG_SHARED_ALLOWANCE_SHARES names a service
sources:
  - src/treg/application/call/allowance.py
  - src/treg/application/call/service.py
  - src/treg/oauth_providers.py
  - src/treg/config.py
  - src/treg/infra/kv.py
  - tests/test_allowance_share.py
  - tests/test_kv.py
  - tests/test_call_architecture.py
related:
  - architecture/proxy-model.md
  - architecture/org-endpoint-quota.md
---

# Team shares of an allowance treg's app holds

Some providers meter a daily allowance on the developer app, not on the account a call acts for.
A connection made through treg's own OAuth client, or carrying treg's own second credential, then
spends a pool every team's connection shares: YouTube's Data API units belong to the Cloud project
behind treg's client, and Google Ads operations to treg's developer token. One team's scheduled job
can spend the day for every team. A daily share per team bounds that.

## Which calls draw

`OAuthProvider.shared_allowance` declares the pool. `drawn_by` says which calls spend it:

- `app`: a connection made through treg's client (`Secret.provider` set by the registry connect).
  A connection on the team's own client carries an empty `provider` and is never counted.
- `platform_extra`: a tool carrying treg's own second credential as a `platform_setting` binding
  equal to `extra_credential_setting`. A team that supplies its own value binds a secret instead
  and is never counted.

`units` prices a call in the provider's own units: the first `(call_regex, units)` found in
"METHOD /upstream/path", else one. YouTube's follow Google's cost table in a few bands; Google
Ads counts one per request (a mutate with several operations counts once).

`allowance.draw_for` decides from the tool and the loaded secrets, with no I/O, next to the
paused-provider check, so a catalog call on a connection, a URL passthrough, MCP and hub steps all
pass through it.

## The share

`TREG_SHARED_ALLOWANCE_SHARES` is JSON, `{"<service>": {"daily": n, "orgs": {"<org id>": n}}}`,
validated at boot against `config.SHARED_ALLOWANCE_SERVICES` (kept in step with the registry by a
test). Empty, the default, counts nothing; a service missing from it is not limited. A share bounds
one team; shares may add up to more than the pool.

`allowance.spend_share` runs first in the call's outbound admission, inside the relay's
`before_outbound` hook, so an archived answer or a call the relay refuses before sending spends
nothing. One Lua operation in the shared store (`kv.take_units`, key
`treg:allowance-share:v1:<service>:<org id>:<Pacific day>`, two-day expiry) refuses a spend past
the share without spending it. The day is the Pacific calendar day, the provider's own reset.
Without `TREG_KV_URL` the per-process fallback counts per process; a store that cannot answer
admits the call.

A refused call gets a 429 of kind `shared_allowance_share`, body error
`shared_allowance_share_exceeded` (`provider`, `share`, `used`, `units`, `resets_at`, and a
`message` naming the team's own client or developer token as the way around it), `Retry-After`
until the reset, not charged, audited `refused_by=cap`, with a `capacity` line
`allowance_share_refused`. A call needing more units than the whole share is told it can never
fit, with no `Retry-After`.
