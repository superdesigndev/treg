import { expect, test, type Page } from '@playwright/test'
import { json, signIn } from './helpers'

async function switchTo(page: Page, team: string) {
  await page.getByRole('button', { name: 'Teams' }).click()
  const switchButton = page.getByRole('button', { name: 'Switch', exact: true })
  // The innermost block holding both the team's name and a Switch button is that team's row.
  await page.locator('div').filter({ has: page.getByText(team, { exact: true }) }).filter({ has: switchButton })
    .last().getByRole('button', { name: 'Switch', exact: true }).click()
}

test('a slow answer for the team you left never replaces the team you switched to', async ({ page }) => {
  await signIn(page, 'teams', 'First team')
  await page.evaluate(() => fetch('/orgs', {
    method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ name: 'Second team' }),
  }).then(r => r.json()))
  const first = (await page.evaluate(() => fetch('/orgs', { credentials: 'include' }).then(r => r.json())))
    .find((o: { name: string }) => o.name === 'First team')
  await page.reload()
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Your own tools', exact: true }).click()
  await switchTo(page, 'Second team')
  await expect(page.getByRole('button', { name: 'Teams' })).toContainText('Second team')

  // The first team's tool list answers late, and names a tool only that team has.
  let released = false
  await page.route('**/tools', async route => {
    if (route.request().headers()['x-treg-org'] !== first.slug) return route.fallback()
    await new Promise(resolve => setTimeout(resolve, 1500))
    released = true
    await route.fulfill(json([{ id: 990001, name: 'first-team-only', base_url: 'https://first.example', bindings: [], bundle_id: null, cli: null }]))
  })
  await switchTo(page, 'First team')
  await switchTo(page, 'Second team')
  await expect.poll(() => released, { timeout: 5000 }).toBe(true)
  await page.waitForTimeout(300)
  await expect(page.getByRole('button', { name: 'Teams' })).toContainText('Second team')
  await expect(page.getByText('first-team-only')).toHaveCount(0)
})

test('a member with no daily cap reads as no limit, a cap can be set and cleared, and spend waits for the rollup', async ({ page }) => {
  await signIn(page, 'team-cap')
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Team', exact: true }).click()
  const cap = page.getByRole('spinbutton', { name: /^Daily cap for / })
  await expect(cap).toHaveValue('')
  await expect(cap).toHaveAttribute('placeholder', 'No limit')
  await page.getByRole('button', { name: '＋ Add agent' }).click()
  await expect(page.getByRole('spinbutton', { name: 'Daily call cap' })).toHaveValue('')
  for (const value of ['25', '']) {
    await cap.fill(value)
    await cap.press('Tab')
    await page.waitForLoadState('networkidle')
    await page.reload()
    await expect(cap).toHaveValue(value)
  }

  // Spend per API key, on this signed-in session: the suite's sign-ins sit at the per-IP code limit.
  // Both the API Keys column and the Usage chart wait for the rollup, then read it.
  let ready = false
  const asked: string[] = []
  await page.route(url => url.pathname.endsWith('/api-keys/spend'), async route => {
    if (!ready) return route.fulfill(json({ ready: false, days: 30, since: '2026-09-13', keys: [], unattributed: null }))
    return route.fulfill(json({ ready: true, days: 30, since: '2026-09-13', keys: [], unattributed: { spent_micro: 1_250_000, charges: 3 } }))
  })
  await page.route(url => url.pathname.endsWith('/usage/spend'), route => {
    asked.push(new URL(route.request().url()).search)
    if (!ready) return route.fulfill(json({ ready: false, history_from: '2026-09-11' }))
    return route.fulfill(json({
      ready: true, history_from: '2026-09-11', from: '2026-09-12', to: '2026-10-11', group: 'day', stack: 'key',
      spent_micro: 1_500_000, charges: 4,
      series: [{ id: '7', name: 'Laptop · dev', spent_micro: 1_000_000, slot: 0 }, { id: 'none', name: 'No API key', spent_micro: 500_000, slot: 3 }],
      buckets: [{ start: '2026-10-10', parts: { 7: 1_000_000 }, charges: 3 }, { start: '2026-10-11', parts: { none: 500_000 }, charges: 1 }],
      ranking: [{ id: '7', name: 'Laptop · dev', spent_micro: 1_000_000, charges: 3 }, { id: 'none', name: 'No API key', spent_micro: 500_000, charges: 1 }],
      options: { keys: [{ id: 7, name: 'Laptop · dev', spent_micro: 1_000_000 }], providers: [{ id: 'exa', name: 'Exa', spent_micro: 1_500_000 }] },
    }))
  })
  const nav = page.getByRole('navigation', { name: 'Primary navigation' })

  // Not ready: no numbers anywhere, only the being-prepared message.
  await nav.getByRole('button', { name: 'Team', exact: true }).click()
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await expect(page.getByRole('columnheader', { name: 'Spent last 30 days' }).first()).toBeVisible()
  await expect(page.locator('.key-table .key-spend').first()).toHaveText('Preparing…')
  await expect(page.locator('.key-table .key-spend span').first()).toHaveAttribute('title', 'Spend history is being prepared. It will appear here within a few days.')
  await expect(page.getByText('No API key')).toHaveCount(0)
  await nav.getByRole('button', { name: 'Activity', exact: true }).click()
  await page.getByRole('tab', { name: 'Usage' }).click()
  await expect(page.locator('.us-card').getByText('Spend history is being prepared. It will appear here within a few days.')).toBeVisible()
  await expect(page.getByRole('button', { name: /^Date range:/ })).toHaveCount(0)

  ready = true
  await page.getByRole('tab', { name: 'Calls' }).click()
  await page.getByRole('tab', { name: 'Usage' }).click()
  await expect(page.getByText(/History starts on/)).toBeVisible()
  await expect(page.getByRole('img', { name: /Billed spend by day: \$1\.50/ })).toBeVisible()
  await expect(page.locator('.us-totals')).toContainText('4charges')
  await expect(page.locator('.us-totals')).not.toContainText(/calls?/i)   // a count of charges, never of calls
  // The picker offers nothing older than the history: no "Last 3 months", no day before it.
  await page.getByRole('button', { name: /^Date range:/ }).click()
  await expect(page.getByRole('button', { name: 'Last 30 days', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Last 3 months', exact: true })).toHaveCount(0)
  expect(asked.at(-1)).toMatch(/^\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}&group=day&stack=key$/)

  await nav.getByRole('button', { name: 'Team', exact: true }).click()
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await expect(page.getByText('Preparing…')).toHaveCount(0)
  await expect(page.locator('.key-table .key-spend').first()).toHaveText('$0.00')
  await expect(page.getByText('Charges on calls made without an API key')).toBeVisible()
  await expect(page.getByText('$1.25')).toBeVisible()
})
