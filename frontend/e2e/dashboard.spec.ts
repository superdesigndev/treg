import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

test('sign in, switch pages, refresh and navigate back, asking for nothing that is not there', async ({ page }) => {
  const errors: string[] = [], missing: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => { if (response.status() === 404) missing.push(new URL(response.url()).pathname) })
  await signIn(page)
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  for (const name of ['Getting started', 'Catalog', 'Connections', 'Your own tools', 'Activity', 'Team']) {
    await navigation.getByRole('button', { name, exact: true }).click()
    await expect(navigation.getByRole('button', { name, exact: true })).toHaveAttribute('aria-current', 'page')
  }
  await page.reload()
  await expect(navigation.getByRole('button', { name: 'Team', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.goBack()
  await expect(navigation.getByRole('button', { name: 'Activity', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.goForward()
  await expect(navigation.getByRole('button', { name: 'Team', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.locator('.rd-account-menu summary').click()
  await page.locator('.rd-account-menu').getByRole('button', { name: 'Billing', exact: true }).click()
  await expect(page).toHaveURL(/#orgs\/billing$/)   // the Team page names its tab in the address
  const referral = page.getByRole('link', { name: 'Referral: Give $5, get $5, or become an affiliate partner', exact: true })
  await expect(referral).toHaveText('Referral')
  await referral.click()
  await expect(page).toHaveURL(/#referrals$/)
  // Every platform tile on the catalog asks for its logo.
  await page.goto('/catalog')
  await page.waitForLoadState('networkidle')
  expect(errors).toEqual([])
  expect(missing).toEqual([])
})

test('public catalog and shared deep links remain available without a session', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/catalog')
  await expect(page.locator('.pubnav')).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Start free', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Sign in' })).toBeVisible()
  await page.goto('/catalog/google')
  await expect(page.locator('.pl-hero')).toBeVisible()
  await page.reload()
  await expect(page.locator('.pl-hero')).toBeVisible()
  // A comparison is its own URL: it survives a reload, and the breadcrumb leads back to the shelf.
  await page.locator('.pl-cmp').first().click()
  await expect(page).toHaveURL(/\/catalog\/google\/[^/]+$/)
  await expect(page.getByRole('table').getByRole('row').nth(1)).toBeVisible()
  await page.reload()
  await expect(page.getByRole('table').getByRole('row').nth(1)).toBeVisible()
  await page.getByRole('table').getByRole('row').nth(1).click()
  await expect(page.getByRole('complementary', { name: 'Tool details' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('complementary', { name: 'Tool details' })).toHaveCount(0)
  await page.locator('.pl-crumbs').getByRole('link', { name: /Google/ }).click()
  await expect(page).toHaveURL(/\/catalog\/google$/)
  await page.goto('/app/tools/shared-example')
  await expect(page.getByRole('heading', { name: /shared-example/ })).toBeVisible()
  await expect(page.getByRole('dialog', { name: 'Sign in' })).toBeVisible()
  expect(errors).toEqual([])
})
