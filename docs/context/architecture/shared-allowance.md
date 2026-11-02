---
title: Shared allowances on treg's app - a connection's calls that spend one pool
status: built, behind `shared_allowance_mode` (TREG_SHARED_ALLOWANCE_MODE), off by default
sources:
  - src/treg/application/call/allowance.py
  - src/treg/application/call/service.py
  - src/treg/oauth_providers.py
  - src/treg/domain/capacity/signatures.py
  - src/treg/domain/capacity/marks.py
  - src/treg/domain/capacity/view.py
  - src/treg/config.py
  - tests/test_shared_allowance.py
  - tests/test_call_architecture.py
related:
  - ops/capacity.md
  - architecture/proxy-model.md
  - architecture/auth-secrets.md
---

# Shared allowances on treg's app

Some providers meter a daily allowance on the developer app, not on the account a call acts for.
A connection made through treg's own OAuth client, or carrying treg's own second credential, then
spends a pool every team's connection shares: YouTube's Data API units belong to the Cloud project
behind treg's client, and Google Ads operations to treg's developer token. Without a guard, one
team's scheduled job spends the day for everyone, and every later call is relayed into the same
quota error until the provider resets.

## Which calls draw

`OAuthProvider.shared_allowance` declares the pool. `drawn_by` says which calls spend it:

- `app`: a connection made through treg's client (`Secret.provider` set by the registry connect).
  A connection on the team's own client carries an empty `provider` and is never counted.
- `platform_extra`: a tool carrying treg's own second credential as a `platform_setting` binding
  equal to `extra_credential_setting`. A team that supplies its own value binds a secret instead
  and is never counted. The binding's `account` gives each of treg's credentials its own pool.

`scopes` names smaller pools inside the allowance: the calls each covers (`call_regex` over
"METHOD /upstream/path"). A scope with `body_regex` is a limit of its own the provider names when
it runs out; one without is a class of costly calls. YouTube has two: `search` (its own daily call
limit) and `writes` (every non-GET, priced far above a read).

`application/call/allowance.draw_for` makes the decision from the tool and the loaded secrets, with
no I/O. It runs at the same point as the paused-provider check, so a catalog call on a connection,
a URL passthrough, MCP and hub steps all pass through it.

## Refuse until the reset

The pool rides the call path's breaker (`domain/capacity/marks.py`, `ops/capacity.md`) under its
own lock keys, `allowance:<service>[#<account>]` and `<that>/<scope>`. Only the provider's own
signature rows judge an answer (`signatures.classify_own`): the generic rows (a bare 402, a 429
with a long `Retry-After`) cannot tell treg's app allowance from one connected account's. A row
gives the reset: a wait stated in the body (`wait_regex`; a stated wait no longer than a burst is a
burst, and a row that expects one but finds none is unknown) or a named reset such as the next
midnight Pacific time. A per-account limit or a short token-wide wait never strikes.

A spent answer strikes the narrowest pool it proves spent: a scope its body names, else the
call's costly scope, else the whole allowance. Costly calls running out (the last units cannot pay
for a write) leaves cheap calls serving; a cheap call told the units are spent locks everything.

Two signatures lock, as on treg's own keys. A lock lasts to the stated reset, bounded by the
lock row's lifetime (`MAX_LOCK`), instead of the six hours that bound a platform key's lock. While
it holds, a drawing call the archive cannot answer is refused before it leaves, with a 503 of kind
`provider_capacity` and body error `shared_allowance_exhausted` (`provider`, `resets_at`, a
`message` naming the team's own client or credential as the way around it), not charged, audited
`refused_by=capacity`. One call
per process per minute is let through as a probe, never answered from the archive, and only
through a lock its answer can prove: the whole allowance's by a call that is not costly, a
scope's by a call in it once the whole allowance is open. A probe's 2xx lifts every lock on the pools the call
draws on. An archive hit is never recovery evidence. `observe` logs `allowance_would_refuse` at
the same point `enforce` refuses, after the archive lookup, so it counts what enforcing would.

The admission reads the cached capacity view after the request's session has ended its DB phase,
before the archive lookup (a probe skips it); the refusal comes after the lookup, inside the
relay's failure handling, so a hold, if any, is released there. A view that cannot be read refuses
nothing. Recovery reads the same cached view as the platform breaker: a 2xx clears a pending
strike its process can see, so across processes "no 2xx in between" holds only within the view's
TTL. Marks are written after the answer, on their own short session, and never raise.

## Mode

`TREG_SHARED_ALLOWANCE_MODE`:

- `off` (default): nothing is classified, locked or refused.
- `observe`: locks are learned and probed, and a call the lock would refuse is relayed with a
  `capacity` line `allowance_would_refuse`.
- `enforce`: the lock refuses.

The lock covers the time after the provider has already said no. Splitting the pool between
teams before it runs out is separate work.
