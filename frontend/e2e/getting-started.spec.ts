import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

test('Getting started says when the agent has connected', async ({ page }) => {
  await signIn(page, 'first-call')
  await page.goto('/app#start')
  const status = page.locator('.rd-agent-status')
  await expect(status).toContainText('Waiting for your agent…')
  const checkedIn = await page.evaluate(() => fetch('/orgs', { credentials: 'include' }).then(r => r.json())
    .then(([org]) => fetch('/agents/checkin', { method: 'POST', credentials: 'include', headers: { 'x-treg-org': org.slug } }))
    .then(r => r.ok))
  expect(checkedIn).toBe(true)
  await expect(status).toContainText('Connected', { timeout: 10000 })
})

test('every Try it out card shows its banner', async ({ page }) => {
  await signIn(page, 'try-banners')
  const grid = page.locator('.rd-try .try-grid')
  await expect(grid.locator('.try-card').first()).toBeVisible()
  await expect(grid.locator('.rd-try-banner')).toHaveCount(await grid.locator('.try-card').count())
})
