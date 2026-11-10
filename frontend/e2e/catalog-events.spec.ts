import { expect, test, type Page } from '@playwright/test'
import { stubPostHog } from './helpers'

const ph = (page: Page) => page.evaluate(() => (window as any).__ph)

test('a shelf, a comparison, a tool and an action each send their catalog event', async ({ page }) => {
  await stubPostHog(page)
  await page.goto('/catalog/google')
  await expect(page.locator('.pl-hero')).toBeVisible()
  await page.locator('.pl-cmp').first().click()
  await page.getByRole('table').getByRole('row').nth(1).click()
  await page.getByRole('complementary', { name: 'Tool details' }).getByRole('button', { name: 'Copy' }).click()
  const events = (await ph(page)).events
  expect(events.map(([n]: [string]) => n)).toEqual(['catalog_platform_viewed', 'catalog_comparison_viewed', 'catalog_tool_opened', 'catalog_action'])
  expect(events.at(-1)[1]).toEqual(expect.objectContaining({ action: 'copy', surface: 'comparison', platform: 'google', signed_in: false }))
})
