import { expect, test } from '@playwright/test'

test('landing email sign-in reaches the dashboard', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/')
  await page.getByRole('link', { name: 'Sign in', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Sign in', exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'Continue with GitHub' })).toHaveAttribute('href', '/auth/github')
  await dialog.getByPlaceholder('you@work.com').fill(`landing-${Date.now()}@example.com`)
  await dialog.getByRole('button', { name: 'Email me a sign-in code' }).click()
  await expect(dialog.locator('#em-err')).toContainText('dev mode')
  await dialog.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByPlaceholder('Team name, e.g. Superdesign')).toBeVisible()
})

test('landing stays usable at phone width without WebGL or the library CDN', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(() => {
    const getContext = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: string, ...args: unknown[]) {
      if (type.includes('webgl')) return null
      return getContext.apply(this, [type, ...args] as Parameters<typeof getContext>)
    } as typeof getContext
  })
  await page.route('https://cdn.jsdelivr.net/npm/**', route => route.abort('failed'))
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('.gateway-sculpture')).toHaveAttribute('data-model-state', 'fallback')
  await expect(page.locator('.hcore')).toBeVisible()
  await expect(page.locator('html')).not.toHaveClass(/opening-stage/)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {
    configurable: true, value: { writeText: () => Promise.reject(new Error('denied')) },
  }))
  await page.getByRole('button', { name: 'Copy agent command', exact: true }).click()
  await expect(page.getByRole('status')).toHaveText('Copy failed, please retry')
  await page.mouse.wheel(0, 450)
  await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(0)
  await page.locator('footer').scrollIntoViewIfNeeded()
  await expect(page.getByRole('link', { name: 'Enrich Arena' })).toBeVisible()
  await page.getByRole('link', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Sign in', exact: true })).toBeVisible()
})
