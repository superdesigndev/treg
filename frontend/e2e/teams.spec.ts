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

test('a member with no daily cap reads as no limit, and a cap can be set and cleared', async ({ page }) => {
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
})

test('a key\'s spend opens into its daily chart below the row, and closes again', async ({ page }) => {
  const asked: string[] = []
  await page.route(url => /\/api-keys\/\d+\/spend$/.test(url.pathname), route => {
    const days = Number(new URL(route.request().url()).searchParams.get('days'))
    asked.push(String(days))
    const by_day = days === 7 ? [{ day: '2026-10-06', spend_micro: 410000, calls: 1 }]
      : [{ day: '2026-09-14', spend_micro: 95000, calls: 1 }, { day: '2026-10-06', spend_micro: 410000, calls: 1 }]
    return route.fulfill(json({ id: 1, days, since: days === 7 ? '2026-10-03T00:00:00' : '2026-09-10T00:00:00', by_day }))
  })
  await signIn(page, 'key-spend')
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Team', exact: true }).click()
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await expect(page.getByRole('columnheader', { name: 'Spent last 30 days' })).toBeVisible()
  const open = page.getByRole('button', { name: /^See more: daily spend for / })
  await open.click()
  await expect(open).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByText(/over last 30 days · 2 billed calls/)).toBeVisible()
  await expect(page.getByRole('img', { name: /Billed spend per day for .*: \$0\.505 over 30 days/ })).toBeVisible()
  await page.getByRole('button', { name: /^Date range:/ }).click()
  await page.getByRole('option', { name: 'Last 7 days' }).click()
  await expect(page.getByText(/over last 7 days · 1 billed call/)).toBeVisible()
  expect(asked).toEqual(['30', '7'])
  await open.click()
  await expect(page.getByText(/billed call/)).toHaveCount(0)
})

test('Activity and Team tabs live in the address: they survive a reload, and a key\'s Activity opens its calls', async ({ page }) => {
  await signIn(page, 'tab-urls')
  const nav = page.getByRole('navigation', { name: 'Primary navigation' })
  // An admin lands on Usage, the first tab; the address names it.
  await nav.getByRole('button', { name: 'Activity', exact: true }).click()
  await expect(page).toHaveURL(/#activity\/usage$/)
  await expect(page.getByRole('tab', { name: 'Usage' })).toHaveAttribute('aria-selected', 'true')
  await page.getByRole('tab', { name: 'Calls' }).click()
  await expect(page).toHaveURL(/#activity\/calls$/)
  await page.reload()
  await expect(page.getByRole('tab', { name: 'Calls' })).toHaveAttribute('aria-selected', 'true')
  // Team tabs too; the old '#billing' and '#usage' links still land, and say where they landed.
  await nav.getByRole('button', { name: 'Team', exact: true }).click()
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await expect(page).toHaveURL(/#orgs\/keys$/)
  await page.goto('/app#billing')
  await expect(page).toHaveURL(/#orgs\/billing$/)
  await page.goto('/app#usage')
  await expect(page).toHaveURL(/#activity\/usage$/)
  await expect(page.getByRole('tab', { name: 'Usage' })).toHaveAttribute('aria-selected', 'true')
  // A key's Activity button opens that key's calls, the key already picked, by its id.
  await page.goto('/app#orgs/keys')
  await page.locator('.key-table').getByRole('button', { name: 'Activity', exact: true }).first().click()
  await expect(page).toHaveURL(/#activity\/calls\?key=\d+$/)
  await expect(page.getByRole('button', { name: /^API key: Default key · / })).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/#orgs\/keys$/)
})
