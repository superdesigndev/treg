---
title: Agent payments — a keyless caller pays a catalog call with MPP or x402
status: built, behind `agent_payments_enabled` (TREG_AGENT_PAYMENTS_ENABLED), off by default
sources:
  - src/treg/application/agent_payments.py
  - src/treg/infra/agent_payments/__init__.py
  - src/treg/infra/agent_payments/tempo.py
  - src/treg/infra/agent_payments/x402.py
  - src/treg/routers/call.py
  - src/treg/bootstrap.py
  - src/treg/config.py
  - src/treg/models.py
  - src/treg/domain/governance/teams.py
  - src/treg/domain/identity/access.py
  - src/treg/domain/identity/api_keys.py
  - src/treg/application/call/resolve.py
  - src/treg/alembic/versions/0091_agent_payments.py
  - tests/test_agent_payments.py
related:
  - architecture/money.md
  - architecture/proxy-model.md
  - architecture/multi-tenancy.md
  - architecture/import-boundaries.md
---

# Agent payments

An agent with no treg account calls a paid catalog tool on `/call/` and pays for that call with
MPP (the Machine Payments Protocol, Tempo `charge` intent) or x402 (v2, `exact` scheme, EIP-3009).
The payment is the authentication: no signup, no token. Default off; a rail is offered only when
its recipient address is set.

## Who takes the payment path

`routers.call.member_or_payer` returns None (the payment path) only when the feature is on, a rail
is configured, and the request carries **no treg credential at all**: no `X-Treg-Token` header
(an empty or invalid one counts as a credential) and no `treg_session` cookie. Every other request
goes through `require_member` exactly as before, and is never challenged.

`routers.call._paid_call` first checks, without reading the body, that `rest` is a catalog id for
the request's method (`agent_payments.serves`); anything else is today's 401. Only then is the body
read, at most `agent_payments.MAX_BODY_BYTES` (1 MiB; more is 413), because a keyless caller is not
yet anyone. Then `_paid_call` asks `agent_payments.quote` for a price. The quote is None, and the
answer is today's `401 not authenticated`, unless `rest` is a catalog id that treg's own key serves
to a caller holding no credential: `application.call.resolve.platform_quote_micro` (pure, no session)
refuses routed `treg.*` ids, endpoints with a `status`, endpoints with OAuth authorization methods,
anonymous and free endpoints, providers without a configured platform key, and a zero estimate.
Own tools and URL passthrough need a team, so they never reach a payment. Only `/call/` takes this
path; `/catalog/call/`, `/table/` and MCP do not.

## The payer team: how a keyless payer fits the money rules

A payer is an EVM address (lowercase hex); one key controls it on Tempo and on Base.
`governance.teams.make_payer_team` stages, once per address, a team made of a machine user on the
unroutable `payers.treg.local` domain (`identity.access.PAYER_DOMAIN`, a machine email: it cannot
sign in or receive signup credit), a `member` membership with **no token**, and the `AgentPayer`
row binding the address to the team. Nobody can authenticate as that team; only a verified payment
from its address acts for it, and only on this path.

No money entry is new. A payment confirmed on chain is an ordinary `money.topup` on the payer team
with `payment_ref = agentpay:<proof id>` (the unique block reference makes it idempotent). The call
then runs the normal road (`run_call_surface` → `execute_call`) as that team: the normal reserve,
relay, settle or release, the normal `insufficient_balance` 402, the normal hold reaper. The call
runtime is unchanged, so every hold rule holds by construction.

What a call does not use (the estimate minus the actual cost, or all of it on a failed call) stays
as the team's **credit**. That is what "not charged" means for a failed call: the hold is released
and the payer's next call draws the credit without a new transfer.

## Prices: the quote and the payment floor

The quote is `money.with_margin(price_call(...).estimate_micro)`, the amount the call will reserve.
One payment carries `max(quote, agent_payments_min_payment_micro)` (default $0.05). TIP-20 tokens
and USDC use 6 decimals, so one base unit is one micro-USD and the amount crosses as the same
integer (`infra.agent_payments.micro_to_decimal` for the SDK that takes a decimal string).

**Credit first.** `agent_payments._claim` reads the payer team's balance before anything is
settled. When the balance already covers the quote, the presented payment is not settled: it only
proves key control, is recorded as used (status `credit`) and expires unused. Otherwise it is
settled and topped up. So a call at or above the floor pays exactly once on chain, and a call below
it buys floor-sized credit that the next calls from the same address draw down. A payment the
payer already broadcast (MPP push mode, `Verified.deferrable = False`) is always settled.

A credit draw moves no money, so the proof must establish key control before any settlement.
x402: the facilitator's `verify` checks the EIP-3009 signature against `from`. MPP pull mode:
pympp's `validate` recovers only a 65-byte secp256k1 sender signature; keychain, multisig, P256 and
WebAuthn signatures only *name* their account and are authenticated by the chain at settlement. So
`MppTempoRail` marks a pull credential deferrable only when its signature is a recovered one
(`tempo._recovered_sender`); any other MPP payment always settles, credit or not.

## The flow (`application/agent_payments.admit`)

1. The rail whose header is present verifies the credential (`rail.verify`). No session is open:
   pull-mode Tempo checks decode the signed transaction locally, x402 asks the facilitator.
2. `_payer_org`: get or create the payer team (a concurrent create loses on the `AgentPayer` key).
3. `_claim`: one transaction inserts `AgentPayment(proof_id UNIQUE)` with status `credit` or
   `settling` and commits. A unique violation is a replay: 402 with a fresh challenge.
4. If settling: `_settle_and_credit` runs as its own task (`asyncio.shield`), so a caller that
   disconnects mid-settlement is still credited when the transfer lands. No session is open during
   `rail.settle`. A definite refusal marks the row `failed` and answers 402; no call runs and no
   money moves. An unknown outcome (`settlement_unknown`: the node or facilitator did not answer
   after the transfer may have been sent, or x402 `settlement_pending`) leaves the row `settling`
   and answers 503 `payment_outcome_unknown` with no call; the proof cannot be sent again.
   Success tops up and marks the row `settled` in one transaction (`_credit`).
5. The request headers minus the payment credential (`agent_payments.consumes`) go to the call as
   the payer team. Every answer after this point, a refusal included, carries
   `X-Treg-Payment: settled|credit` and, when this payment settled,
   `X-Treg-Payment-Reference: <transaction>`; a 2xx answer also carries the protocol receipt
   (`Payment-Receipt`, `PAYMENT-RESPONSE`).

## The challenge, and the 402s it is not

A missing or unusable payment answers 402 with every rail's challenge: `WWW-Authenticate: Payment`
(one per accepted Tempo currency, HMAC-bound by `pympp` with `agent_payments_secret`, bound to the
method, path and query) and `PAYMENT-REQUIRED` (x402 v2, `resource.url` is the full request URL).
The body is `{"detail": {"error": "payment_required", "amount_micro", "estimated_cost_micro",
"protocols", "reason"?}}`, with `X-Treg-Error: 1` and `Cache-Control: no-store`. A refusal names
`reason`: `payment_replayed`, `invalid_challenge` (another route, query or amount),
`payment_expired`, `malformed_credential`, `unsupported_version` (x402 v1 `X-PAYMENT`),
`verification_failed`, `settlement_failed`, `ambiguous_payment`.

It is told apart from treg's `insufficient_balance` and `route_max_cost` 402s by `detail.error`
and the protocol headers, and from a provider's 402 by `X-Treg-Error`. A provider's 402 on a paid
call is relayed verbatim, its own `WWW-Authenticate` included; treg adds only its usual `X-Treg-*`
headers (`X-Treg-Payment` among them) and no receipt.

## Replays

- MPP: the proof is the transaction hash the chain records, in both modes (pull: pympp's
  canonical-encoding hash of the signed transaction; push: the hash sent). One transfer pays once,
  whichever mode presents it and under whichever challenge it is wrapped.
- x402: network, asset, payer and the EIP-3009 nonce. The token contract also refuses a reused nonce.
- The proof row is committed before any settlement, so two concurrent uses of one proof settle at
  most once.
- `MppTempoRail._check_binding` compares the HMAC-authenticated challenge's route scope with the
  request; `X402Rail.verify` compares the accepted terms and the resource URL with the offer.

## Rails (`infra/agent_payments/`)

`PaymentRail` is the seam: `presented`, `challenge`, `verify`, `settle`, `consumed_headers`.
`bootstrap.configure_agent_payments` builds the configured rails; tests install their own with
`agent_payments.configure`. The SDKs load only inside the concrete rails (an import-linter
contract keeps `mpp` and `x402` there).

- `MppTempoRail` uses `pympp` (`mpp.server.Mpp`, `mpp.methods.tempo.server.charge`):
  `validate_credential` to verify without broadcasting, `broadcast_credential` to settle. Two
  private pympp helpers (the canonical transaction hash, the RLP decoder) are used, so the
  dependency is pinned below the next minor version.
  Fees are not sponsored (`feePayer` off): the client pays its own fee in a USD TIP-20 token.
- `X402Rail` uses the official `x402` SDK: header codecs, `PaymentRequirements`, and an
  `HTTPFacilitatorClient` for `verify` and `settle`; the facilitator pays gas.

treg holds no private key for either rail: it broadcasts the client's signed transaction or asks
the facilitator. Settlement is synchronous, before the relay, to treg's own recipient address.

## Settings

`agent_payments_enabled`, `agent_payments_secret` (32+ characters; startup refuses without it),
`agent_payments_realm`, `agent_payments_min_payment_micro`; Tempo: `..._tempo_recipient`,
`..._tempo_chain_id` (42431 Moderato testnet by default), `..._tempo_rpc_url`,
`..._tempo_currency`; x402: `..._x402_pay_to`, `..._x402_network` (`eip155:84532` Base Sepolia by
default), `..._x402_asset`, `..._x402_asset_name`, `..._x402_asset_version`,
`..._x402_facilitator_url`, `..._x402_max_timeout_s`. Every address is checked to be 20 bytes.

## Not built

- MPP sessions (payment channels with cumulative vouchers): `pympp` has no session server yet.
- Stripe as a settlement rail.
- A worker that reconciles a `settling` row (an unknown settlement outcome, or a crash between
  settlement and top-up), or a credit-first authorization someone else submitted before it expired.
- Withdrawing or expiring credit (there is no refund entry).
