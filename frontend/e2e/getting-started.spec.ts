import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

test('every Try it out card shows its banner', async ({ page }) => {
  await signIn(page, 'try-banners')
  const grid = page.locator('.rd-try .try-grid')
  await expect(grid.locator('.try-card').first()).toBeVisible()
  await expect(grid.locator('.rd-try-banner')).toHaveCount(await grid.locator('.try-card').count())
})
