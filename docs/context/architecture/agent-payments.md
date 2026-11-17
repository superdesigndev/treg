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
  - src/treg/alembic/versions/0104_agent_payments.py
  - src/treg/alembic/versions/0106_agentpayer_per_network.py
  - src/treg/ratestore.py
  - tests/test_agent_payments.py
  - tests/test_agentpayer_network_migration.py
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
the request's method (`agent_payments.unserved`); anything else is a 401. Then the per-address
limit (`agent_payments.limit_request`, below). Only then is the body read, at most `agent_payments.MAX_BODY_BYTES` (1 MiB; more is 413), because a keyless caller is not
yet anyone. Then `_paid_call` hands the request to `agent_payments.open_call`, which prices it
(`quote`), takes the payment or draws credit (`admit`) and drops the payment headers from what
the call relays; the router only maps its three refusals to 401, 402 and 503. The answer is a
401 (`NotServed`) unless `rest` is a catalog id that treg's own key serves to a caller holding no
credential: `application.call.resolve.wallet_refusal` (pure, no session, the static part) and
`platform_quote` (the estimate for this query) refuse routed `treg.*` ids, endpoints with a
`status`, endpoints with OAuth authorization methods, paused providers, endpoints whose `settle`
reads a usage meter (the real cost can exceed the hold, and a wallet has no account to owe the
rest), anonymous and free
endpoints, providers without a configured platform key, and a zero estimate. The 401 body is
`{"detail": {"error": "not_authenticated", "reason", "message"}}`; `reason` is one of
`not_a_catalog_tool`, `wrong_method`, `routed`, `unavailable`, `needs_connection`, `paused`,
`free`, `own_key_only`, `usage_priced`, `invalid_request`, and the message says to sign in.
Own tools and URL passthrough need a team, so they never reach a payment.

Three limits (`ratestore` under `agent_payments`), all answering 429 `rate_limited` with
`Retry-After: 60` and checking or charging nothing:
- per client address, before the body is read (`limit_request`): requests that carry a payment or
  proof (`agent_payments_max_payments_per_ip_minute`, default 30), and requests that carry none
  (`..._max_quotes_per_ip_minute`, default 120), each of which is priced;
- across every client (`_limit_verification`): payment and proof checks that reach the network
  (`..._max_verifications_minute`, default 600). A rail spends the slot (`verify(...,
  before_remote=)`) just before its first Tempo RPC or facilitator request, and a verified payment
  spends it before it is claimed (an MPP pull transfer is checked locally and first reaches the
  chain at settlement); one request spends at most one. A credential its local checks refuse
  (undecodable, another route's challenge, a bad HMAC) and a proof checked locally (SIWX, an MPP
  proof signed by the account itself) cost none, so junk headers from many addresses cannot lock
  paying wallets out. Many addresses cannot get around it; reaching it is logged.

These checks are atomic (`rate_check(..., atomic=True)`): one upsert creates or locks the key's row
until the commit, so concurrent requests count every hit. Other `ratestore` callers keep the
unlocked read.

One kind of free endpoint is served: an async utility (`resolve.async_utility`: a poll or result
fetch named by a catalog `async` descriptor, or one with `resource_ownership.requires`). Its quote
is a **proof only** (`PaidOffer.proof_only`): the 402 (`reason: proof_required`) offers MPP's
zero-amount proof challenge and, on x402, a payment of the minimum that `admit` verifies and never
settles (no MPP payment challenge: an MPP payment always settles). The wallet must already have a
payer team on that network (`unknown_wallet` otherwise; none is made), and an x402 payment must be
one treg can check without settling and that expires soon (`signature_needed` otherwise; see
*Short-lived proofs*). The call then runs as that team, so the call
runtime's own ownership check (`_enforce_platform_async_ownership`) decides whether the task is
this wallet's; no money moves. A challenge for a tool that starts a task says the hold stays open
until the task ends and that polling with the same wallet is free.

While the feature is on, catalog search rows and tool details carry `wallet`
(`agent_payments.wallet_view`): `{payable: true, protocols, minimum_payment_micro, fee_bps, note}`, or
`{payable: true, free_for_task_owner: true, note}` for an async utility, or
`{payable: false, reason, note}` with the same reasons as the 401. Off, the field is absent.
The agent-facing files (`llms.txt`, `skill.md`) carry the wallet road inside
`<!--agentpay-->...<!--/agentpay-->` blocks, kept only while `agent_payments.enabled()`
(`routers.web._strip_routed`), so a deployment with the feature off never teaches it. Only `/call/` takes this
path; `/catalog/call/`, `/table/` and MCP do not.

## The payer team: how a keyless payer fits the money rules

A payer is an EVM address (lowercase hex) **on one network**. One key can control the address on
Tempo and on Base, but the same address may have a different owner per chain (a smart wallet), so
credit on one network never pays on another. `governance.teams.make_payer_team` stages, once per
(network, address), a team made of a machine user on the unroutable `payers.treg.local` domain
(`<address>.<network slug>@payers.treg.local` ; `identity.access.PAYER_DOMAIN`, a machine email: it cannot
sign in or receive signup credit), a `member` membership with **no token**, and the `AgentPayer`
row (primary key `(network, address)`, revision 0106) binding the wallet to the team. A wallet whose team was deleted keeps its machine user, which
the next payment joins to a new team. Nobody can authenticate as that team; only a verified payment
or proof from its address on its network acts for it, and only on this path.

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
One payment carries `max(with_fee(quote), agent_payments_min_payment_micro)` (default $0.05).
**Fee.** `agent_payments_fee_bps` (default 0; 100 = 1%) is treg's cut of each settled payment.
`with_fee` raises the quote so the payment, less the fee, still covers it; `_claim` stores the fee
as `AgentPayment.fee_micro` (0 on a credit draw), and `_credit` tops up `amount_micro - fee_micro`,
so the fee is never credited and a later setting change does not alter a recorded payment. Stripe
records the whole payment. TIP-20 tokens
and USDC use 6 decimals, so one base unit is one micro-USD and the amount crosses as the same
integer (`infra.agent_payments.micro_to_decimal` for the SDK that takes a decimal string).

**Credit first, MPP.** A wallet with credit proves itself with a **zero-amount proof** and pays
nothing (`_admit_proof`). The 402 lists the rail's proof challenge (a Tempo charge challenge
with `amount: "0"`) before its payment challenge, because standard clients (pympp, mppx) answer
the first challenge they can pay. The client signs an EIP-712 `Proof(account, challengeId,
realm)`; pympp's `validate` checks it (no chain call, unless the signer is an access key of the
account) and names the payer. The proof's challenge is HMAC-bound to the method, path and query
like any MPP challenge, and its id is claimed once (`ratestore.kv_claim` under `agentpay-proof`,
kept past the challenge's expiry; each claim first sweeps the namespace's expired rows, as each
per-address limit sweeps its own, so neither grows without bound). If the wallet's payer team on that network holds at least the
quote, the call runs on that credit: no row, no settlement, `X-Treg-Payment: credit`. Otherwise
(including a wallet treg has never seen) the answer is 402 `credit_insufficient` with the payment
challenges only, and the client pays. A brand-new wallet therefore takes one more round trip; the
clients retry up to 3 times. If the credit is spent by another call between the check and the hold,
the call's `insufficient_balance` answers the same `credit_insufficient` 402
(`agent_payments.credit_short`). An MPP **payment** is always settled (`deferrable = False`): treg
never holds an MPP transfer it did not send. mppx refuses to sign a proof whose realm is not the
hostname it called (it throws rather than choosing another challenge), so the proof challenge is
offered only when the request's `Host` equals the rail's realm (`Offer.host`); any other host gets
the payment challenges only.

**Credit first, x402 with SIWX.** The x402 402 carries the `sign-in-with-x` extension
(`mpp.x402.siwx.create_extension`: domain and URI from `public_url`, a fresh nonce, five minutes,
`eip155:<chain>` with `eip191` signatures). The client signs an EIP-4361 (Sign-In with Ethereum)
message and sends it in `SIGN-IN-WITH-X`; `X402Rail` checks it with `siwx.verify` (domain, URI
origin, times, chain, EOA signature recovery; no facilitator call) and then requires its `uri` to
be this request's resource URL, so it binds the route and query as an MPP proof does. treg claims
the proof once under `agentpay-proof`, keyed by network, address and nonce, and `_admit_proof`
treats it as the MPP proof above. As with MPP, the extension is offered only when the request's
`Host` is `public_url`'s host (clients sign only for the host they called), and the
`credit_insufficient` 402 leaves it out. Smart-contract wallets (EIP-1271) are not verified.

**Credit first, x402 without SIWX.** A client that sends a payment instead: `agent_payments._claim` reads the
payer team's balance before anything is settled. When the balance already covers the quote, the
presented payment is not settled: it only proves key control, is recorded as used (status `credit`)
and expires unused. Otherwise it is settled and topped up. So a call at or above the floor pays
exactly once on chain, and a call below it buys floor-sized credit that the next calls from the same
address draw down.

**Short-lived proofs (x402).** A payment treg does not settle stays valid until it expires, and anyone
holding it may submit it. So `admit` lets it draw credit (or prove a poll) only when its
`expires_at` is within `agent_payments_credit_proof_max_s` (default 150); a longer-lived one is
settled like any payment (a poll answers `signature_needed`). A pull-mode Tempo transfer with no
`valid_before` is refused outright (`expiry_required`): it stays valid until its nonce is used, so
the reconciler could never tell that it can no longer land. Standard clients
sign short ones: x402 sets `validBefore` from the challenge's `maxTimeoutSeconds`
(`..._x402_max_timeout_s`, 120), and pympp's Tempo client signs an expiring nonce valid 60 seconds,
capped by the challenge's `expires`, which `MppTempoRail` sets `..._mpp_challenge_ttl_s` (120)
ahead. The reconciler then looks each expired `credit` row up once (below).

A credit draw moves no money, so the proof must establish key control before any settlement.
x402: the facilitator's `verify` checks the EIP-3009 signature against `from`. MPP: the
zero-amount proof's signature is recovered against the account it names, or checked on chain as
that account's access key.

## The flow (`application/agent_payments.admit`)

1. The rail whose header is present verifies the credential (`rail.verify`), after the check cap.
   No session is open: Tempo checks decode the signed transaction or proof locally, x402 asks the
   facilitator. A proof goes to `_admit_proof` (above) and stops here.
2. `_payer_org`: get or create the payer team for (network, address) (a concurrent create loses on the `AgentPayer` key
   or the machine user's email, rolls back and reads the winner's team). `_suspension` then
   refuses a suspended team or member with 403 `payer_suspended`, as `require_member` does on the
   account path, before the proof is claimed or anything settles. `agentpayer` and `agentpayment`
   carry `org_id` with no foreign key (AGENTS.md); team deletion removes both.
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
5. The request headers minus every payment credential header, whichever rails are on
   (`agent_payments.consumes`), go to the call as
   the payer team. Every answer after this point, a refusal included, carries
   `X-Treg-Payment: paid|credit`, `X-Treg-Credit-Micro` (the payer's credit once the call has
   settled or released) and, when this payment settled, `X-Treg-Paid-Micro` and
   `X-Treg-Payment-Reference: <transaction>`; `X-Treg-Cost-Micro` is the call's charge as on
   every call. A 2xx answer also carries the protocol receipt (`Payment-Receipt`,
   `PAYMENT-RESPONSE`). The answer body is never changed. A paid answer that sets no
   `Cache-Control` gets `Cache-Control: private`. treg's own 402 refusals after a payment
   (`insufficient_balance`, `route_max_cost`) answer 503 on this path, because a payment client
   answers every 402 with another payment; after a credit draw, `insufficient_balance` is the
   `credit_insufficient` payment challenge instead.

## The challenge, and the 402s it is not

A missing or unusable payment answers 402 with every rail's challenge: first the MPP zero-amount
proof challenges (on the realm's host), then `WWW-Authenticate: Payment` (one per accepted Tempo
currency, HMAC-bound by `pympp` with `agent_payments_secret`, bound to the method, path and query)
and `PAYMENT-REQUIRED` (x402 v2, `resource.url` is the full request URL).
The body is `{"detail": {"error": "payment_required", "reason", "amount_micro",
"estimated_cost_micro", "minimum_payment_micro", "fee_bps", "protocols", "message"}}`, with `X-Treg-Error: 1`
and `Cache-Control: no-store`. The message leads with the call's top price, then what to pay (the
smallest payment when that is higher), that only the real price is charged and the rest stays as
the wallet's credit (not paid back as money), the fee when it is not 0, and, with a proof
challenge, that a wallet with credit signs it and pays nothing now. A first challenge has
`reason: payment_required`; a refusal names `reason`: `credit_insufficient` (a proof whose credit
does not cover the call; payment challenges only), `payment_replayed`, `invalid_challenge` (another route, query or amount),
`payment_expired`, `malformed_credential`, `expiry_required` (a Tempo transfer with no
`valid_before`), `unsupported_version` (x402 v1 `X-PAYMENT`),
`verification_failed`, `settlement_failed`, `ambiguous_payment`, `unknown_wallet` and
`signature_needed` (polls). A malformed x402 payload
(`malformed_credential`, the payload's `validBefore` included) is refused before the facilitator is
asked. A facilitator failure that names a transaction is `settlement_unknown`: the row stays
`settling` and the reconciler asks the chain.

It is told apart from treg's `insufficient_balance` and `route_max_cost` 402s (a signed-in
caller's; a payer gets them as 503) by `detail.error` and the protocol headers, and from a provider's 402 by `X-Treg-Error`. A provider's 402 on a paid
call is relayed verbatim, its own `WWW-Authenticate` included; treg adds only its usual `X-Treg-*`
headers (`X-Treg-Payment` among them) and no receipt.

## Replays

- MPP proof: `mpp:<network>:proof:<challenge id>`, claimed once in `ratestore`; a proof is valid
  only for its challenge's route and query, until the challenge expires.
- MPP payment: the proof is the transaction hash the chain records, in both modes
  (`Validation.details["transaction_hash"]`; a transaction a fee payer would co-sign has none yet
  and is refused). One transfer pays once,
  whichever mode presents it and under whichever challenge it is wrapped.
- x402: network, asset, payer and the EIP-3009 nonce. The token contract also refuses a reused nonce.
- The proof row is committed before any settlement, so two concurrent uses of one proof settle at
  most once.
- `MppTempoRail` passes the request to pympp (`http_request=`), which requires the
  HMAC-authenticated challenge's route scope (method, route, path, query) to match it; `X402Rail.verify` compares the accepted terms and the required `resource.url` (the full request URL) with the offer.
- Neither rail binds the request body. Every call priced under the floor asks the same amount, so a
  copied, unused payment can pay once for another body of the same route and query, as its wallet.

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
  `valid_before` (always present: one without it is refused; the moment of the request in push
  mode, which is mined already).
  Tempo has no reorgs, so a confirmed receipt is final.
- x402: the token's `AuthorizationUsed(payer, nonce)` log (EIP-3009), searched only between shortly
  before the claim and shortly after `validBefore`, then that transaction's receipt. Public nodes
  prune old blocks and refuse wide log ranges, so `Chain.block_at` searches from the head and
  `Chain.logs` asks in spans of `LOG_SPAN` blocks. Base can reorg recent blocks, so the window ends
  at the head tagged `..._x402_block_tag` (default `finalized`): a transfer counts as landed, and a
  payment as expired, only once a block that no reorg can undo shows it.

The answer is `landed` (a successful receipt with a transfer of at least that amount of that asset
from the payer to `pay_to`): `_credit` books the same `topup` under the same `agentpay:<proof id>`
reference, so a request and the reconciler crediting together book it once. `absent` (expired
unsettled, reverted, or a transaction that moved something else): the row becomes `failed`, only if
still `settling`. `pending` (not knowable yet, node unavailable): left for the next run.

A `credit` row (x402 only: MPP draws credit with a proof, which leaves no row) is looked up the same way, once, after its `expires_at` plus `CREDIT_PROOF_GRACE`,
when the chain's answer is final. `landed` (someone submitted the payment anyway): the row moves to
`settling` in a conditional update, so one reconciler wins, and `_credit` tops the team up; the
payer loses nothing and the ledger holds the money. `absent`: the row becomes `expired`. `pending`:
the next run asks again. Short-lived proofs keep this set to the last few minutes of credit draws.

Both lookups select only rows whose protocol and network belong to a rail this process has
configured, so the rows of a switched-off rail wait in the table and never fill a batch ahead of
rows that can be resolved. They count in `stuck` until that rail is configured again.

## Where the money lands (`agent_payments_settlement`)

- `wallet` (default): the recipient addresses are treg's own wallet; payouts are off-chain work.
- `stripe`: the recipient addresses are Stripe crypto deposit addresses, reusable, one per network,
  made once with `treg-worker agentpay stripe-address --network tempo|base`
  (`infra.stripe.create_crypto_deposit_address`). Stripe converts deposits to fiat in the Stripe
  balance. Payments are rounded up to whole cents (`CENT_MICRO`), because Stripe records cents.
  After settlement the reconciler records each settled payment as a PaymentIntent in
  `transaction_verification` mode (`infra.stripe.record_crypto_payment`, the transaction hash as
  idempotency key, and metadata `treg_agent_payment_id`, `treg_team_id`, `payer_address`,
  `protocol`, `network` so support finds a payment by wallet) and stores its id in
  `stripe_payment_intent`; a payment settled to an address
  the rail no longer offers (an earlier wallet) is not recorded. The query itself selects only
  payments to a current deposit address, so older ones never fill the batch. Stablecoin acceptance is enabled
  per Stripe account, so `agent_payments_stripe_secret_key` may name another account than
  `stripe_secret_key` (card top-ups); empty, the two are one. Startup refuses `stripe` with neither
  key, and without an explicit Tempo currency (one Stripe accepts). x402 on mainnet
  still settles through Coinbase's facilitator in this mode.

## Rails (`infra/agent_payments/`)

`PaymentRail` is the seam: `presented`, `proves` (whether this request can be asked for a wallet
proof), `challenge(offer, pay=, prove=)` (the proof first, then the payment), `verify` (`Verified.kind` is `payment` or `proof`), `settle`, `find`,
`consumed_headers`, `deferrable_payments` (False on MPP: every payment settles).
`bootstrap.configure_agent_payments` builds the configured rails; tests install their own with
`agent_payments.configure`. One SDK, `pympp`, serves both rails; it loads only inside the concrete
rails (an import-linter contract keeps `mpp` there, and the `x402` SDK out of `treg`).

- `MppTempoRail` uses `pympp` (`mpp.server.Mpp`, `mpp.methods.tempo.server.charge`):
  `validate_credential` to verify without broadcasting, `broadcast_credential` to settle, both
  bound to the request's route. pympp claims each transaction hash in a replay store, and refuses
  to start without a shared one on a production or multi-instance platform (it reads `RENDER`,
  `FLY_APP_NAME` and the like); `agent_payments.ReplayStore` is that store on the `Ephemeral` table
  (`agentpay-mpp`, kept a week: every transfer expires within minutes, and `AgentPayment.proof_id`
  refuses a second use for good). Only public pympp interfaces are used; the dependency is still
  pinned below the next minor version, as pympp is pre-1.0.
  Fees are not sponsored (`feePayer` off): the client pays its own fee in a USD TIP-20 token.
  With `..._tempo_rpc_api_key` (Tempo's hosted RPC), one `httpx.AsyncClient` carrying it in the
  `tempo-api-key` header serves both pympp (`http_client`) and the reconciler's `evm.Chain`.
- `X402Rail` uses `pympp`'s x402 support (`mpp.x402`): the header codecs, which validate the
  untrusted payload, `mpp.x402.siwx` for the SIWX proof, and an `HttpFacilitator` for `verify` and
  `settle`; the facilitator pays gas.
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
`agent_payments_min_payment_micro`, `agent_payments_fee_bps`, `agent_payments_credit_proof_max_s`,
`agent_payments_mpp_challenge_ttl_s`; the rail switches `..._mpp_enabled` and `..._x402_enabled`;
Tempo: `..._tempo_recipient`,
`..._tempo_chain_id` (42431 Moderato testnet by default), `..._tempo_rpc_url`,
`..._tempo_rpc_api_key` (needs `..._tempo_rpc_url`),
`..._tempo_currency`; x402: `..._x402_pay_to`, `..._x402_network` (`eip155:84532` Base Sepolia by
default), `..._x402_asset`, `..._x402_asset_name`, `..._x402_asset_version`,
`..._x402_facilitator_url`, `..._x402_max_timeout_s`, `..._x402_rpc_url` (empty: the network's
public RPC; required on Base mainnet), `..._x402_block_tag` (`latest` | `safe` | `finalized`,
default `finalized`), `..._x402_cdp_api_key_id` and `..._x402_cdp_api_key_secret` (both or neither);
`agent_payments_settlement` (`wallet` | `stripe`), `agent_payments_stripe_secret_key` (empty:
`stripe_secret_key`), `agent_payments_reconcile_min_age_s`,
`agent_payments_stuck_after_s`, `agent_payments_max_payments_per_ip_minute`,
`agent_payments_max_quotes_per_ip_minute`, `agent_payments_max_verifications_minute`. Every
address is checked to be 20 bytes.

With the feature on, startup checks the whole set and refuses with one error that names every
problem (`Settings._agent_payments_problems`): the secret; at least one rail switch; each enabled
rail's recipient; for `stripe` settlement a Stripe key and an explicit Tempo currency; a Tempo RPC API key only
with its RPC URL; on Base
mainnet a CDP key, its own RPC URL, Base mainnet USDC as the asset and its EIP-712 name `USD Coin`; a CDP key only
with Coinbase's facilitator URL; and both rails on the same kind of network (Tempo 4217 with Base
mainnet, or both testnets).

## Reading credit (`GET /agentpay/credit`)

A keyless wallet reads its credit after the same proof, MPP's zero-amount proof or x402's SIWX
(`agent_payments.credit_of`). The first request answers 402 `proof_required` with the proof
challenges only (x402's format needs a payment in `accepts`, so it lists one, which the route
refuses); a request
with a valid, unused proof answers `{network, address, credit_micro, recent}` (the last 20
`agentpayment` rows of that wallet's team on that network: `at`, `protocol`, `amount_micro`,
`status`, `reference`), `Cache-Control: no-store`. A wallet treg has never seen reads 0 and no rows.
A payment sent instead of a proof is refused (`proof_required`) and never settled. The same limits
apply. The route answers 404 while the feature is off.

## Not built

- MPP sessions (payment channels with cumulative vouchers): `pympp` now ships a session server;
  treg does not use it yet.
- x402 `upto` (pay up to a maximum): not in `pympp`.
- Smart-contract wallet signatures (EIP-1271) for the x402 sign-in-with-x proof. Such a wallet can
  still pay; to use its credit it signs a payment that treg only checks and never settles.
- Using part of the credit toward a payment (a 402 that asks only for the missing part).
- Withdrawing or expiring credit (there is no refund entry).
