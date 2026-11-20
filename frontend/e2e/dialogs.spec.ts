import { expect, test, type Locator, type Page } from '@playwright/test'
import { billingOn, openTopUp, signIn } from './helpers'

// Every dialog: it has an accessible name, focus moves in when it opens, Tab and Shift+Tab stay inside, Escape closes it,
// and focus returns to the control that opened it.

const focusInside = (dialog: Locator) => dialog.evaluate(el => el.contains(document.activeElement))

async function expectTrapped(page: Page, dialog: Locator) {
  await expect(dialog).toHaveAccessibleName(/\S/)
  expect(await focusInside(dialog)).toBe(true)
  // Focus starts on a field or the dialog itself, never on its close button.
  await expect(dialog.getByRole('button', { name: 'Close' })).not.toBeFocused()
  for (const key of ['Tab', 'Shift+Tab']) {
    for (let i = 0; i < 20; i++) {
      await page.keyboard.press(key)
      expect(await focusInside(dialog), `${key} #${i + 1}`).toBe(true)
    }
  }
}

test('the top-up dialog takes focus, keeps it, and gives it back on Escape', async ({ page }) => {
  await billingOn(page)
  await signIn(page, 'dialog-topup')
  await openTopUp(page)
  const dialog = page.getByRole('dialog')
  await expectTrapped(page, dialog)
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Top up', exact: true })).toBeFocused()
})

test('catalog dialogs close on Escape and return focus to their trigger', async ({ page }) => {
  await signIn(page, 'dialog-catalog')
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Catalog', exact: true }).click()
  for (const trigger of ['List as vendor', 'Request a tool']) {
    const button = page.getByRole('button', { name: trigger, exact: true })
    await button.click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expectTrapped(page, dialog)
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(button).toBeFocused()
  }
})

test('the new-team dialog starts in its name field and closes on Escape', async ({ page }) => {
  await signIn(page, 'dialog-team')
  await page.getByRole('button', { name: 'Teams' }).click()
  await page.getByRole('button', { name: '＋ New team' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByPlaceholder('Team name, e.g. Superdesign')).toBeFocused()
  await expectTrapped(page, dialog)
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
})

test('the sign-in dialog closes on Escape', async ({ page }) => {
  await page.goto('/catalog')
  const start = page.getByRole('button', { name: 'Start free', exact: true })
  await start.click()
  const dialog = page.getByRole('dialog', { name: 'Sign in' })
  await expect(dialog).toBeVisible()
  await expectTrapped(page, dialog)
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(start).toBeFocused()
})
