import { expect, test } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import ReferralsPage from '../src/pages/ReferralsPage.vue'
import { provideDashboard } from '../src/state/context'
import referrals from '../src/state/referrals.js'

async function page(cap: { paid: number; limit: number | null; affiliate: boolean }, tab = 'friend') {
  const data = {
    refTab: tab,
    refCopied: false,
    ref: {
      loaded: true, loading: false, eligible: true, link: 'https://example.test/?ref=friend',
      credit_org: { name: 'My team' },
      cap: { ...cap, standard_limit: 20 },
      terms: { referred_micro: 5_000_000, referrer_micro: 5_000_000, min_topup_micro: 10_000_000, hold_days: 7 },
      totals: { signed_up: 25, topped_up: cap.paid, earned_micro: 0, pending_micro: 0 },
      referrals: [{ email: 'friend@example.test', status: 'capped', reward_micro: 0, signed_up_at: '2026-01-01' }],
    },
  }
  const methods = {
    money: (value: number) => `$${value / 1_000_000}`,
    bhistDate: (value: string) => value,
    refStatus: referrals.refStatus,
    track: () => {}, copyRefLink: () => {}, go: () => {},
  }
  const vm = { ...data, ...methods, $data: data, $options: { methods }, $nextTick: () => {} }
  return renderToString(createSSRApp({
    provide: provideDashboard(vm as any),
    render: () => h(ReferralsPage),
  }))
}

test('ordinary referrers only see the limit warning when their allowance is used', async () => {
  expect(await page({ paid: 19, limit: 20, affiliate: false })).not.toContain('hit the 20-referral limit')
  expect(await page({ paid: 20, limit: 20, affiliate: false })).toContain('hit the 20-referral limit')
})

test('affiliates beyond the ordinary cap see their exemption and historical capped referrals', async () => {
  const html = await page({ paid: 25, limit: null, affiliate: true })
  expect(html).not.toContain('hit the')
  expect(html).toContain('Affiliate access active')
  expect(html).toContain('no lifetime referral limit')
  expect(html).toContain('Over your limit')
})

test('affiliate copy describes credit rewards and does not promise cash payouts', async () => {
  const html = await page({ paid: 25, limit: null, affiliate: true }, 'partner')
  expect(html).toContain('in credit')
  expect(html).toContain('Affiliate access active')
  expect(html).not.toContain('cash')
  expect(html).not.toContain('Apply to be an affiliate partner')
})

test('ordinary users can still apply for the affiliate exemption', async () => {
  const html = await page({ paid: 20, limit: 20, affiliate: false }, 'partner')
  expect(html).toContain('Apply to be an affiliate partner')
  expect(html).toContain('Approval required')
  expect(html).not.toContain('Affiliate access active')
})
