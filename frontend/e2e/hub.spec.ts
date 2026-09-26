import { expect, test, type Page } from '@playwright/test'
import tool from './fixtures/hub-tool.json' with { type: 'json' }
import health from './fixtures/hub-health.json' with { type: 'json' }
import run from './fixtures/hub-run.json' with { type: 'json' }

// The hub is behind TREG_HUB_ENABLED (and TREG_HUB_TEAMS) on the server; the browser-test server
// runs with it off. So the first test proves the entry stays hidden when /hub/tools/mine answers
// 404, and the second answers the hub routes here, with real shapes captured from a live run.

async function signIn(page: Page) {
  await page.goto('/app?ref=frontend-test')
  await page.getByPlaceholder('you@work.com').fill(`browser-hub-${Date.now()}@example.com`)
  await page.getByRole('button', { name: 'Email me a sign-in code' }).click()
  const code = await page.getByText(/dev code \d{6}/).innerText()
  await page.getByPlaceholder('6-digit code').fill(code.match(/\d{6}/)![0])
  await page.getByRole('dialog', { name: 'Sign in' }).getByRole('button', { name: 'Sign in', exact: true }).click()
  await page.getByPlaceholder('Team name, e.g. Superdesign').fill('Hub test team')
  await page.getByRole('button', { name: 'Create team →', exact: true }).click()
  await expect(page.getByText('Which agent are you using?', { exact: true })).toBeVisible()
  await page.getByRole('link', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
}

async function hubOn(page: Page) {
  const json = (body: unknown) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  await page.route('**/hub/tools/mine', route => route.fulfill(json([tool])))
  await page.route('**/hub/tools/*/earnings*', route => route.fulfill(json(
    { tool_id: tool.tool_id, days: 90, earned_micro: 0, runs: 0, avg_price_micro: 0, by_day: [] })))
  await page.route('**/hub/tools/*/health', route => route.fulfill(json(health)))
  await page.route('**/hub/runs/*', route => route.fulfill(json(run)))
}

test('the Hub entry stays hidden when the hub does not answer for this team', async ({ page }) => {
  await signIn(page)
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  await expect(navigation.getByRole('button', { name: 'Activity', exact: true })).toBeVisible()
  await expect(navigation.getByRole('button', { name: 'Hub', exact: true })).toHaveCount(0)
})

test('the maker opens the Hub, every tab of a tool, and a run page', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await hubOn(page)
  await signIn(page)
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  await navigation.getByRole('button', { name: 'Hub', exact: true }).click()
  await expect(navigation.getByRole('button', { name: 'Hub', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page).toHaveURL(/#hub$/)
  await expect(page.getByRole('heading', { name: /^Hub - / })).toBeVisible()
  await page.getByText(tool.tool_id, { exact: true }).click()
  for (const tab of ['Versions', 'Price', 'Listing', 'Earnings', 'Runs & log', 'Health', 'Overview']) {
    await page.locator('.tabs').getByRole('button', { name: tab, exact: true }).click()
    await expect(page.locator('.tabs').getByRole('button', { name: tab, exact: true })).toHaveClass(/active/)
  }
  await page.reload()
  await expect(navigation.getByRole('button', { name: 'Hub', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.goto('/app/runs/' + run.run_id)
  await expect(page.getByRole('heading', { name: /^Run / })).toBeVisible()
  await expect(page.getByText(`${run.tool_id}@${run.version}`)).toBeVisible()
  await page.getByRole('button', { name: '← Hub' }).click()
  await expect(page).toHaveURL(/#hub$/)
  expect(errors).toEqual([])
})
