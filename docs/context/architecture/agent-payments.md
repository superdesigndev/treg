---
title: Agent payments — a keyless caller pays a catalog call with MPP or x402
status: built, behind `agent_payments_enabled` (TREG_AGENT_PAYMENTS_ENABLED), off by default
sources:
  - src/treg/application/agent_payments.py
  - src/treg/infra/agent_payments/__init__.py
  - src/treg/infra/agent_payments/tempo.py
  - src/treg/infra/agent_payments/x402.py
  - src/treg/infra/agent_payments/evm.py
  - src/treg/infra/stripe.py
  - src/treg/worker.py
  - src/treg/routers/call.py
  - src/treg/routers/catalog.py
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
The payment is the authentication: no signup, no token. Default off; each rail has its own switch
(`agent_payments_mpp_enabled`, `agent_payments_x402_enabled`).

## Who takes the payment path

`routers.call.member_or_payer` returns None (the payment path) only when the feature is on, a rail
is configured, and the request carries **no treg credential at all**: no `X-Treg-Token` header
(an empty or invalid one counts as a credential) and no `treg_session` cookie. Every other request
goes through `require_member` exactly as before, and is never challenged.

`routers.call._paid_call` first checks, without reading the body, that `rest` is a catalog id for
the request's method (`agent_payments.unserved`); anything else is a 401. Only then is the body
read, at most `agent_payments.MAX_BODY_BYTES` (1 MiB; more is 413), because a keyless caller is not
yet anyone. Then `_paid_call` hands the request to `agent_payments.open_call`, which prices it
(`quote`), takes the payment or draws credit (`admit`) and drops the payment headers from what
the call relays; the router only maps its three refusals to 401, 402 and 503. The answer is a
401 (`NotServed`) unless `rest` is a catalog id that treg's own key serves to a caller holding no
credential: `application.call.resolve.wallet_refusal` (pure, no session, the static part) and
`platform_quote` (the estimate for this query) refuse routed `treg.*` ids, endpoints with a
`status`, endpoints with OAuth authorization methods, paused providers, anonymous and free
endpoints, providers without a configured platform key, and a zero estimate. The 401 body is
`{"detail": {"error": "not_authenticated", "reason", "message"}}`; `reason` is one of
`not_a_catalog_tool`, `wrong_method`, `routed`, `unavailable`, `needs_connection`, `paused`,
`free`, `own_key_only`, `invalid_request`, and the message says to sign in.
Own tools and URL passthrough need a team, so they never reach a payment.

Requests that carry a payment credential are limited per client address
(`agent_payments_max_payments_per_ip_minute`, default 30, `ratestore` under `agent_payments`),
because each costs a signature check and an x402 one a facilitator request. Over the limit the
answer is 429 `rate_limited` with `Retry-After: 60` and no payment is checked; a request without a
payment still gets its challenge.

One kind of free endpoint is served: an async utility (`resolve.async_utility`: a poll or result
fetch named by a catalog `async` descriptor, or one with `resource_ownership.requires`). Its quote
is a **proof only** (`PaidOffer.proof_only`): the 402 (`reason: proof_required`) asks for a
payment of the minimum, which `admit` verifies and never settles. The wallet must already have a
payer team (`unknown_wallet` otherwise; none is made) and the payment must be one treg can check
without settling (`signature_needed` otherwise). The call then runs as that team, so the call
runtime's own ownership check (`_enforce_platform_async_ownership`) decides whether the task is
this wallet's; no money moves. A challenge for a tool that starts a task says the hold stays open
until the task ends and that polling with the same wallet is free.

While the feature is on, catalog search rows and tool details carry `wallet`
(`agent_payments.wallet_view`): `{payable: true, protocols, minimum_payment_micro, note}`, or
`{payable: true, free_for_task_owner: true, note}` for an async utility, or
`{payable: false, reason, note}` with the same reasons as the 401. Off, the field is absent. Only `/call/` takes this
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
   and answers 503 `payment_outcome_unknown` with no call; the proof cannot be sent again; the
   reconciler resolves it later. Success tops up and marks the row `settled` in one transaction
   (`_credit`).
5. The request headers minus the payment credential (`agent_payments.consumes`) go to the call as
   the payer team. Every answer after this point, a refusal included, carries
   `X-Treg-Payment: paid|credit`, `X-Treg-Credit-Micro` (the payer's credit once the call has
   settled or released) and, when this payment settled, `X-Treg-Paid-Micro` and
   `X-Treg-Payment-Reference: <transaction>`; `X-Treg-Cost-Micro` is the call's charge as on
   every call. A 2xx answer also carries the protocol receipt (`Payment-Receipt`,
   `PAYMENT-RESPONSE`). The answer body is never changed. A paid answer that sets no
   `Cache-Control` gets `Cache-Control: private`. treg's own 402 refusals after a payment
   (`insufficient_balance`, `route_max_cost`) answer 503 on this path, because a payment client
   answers every 402 with another payment.

## The challenge, and the 402s it is not

A missing or unusable payment answers 402 with every rail's challenge: `WWW-Authenticate: Payment`
(one per accepted Tempo currency, HMAC-bound by `pympp` with `agent_payments_secret`, bound to the
method, path and query) and `PAYMENT-REQUIRED` (x402 v2, `resource.url` is the full request URL).
The body is `{"detail": {"error": "payment_required", "reason", "amount_micro",
"estimated_cost_micro", "minimum_payment_micro", "protocols", "message"}}`, with `X-Treg-Error: 1`
and `Cache-Control: no-store`. The message says what to pay and why that amount (the top price, or
the payment minimum), that only the real price is charged, and that what is not used stays as
credit that is not refunded. A first challenge has `reason: payment_required`; a refusal names
`reason`: `payment_replayed`, `invalid_challenge` (another route, query or amount),
`payment_expired`, `malformed_credential`, `unsupported_version` (x402 v1 `X-PAYMENT`),
`verification_failed`, `settlement_failed`, `ambiguous_payment`.

It is told apart from treg's `insufficient_balance` and `route_max_cost` 402s (a signed-in
caller's; a payer gets them as 503) by `detail.error` and the protocol headers, and from a provider's 402 by `X-Treg-Error`. A provider's 402 on a paid
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

## The reconciler (`treg-worker agentpay reconcile`)

Each run prints its result as JSON. `stuck` counts the rows still `settling` past
`agent_payments_stuck_after_s` (default an hour), and a non-zero count is also logged as a
warning: that is the number to alert on.

A row left `settling` (the node or facilitator went silent after the transfer may have been sent, or
the process died between settlement and top-up) is resolved by `agent_payments.reconcile`, once it
is older than `agent_payments_reconcile_min_age_s` (default 300; younger rows belong to their
request). Each row stores what the chain must show: `asset`, `pay_to`, `amount_micro`, the payer, and
`expires_at` (the last moment the authorization can settle). `rail.find` reads the chain with plain
JSON-RPC (`infra/agent_payments/evm.py`), no session open:

- MPP: the receipt of the proof's transaction hash. `expires_at` is the signed transaction's
  `valid_before` (a day when it has none; the moment of the request in push mode, which is mined
  already).
- x402: the token's `AuthorizationUsed(payer, nonce)` log (EIP-3009), searched only between shortly
  before the claim and shortly after `validBefore`, then that transaction's receipt. Public nodes
  prune old blocks and refuse wide log ranges, so `Chain.block_at` searches from the head and
  `Chain.logs` asks in spans of `LOG_SPAN` blocks.

The answer is `landed` (a successful receipt with a transfer of exactly that amount of that asset
from the payer to `pay_to`): `_credit` books the same `topup` under the same `agentpay:<proof id>`
reference, so a request and the reconciler crediting together book it once. `absent` (expired
unsettled, reverted, or a transaction that moved something else): the row becomes `failed`, only if
still `settling`. `pending` (not knowable yet, node unavailable): left for the next run.

## Where the money lands (`agent_payments_settlement`)

- `wallet` (default): the recipient addresses are treg's own wallet; payouts are off-chain work.
- `stripe`: the recipient addresses are Stripe crypto deposit addresses, reusable, one per network,
  made once with `treg-worker agentpay stripe-address --network tempo|base`
  (`infra.stripe.create_crypto_deposit_address`). Stripe converts deposits to fiat in the Stripe
  balance. Payments are rounded up to whole cents (`CENT_MICRO`), because Stripe records cents.
  After settlement the reconciler records each settled payment as a PaymentIntent in
  `transaction_verification` mode (`infra.stripe.record_crypto_payment`, the transaction hash as
  idempotency key) and stores its id in `stripe_payment_intent`; a payment settled to an address
  the rail no longer offers (an earlier wallet) is not recorded. Startup refuses `stripe` without
  `stripe_secret_key`, and without an explicit Tempo currency (one Stripe accepts). x402 on mainnet
  still settles through Coinbase's facilitator in this mode.

## Rails (`infra/agent_payments/`)

`PaymentRail` is the seam: `presented`, `challenge`, `verify`, `settle`, `consumed_headers`.
`bootstrap.configure_agent_payments` builds the configured rails; tests install their own with
`agent_payments.configure`. One SDK, `pympp`, serves both rails; it loads only inside the concrete
rails (an import-linter contract keeps `mpp` there, and the `x402` SDK out of `treg`).

- `MppTempoRail` uses `pympp` (`mpp.server.Mpp`, `mpp.methods.tempo.server.charge`):
  `validate_credential` to verify without broadcasting, `broadcast_credential` to settle. Two
  private pympp helpers (the canonical transaction hash, the RLP decoder) are used, so the
  dependency is pinned below the next minor version.
  Fees are not sponsored (`feePayer` off): the client pays its own fee in a USD TIP-20 token.
- `X402Rail` uses `pympp`'s x402 support (`mpp.x402`): the header codecs, which validate the
  untrusted payload, and an `HttpFacilitator` for `verify` and `settle`; the facilitator pays gas.
  treg's own checks stay in the rail: the accepted terms and resource URL against the offer, the
  verified payer against the signer, and the proof id. The public `x402.org` facilitator serves
  testnets only. On mainnet the facilitator is Coinbase's, which takes a Coinbase Developer
  Platform API key: `mpp.x402.cdp.create_cdp_auth_headers` signs a fresh JWT per request
  (`EdDSA` for an Ed25519 secret, `ES256` for a legacy EC PEM), bound to Coinbase's host and the
  request's path. Startup refuses a CDP key with any other facilitator URL. The `x402` and
  Coinbase SDKs are not used.

treg holds no private key for either rail: it broadcasts the client's signed transaction or asks
the facilitator. Settlement is synchronous, before the relay, to treg's own recipient address.

## Settings

`agent_payments_enabled`, `agent_payments_secret` (32+ characters), `agent_payments_realm`,
`agent_payments_min_payment_micro`; the rail switches `..._mpp_enabled` and `..._x402_enabled`;
Tempo: `..._tempo_recipient`,
`..._tempo_chain_id` (42431 Moderato testnet by default), `..._tempo_rpc_url`,
`..._tempo_currency`; x402: `..._x402_pay_to`, `..._x402_network` (`eip155:84532` Base Sepolia by
default), `..._x402_asset`, `..._x402_asset_name`, `..._x402_asset_version`,
`..._x402_facilitator_url`, `..._x402_max_timeout_s`, `..._x402_rpc_url` (empty: the network's
public RPC), `..._x402_cdp_api_key_id` and `..._x402_cdp_api_key_secret` (both or neither);
`agent_payments_settlement` (`wallet` | `stripe`), `agent_payments_reconcile_min_age_s`,
`agent_payments_stuck_after_s`, `agent_payments_max_payments_per_ip_minute`. Every
address is checked to be 20 bytes.

With the feature on, startup checks the whole set and refuses with one error that names every
problem (`Settings._agent_payments_problems`): the secret; at least one rail switch; each enabled
rail's recipient; for `stripe` settlement the Stripe key and an explicit Tempo currency; on Base
mainnet a CDP key, Base mainnet USDC as the asset and its EIP-712 name `USD Coin`; a CDP key only
with Coinbase's facilitator URL; and both rails on the same kind of network (Tempo 4217 with Base
mainnet, or both testnets).

## Not built

- MPP sessions (payment channels with cumulative vouchers): `pympp` has no session server yet.
- Reconciling a credit-first authorization that someone other than treg submitted before it
  expired (the money arrives with no `settling` row to resolve).
- Withdrawing or expiring credit (there is no refund entry).
