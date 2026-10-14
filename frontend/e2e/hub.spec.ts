import { expect, test } from '@playwright/test'
import { hubOn, hubRun as run, hubTool as tool, signIn } from './helpers'

// The hub is behind TREG_HUB_ENABLED (and TREG_HUB_TEAMS) on the server; the browser-test server
// runs with it off. So the first test proves the entry stays hidden there, and the second answers
// /meta and the hub routes here, with real shapes captured from a live run.

test('the Hub entry stays hidden when the hub is off, or not open to this team', async ({ page }) => {
  const hubRequests: string[] = []
  page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/hub/')) hubRequests.push(request.url()) })
  await signIn(page, 'browser-hub', 'Hub test team')
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  await expect(navigation.getByRole('button', { name: 'Activity', exact: true })).toBeVisible()
  await expect(navigation.getByRole('button', { name: 'Hub', exact: true })).toHaveCount(0)
  // Off on this server (/meta.hub false): nothing is asked of routes that could only answer 404.
  expect(hubRequests).toEqual([])
  // On, but not for this team: the probe answers 404 and the entry stays hidden.
  await page.route('**/meta', async route => route.fulfill({ json: { ...(await (await route.fetch()).json()), hub: true } }))
  await page.route('**/hub/tools/mine', route => route.fulfill({ status: 404, json: { detail: 'Not Found' } }))
  await page.reload()
  await expect(navigation.getByRole('button', { name: 'Activity', exact: true })).toBeVisible()
  await expect.poll(() => hubRequests.length).toBeGreaterThan(0)
  await expect(navigation.getByRole('button', { name: 'Hub', exact: true })).toHaveCount(0)
})

test('the maker opens the Hub, every tab of a tool, and a run page', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await hubOn(page)
  await signIn(page, 'browser-hub', 'Hub test team')
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
