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
  const staleAnswer = page.waitForEvent('requestfinished', request =>
    request.url().endsWith('/tools') && request.headers()['x-treg-org'] === first.slug, { timeout: 5000 })
  await switchTo(page, 'First team')
  await switchTo(page, 'Second team')
  await staleAnswer
  expect(released).toBe(true)
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))))
  await expect(page.getByRole('button', { name: 'Teams' })).toContainText('Second team')
  await expect(page.getByText('first-team-only')).toHaveCount(0)
})

test('a member with no daily cap reads as no limit, a cap can be set and cleared, spend waits for the rollup, and tabs live in the address', async ({ page }) => {
  await signIn(page, 'team-cap')
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Team', exact: true }).click()
  const cap = page.getByRole('spinbutton', { name: /^Calls per day for / })
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

  // The team's daily spend default lives behind the Spend / day header, in a dialog.
  const setAll = page.getByRole('button', { name: 'Set for all' })
  await setAll.click()
  const dialog = page.getByRole('dialog', { name: 'Daily spend for each member/agent' })
  await dialog.getByRole('spinbutton', { name: 'Daily spend limit for every member' }).fill('5')
  await dialog.getByRole('button', { name: 'Save' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'All: $5.00' })).toBeVisible()
  await page.reload()
  // The owner never follows the team default, so their row offers only No limit and Custom.
  const ownerSpend = page.getByRole('combobox', { name: /^Daily spend limit for / }).first()
  await expect(ownerSpend.locator('option')).toHaveText(['No limit', 'Custom'])
  await expect(ownerSpend).toHaveValue('none')
  await page.getByRole('button', { name: 'All: $5.00' }).click()
  await dialog.getByRole('button', { name: 'Remove limit' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(setAll).toBeVisible()

  // One member's own limit: a menu of team default / no limit / custom. A custom amount saves when
  // the box loses focus, takes whole dollars only, and survives a reload.
  const spend = page.getByRole('combobox', { name: /^Daily spend limit for / }).first()
  await spend.selectOption('custom')
  const amount = page.getByRole('spinbutton', { name: /^Custom daily spend for / }).first()
  await expect(amount).toBeFocused()
  await amount.fill('7')
  await amount.blur()
  await expect(page.getByRole('status').filter({ hasText: 'Spend limit saved for' })).toBeVisible()
  await page.reload()
  await expect(spend).toHaveValue('custom')
  await expect(amount).toHaveValue('7')
  await spend.selectOption('none')
  await expect(amount).toHaveCount(0)
  await page.reload()
  await expect(spend).toHaveValue('none')

  // Spend per API key: both the API Keys column and the Usage chart wait for the rollup, then read it.
  let ready = false
  const asked: string[] = []
  await page.route(url => url.pathname.endsWith('/api-keys/spend'), async route => {
    if (!ready) return route.fulfill(json({ ready: false, days: 30, since: '2026-09-13', keys: [], unattributed: null }))
    return route.fulfill(json({ ready: true, days: 30, since: '2026-09-13', keys: [], unattributed: { spent_micro: 1_250_000, charges: 3 } }))
  })
  await page.route(url => url.pathname.endsWith('/usage/spend'), async route => {
    asked.push(new URL(route.request().url()).search)
    // A month answer takes a moment, as a real one can: long enough to point at the old bars.
    if (new URL(route.request().url()).searchParams.get('group') === 'month') await new Promise(r => setTimeout(r, 700))
    if (!ready) return route.fulfill(json({ ready: false, history_from: '2026-09-11' }))
    return route.fulfill(json({
      ready: true, history_from: '2026-09-11', from: '2026-09-12', to: '2026-10-11', stack: 'key',
      group: new URL(route.request().url()).searchParams.get('group') || 'day',
      spent_micro: 1_504_321, charges: 4,
      series: [{ id: '7', name: 'Laptop · dev', spent_micro: 1_000_000, slot: 0 }, { id: 'none', name: 'No API key', spent_micro: 500_000, slot: 3 }],
      // By day, thirty bars; by month, two. A bar remembered from the day view must not break the month view.
      buckets: new URL(route.request().url()).searchParams.get('group') === 'month'
        ? [{ start: '2026-09-01', parts: {}, charges: 0 }, { start: '2026-10-01', parts: { 7: 1_000_000, none: 500_000 }, charges: 4 }]
        : Array.from({ length: 30 }, (_, i) => ({ start: new Date(Date.UTC(2026, 8, 12 + i)).toISOString().slice(0, 10),
            parts: i === 28 ? { 7: 1_000_000 } : i === 29 ? { none: 500_000 } : {}, charges: i === 28 ? 3 : i === 29 ? 1 : 0 })),
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
  // The card reads to the cent; the exact amount appears at once on hover, and on keyboard focus.
  const total = page.locator('.us-totals b').first()
  await expect(total).toContainText('$1.50')
  const exact = total.getByRole('tooltip')
  await expect(exact).toBeHidden()
  await total.locator('.money').hover()
  await expect(exact).toBeVisible()
  await expect(exact).toHaveText('$1.504321')
  await page.mouse.move(0, 0)
  await expect(exact).toBeHidden()
  await total.locator('.money').focus()
  await expect(exact).toBeVisible()
  await total.locator('.money').blur()
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

  // Tabs live in the address: a reload keeps the tab, and a key's Activity opens its calls by id.
  await expect(page).toHaveURL(/#orgs\/keys$/)
  await page.reload()
  await expect(page.getByRole('columnheader', { name: 'Spent last 30 days' }).first()).toBeVisible()
  await page.locator('.key-table').getByRole('button', { name: 'Activity', exact: true }).first().click()
  await expect(page).toHaveURL(/#activity\/calls\?key=\d+$/)
  await expect(page.getByRole('tab', { name: 'Calls' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('button', { name: /^API key: Default key · / })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: /^API key: Default key · / })).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/#orgs\/keys$/)

  // The counts: every section loads on its own; Top tools and Per day page ten rows at a time.
  await page.route(url => /\/orgs\/\d+\/usage$/.test(url.pathname), route => route.fulfill(json({
    days: 30, since: '2026-09-12T00:00:00',
    totals: { call: 60, local_run: 0, server_run: 0, total: 60 },
    by_user: [{ user_email: 'dev@example.com', call: 60, local_run: 0, server_run: 0, total: 60 }],
    by_tool: Array.from({ length: 23 }, (_, i) => ({ name: `tool${String(i).padStart(2, '0')}`, total: 30 - i })),
    by_day: Array.from({ length: 12 }, (_, i) => ({ day: `2026-10-${String(i + 1).padStart(2, '0')}`, total: i + 1 })),
    spend: { spend_micro: 0, spend_usd: 0, billed_calls: 0 },
  })))
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  // An old '#usage' link lands on the Usage tab and names it (it once left a blank page).
  await page.goto('/app#usage')
  await expect(page).toHaveURL(/#activity\/usage$/)
  await expect(page.getByRole('tab', { name: 'Usage' })).toHaveAttribute('aria-selected', 'true')
  const tools = page.locator('section').filter({ has: page.getByRole('heading', { name: /Top tools/ }) })
  await expect(tools.getByText('1–10 of 23')).toBeVisible()
  await expect(tools.getByRole('row')).toHaveCount(11)   // the header and ten rows
  await tools.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(tools.getByText('11–20 of 23')).toBeVisible()
  await expect(tools.getByText('tool10', { exact: true })).toBeVisible()
  const days = page.locator('section').filter({ has: page.getByRole('heading', { name: /Per day/ }) })
  await expect(days.getByRole('row').nth(1)).toContainText('2026-10-12')   // newest first
  await days.getByRole('button', { name: 'Older', exact: true }).click()
  await expect(days.getByText('11–12 of 12')).toBeVisible()

  // The chart keeps drawing when the bars change under a hovered one: pick By month, and while
  // its answer loads, point at the day view's last bar; then the two month bars arrive.
  await page.getByRole('button', { name: 'Group by: By day' }).click()
  await page.getByRole('option', { name: 'By month' }).click()
  const plot = page.locator('.sb-plot')
  const box = (await plot.boundingBox())!
  await plot.hover({ position: { x: box.width - 4, y: box.height / 2 } })
  await expect(page.getByRole('img', { name: /Billed spend by month: \$1\.50/ })).toBeVisible()
  await expect(plot.locator('rect').first()).toBeVisible()
  expect(errors).toEqual([])

  // The chart's key filter belongs to the chart alone: the address does not carry it.
  await page.getByRole('button', { name: /^API key: All API keys/ }).click()
  await page.getByRole('option', { name: 'Laptop · dev' }).click()
  await expect(page.getByRole('button', { name: /^API key: Laptop · dev/ })).toBeVisible()
  await expect(page).toHaveURL(/#activity\/usage$/)

  // And '#billing', the name 402 bodies and emails use, lands on Team's Billing tab and says so.
  await page.goto('/app#billing')
  await expect(page).toHaveURL(/#orgs\/billing$/)
})
