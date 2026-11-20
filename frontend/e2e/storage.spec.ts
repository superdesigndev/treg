import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// Safari with site data blocked throws on the first touch of localStorage. The dashboard keeps
// only conveniences there, so it must work, just without remembering them.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() { throw new DOMException('The operation is insecure.', 'SecurityError') },
    })
  })
})

test('the dashboard works when browser storage is blocked', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await signIn(page, 'no-storage')
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  for (const name of ['Catalog', 'Your own tools', 'Team']) {
    await navigation.getByRole('button', { name, exact: true }).click()
    await expect(navigation.getByRole('button', { name, exact: true })).toHaveAttribute('aria-current', 'page')
  }
  await page.reload()
  await expect(navigation.getByRole('button', { name: 'Team', exact: true })).toHaveAttribute('aria-current', 'page')
  expect(errors).toEqual([])
})

test('the public catalog works when browser storage is blocked', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/catalog/google')
  await expect(page.locator('.pl-cmp').first()).toBeVisible()
  await page.locator('.pl-cmp').first().click()
  await expect(page.getByRole('table').first()).toBeVisible()
  expect(errors).toEqual([])
})
