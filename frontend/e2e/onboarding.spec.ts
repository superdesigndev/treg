import { expect, test } from '@playwright/test'

// The first-run flow on the browser-test server (no lookup keys, no provider credit): every sheet
// still opens, moves on, and ends on the dashboard.
test('a new account goes from setup to a first call to the dashboard', async ({ page }) => {
  // signed in the way e2e/helpers.ts signIn does, without its team-name modal step
  await page.goto('/app?ref=frontend-test')
  await page.getByPlaceholder('you@work.com').fill(`new-${Date.now()}@onboarding.test`)
  await page.getByRole('button', { name: 'Email me a sign-in code' }).click()
  const code = await page.getByText(/dev code \d{6}/).innerText()
  await page.getByPlaceholder('6-digit code').fill(code.match(/\d{6}/)![0])
  await page.getByRole('dialog', { name: 'Sign in' }).getByRole('button', { name: 'Sign in', exact: true }).click()

  const flow = page.getByRole('dialog', { name: 'Set up treg' })
  const setup = flow.getByRole('heading', { name: "Here's what we found" })
  await expect(setup).toBeVisible({ timeout: 30000 })
  await flow.getByRole('button', { name: /Continue/ }).click({ timeout: 45000 })

  const tasks = flow.getByRole('heading', { name: 'A task treg could help your agent with' })
  await expect(tasks).toBeVisible()
  await expect(setup).toBeHidden()
  await flow.getByRole('radio').first().click()
  await flow.getByRole('button', { name: /Continue/ }).click()

  await expect(flow.getByRole('heading', { name: "Try your agent's first call" })).toBeVisible()
  await expect(tasks).toBeHidden()
  await expect(flow.getByText(/treg call treg\./)).toBeVisible()

  await flow.getByRole('button', { name: /Open the dashboard/ }).click()
  await expect(flow).toBeHidden()
  await page.reload()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
  await expect(page.getByRole('dialog', { name: 'Set up treg' })).toBeHidden()
})
