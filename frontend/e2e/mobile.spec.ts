import { expect, test, type Page } from '@playwright/test'
import { billingOn, fitsWidth, hubOn, hubTool, openTopUp, signIn, textCollisions } from './helpers'

// At phone width the page itself never scrolls sideways: wide tables and code scroll inside
// themselves, and everything else wraps.
test.use({ viewport: { width: 390, height: 844 } })

test('the top-up dialog shows every amount on a phone', async ({ page }) => {
  await billingOn(page)
  await signIn(page, 'phone-topup')
  await openTopUp(page)
  const dialog = page.getByRole('dialog')
  for (const name of ['$10', '$50', '$100', '$200', 'Other']) {
    // A preset's name carries its bonus ("$50 +$2.50 bonus"); match on the amount it starts with.
    const amount = new RegExp('^' + name.replace('$', '\\$') + '(\\s|$)')
    const box = (await dialog.getByRole('button', { name: amount }).boundingBox())!
    expect(box.x, name).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width, name).toBeLessThanOrEqual(390)
  }
  expect(await fitsWidth(page)).toBe(true)
})

test('a hub tool page does not scroll sideways on a phone', async ({ page }) => {
  await hubOn(page)
  await signIn(page, 'phone-hub')
  await page.goto('/app#hub')
  await page.getByText(hubTool.tool_id, { exact: true }).click()
  await expect(page.getByText(hubTool.uses[0]!, { exact: true })).toBeVisible()
  expect(await fitsWidth(page)).toBe(true)
})

test('a comparison keeps provider prices clear of each other on a phone', async ({ page }) => {
  await page.goto('/catalog/people')
  await expect(page.locator('.pl-cmp').first()).toBeVisible()
  expect(await fitsWidth(page)).toBe(true)
  await page.locator('.pl-cmp').first().click()
  await expect(page.getByRole('table').first()).toBeVisible()
  expect(await fitsWidth(page)).toBe(true)
  // No two pieces of text in one comparison row paint over each other (helpers.textCollisions).
  expect(await textCollisions(page, 'tr')).toEqual([])
})
