import { afterEach, expect, test, vi } from 'vitest'
import billing, { TOPUP_POLLS } from '../src/state/billing.js'

const glance = (micro: number) => billing.moneyGlance.call({ money: billing.money }, micro)

test('the top bar balance shows cents and never rounds up', () => {
  expect(glance(24_485_672)).toBe('$24.48')
  expect(glance(24_999_999)).toBe('$24.99')
  expect(glance(1_000_000)).toBe('$1.00')
})

test('a balance under a cent says so rather than reading as nothing', () => {
  expect(glance(4_200)).toBe('<$0.01')
  expect(glance(0)).toBe('$0.00')
  expect(glance(-1_234_567)).toBe('-$1.23')
})

test('the Usage spend card reads to the cent, never rounded up, and shows any spend under a cent', () => {
  expect(glance(228_470_706)).toBe('$228.47')   // was $228.470706
  expect(glance(4_200)).toBe('<$0.01')
  expect(glance(0)).toBe('$0.00')
})

const tiers = { billing: { topup: { bonus_tiers: { '10': 0, '50': 5, '100': 10, '200': 15 } } } }
const next = (usd: number) => billing.nextBonusTier.call(tiers, usd)

test('the next bonus tier is the cheapest amount that earns more', () => {
  expect(next(10)).toEqual({ usd: 50, pct: 5 })
  expect(next(50)).toEqual({ usd: 100, pct: 10 })
  expect(next(150)).toEqual({ usd: 200, pct: 15 })
  expect(next(200)).toBeNull()
})

function stripeReturn(balances: (number | null)[], baseline?: number) {
  const data = new Map<string, string>()
  vi.stubGlobal('window', { localStorage: {
    getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => { data.set(k, v) },
    removeItem: (k: string) => { data.delete(k) },
  } })
  let reads = 0
  const ctx: any = {
    ...billing, canAdmin: true, activeOrgId: 3, topupPollMs: 0, billing: { balance_micro: baseline },
    billingNote: '', ticket: () => () => true,
    api: async () => {
      const b = balances[Math.min(reads++, balances.length - 1)]
      if (b === null) throw new Error('down')
      return { org_id: 3, balance_micro: b }
    },
  }
  if (baseline !== undefined) ctx._keepTopupBaseline()
  return { ctx, reads: () => reads }
}

afterEach(() => { vi.unstubAllGlobals() })

test('a top-up return says how much arrived once the balance moves', async () => {
  const { ctx } = stripeReturn([2_000_000, 2_000_000, null, 22_000_000], 2_000_000)
  await ctx.followStripeReturn('topup-success')
  expect(ctx.billingNote).toBe('Added $20.00 to your balance.')
  expect(ctx.billing.balance_micro).toBe(22_000_000)
})

test('a webhook that beat the browser back still reads as added', async () => {
  const { ctx, reads } = stripeReturn([12_000_000], 2_000_000)
  await ctx.followStripeReturn('topup-success')
  expect(ctx.billingNote).toBe('Added $10.00 to your balance.')
  expect(reads()).toBe(1)
})

test('the balance check stops after a bounded number of reads', async () => {
  const { ctx, reads } = stripeReturn([2_000_000], 2_000_000)
  await ctx.followStripeReturn('topup-success')
  expect(reads()).toBe(TOPUP_POLLS)
  expect(ctx.billingNote).toMatch(/^Payment received\. It can take a minute/)
})

test('a cancelled payment says nothing was charged and reads nothing', async () => {
  const { ctx, reads } = stripeReturn([1])
  await ctx.followStripeReturn('topup-cancelled')
  expect(ctx.billingNote).toBe('Payment cancelled. Nothing was charged.')
  expect(reads()).toBe(0)
})
