# Agent payments: go-live plan

Temporary working plan for this PR. Remove this file from the branch before merge.

## The shape

- **Two Stripe accounts.** The current account keeps card top-ups, auto top-up and subscriptions,
  unchanged. A second account, the one Stripe approves for stablecoins, receives every agent
  payment (MPP and x402). Stripe gives the deposit addresses, records each payment and pays it out.
- **Coinbase CDP** is the x402 facilitator on mainnet. Stripe's own x402 guide uses it; Stripe does
  not facilitate x402. MPP does not need it.
- **Tempo's hosted RPC** (tempo.xyz/console, API key sent as the `tempo-api-key` header) is the
  Tempo node.
- **Coinbase CDP Node** is the Base node (same CDP account).

## 1. Waiting on

- Stripe enables stablecoin payments on the second account (its card onboarding must be finished
  first; then Settings → Payment methods).

## 2. Code (in this PR)

1. Done: a separate Stripe key for agent payments, `TREG_AGENT_PAYMENTS_STRIPE_SECRET_KEY`, falling
   back to `TREG_STRIPE_SECRET_KEY` when empty.
2. Done: Tempo's hosted RPC key, `TREG_AGENT_PAYMENTS_TEMPO_RPC_API_KEY` (needs
   `TREG_AGENT_PAYMENTS_TEMPO_RPC_URL`), sent as the `tempo-api-key` header by pympp and our chain
   reads.
3. Done: one play-money round in Stripe test mode, with a separate agent-payments Stripe key and
   Tempo's hosted RPC key. One MPP payment (Tempo testnet) and one x402 payment (Base Sepolia, the
   public facilitator) went to Stripe's test deposit addresses, and `agentpay reconcile` recorded
   both as succeeded PaymentIntents.
4. Done: the Coinbase CDP key path against Coinbase's real facilitator: one x402 payment on Base
   Sepolia settled through it and was recorded in Stripe test mode; the facilitator refuses the same
   request without the key (401).
5. Done: treg's fee on each payment, `TREG_AGENT_PAYMENTS_FEE_BPS` (100 = 1%), 0 for now. The
   payment is raised so the credit after the fee still covers the call; the fee is never credited.

## 3. Keys and accounts

| What | Where |
|---|---|
| Agent-payments Stripe secret key | That Stripe account → Developers → API keys |
| Coinbase CDP key (ID + secret) | portal.cdp.coinbase.com |
| Base node URL | CDP portal → Node |
| Tempo RPC API key | tempo.xyz/console |
| Agent payments secret | `openssl rand -hex 32` |
| Stripe deposit addresses | `treg-worker agentpay stripe-address --network tempo` and `--network base` |

Every key goes into the host's secret settings only. Never into chat or this repo.

## 4. Env for mainnet

```
TREG_AGENT_PAYMENTS_ENABLED=true
TREG_AGENT_PAYMENTS_SECRET=<openssl output>
TREG_AGENT_PAYMENTS_SETTLEMENT=stripe
TREG_AGENT_PAYMENTS_STRIPE_SECRET_KEY=<agent-payments Stripe key>
TREG_AGENT_PAYMENTS_FEE_BPS=0   # treg's fee per payment; 0 for now

# MPP on Tempo
TREG_AGENT_PAYMENTS_MPP_ENABLED=true
TREG_AGENT_PAYMENTS_TEMPO_RECIPIENT=<Stripe Tempo deposit address>
TREG_AGENT_PAYMENTS_TEMPO_CHAIN_ID=4217
TREG_AGENT_PAYMENTS_TEMPO_CURRENCY=0x20C000000000000000000000b9537d11c60E8b50
TREG_AGENT_PAYMENTS_TEMPO_RPC_URL=https://api.tempo.xyz/rpc
TREG_AGENT_PAYMENTS_TEMPO_RPC_API_KEY=<Tempo console key>

# x402 on Base
TREG_AGENT_PAYMENTS_X402_ENABLED=true
TREG_AGENT_PAYMENTS_X402_PAY_TO=<Stripe Base deposit address>
TREG_AGENT_PAYMENTS_X402_NETWORK=eip155:8453
TREG_AGENT_PAYMENTS_X402_ASSET=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
TREG_AGENT_PAYMENTS_X402_ASSET_NAME=USD Coin
TREG_AGENT_PAYMENTS_X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
TREG_AGENT_PAYMENTS_X402_CDP_API_KEY_ID=<CDP key id>
TREG_AGENT_PAYMENTS_X402_CDP_API_KEY_SECRET=<CDP key secret>
TREG_AGENT_PAYMENTS_X402_RPC_URL=<CDP Node URL>
```

- `X402_ASSET_NAME` must be `USD Coin` on Base mainnet (read from the contract); the default `USDC`
  is right only on Base Sepolia, and a wrong name fails every x402 signature.
- The worker that runs `agentpay reconcile` needs the same env.

## 5. Order

1. Finish section 2. Remove this file. Review and merge with the feature off.
2. `python -m treg upgrade` (revision 0101).
3. Schedule `treg-worker agentpay reconcile` every few minutes.
4. Stripe enables stablecoins; run the Stripe test-mode round.
5. Create the two deposit addresses; set the env with `SETTLEMENT=stripe`.
6. Turn on MPP first; make one real minimum payment and see it in the Stripe balance.
7. Turn on x402; make one real payment.
8. Update `src/treg/web/llms.txt`, `src/treg/web/skill.md` and `README.md` in the enabling PR.

## 6. Later (not this PR)

- Discovery: the x402 Bazaar extension and per-endpoint price data in OpenAPI, so agents find treg.
- Settle x402 only after the call succeeds (no charge on a failed call); weigh the unpaid-call risk.
- A "pay with crypto" top-up button on the dashboard, on the agent-payments Stripe account, with its
  own webhook.
- A backup RPC URL per chain, if one node proves unreliable.
