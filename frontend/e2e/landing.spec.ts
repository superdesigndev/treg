import { expect, test } from '@playwright/test'

test('landing email sign-in reaches the dashboard', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/')
  await page.getByRole('link', { name: 'Sign in', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Sign in', exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'GitHub' })).toHaveAttribute('href', '/auth/github')
  await dialog.getByPlaceholder('you@work.com').fill(`landing-${Date.now()}@example.com`)
  await dialog.getByPlaceholder('you@work.com').press('Enter')
  await expect(dialog.locator('#em-err')).toContainText('dev mode')
  await dialog.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByText('We made your team Example.', { exact: false })).toBeVisible()
})
