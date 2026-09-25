import { expect, test } from '@playwright/test'

// Enable BFCache for history regression coverage.
test.use({ launchOptions: { ignoreDefaultArgs: ['--disable-back-forward-cache'] } })

test.describe('browser history', () => {
  test('restores the 3D scene and catalog scrolling from the back/forward cache', async ({ page }) => {
    // Software WebGL on CI renders the scene much more slowly than a desktop GPU.
    test.setTimeout(120000)
    // Allow BFCache on local HTTP; production cache headers stay unchanged.
    await page.route('http://127.0.0.1:18791/', async route => {
      const response = await route.fetch()
      await route.fulfill({ response, headers: { ...response.headers(), 'cache-control': 'private, no-cache' } })
    })
    await page.goto('/')
    await page.unrouteAll()
    await expect(page.locator('.gateway-sculpture')).toHaveAttribute('data-model-state', 'ready', { timeout: 20000 })
    await page.keyboard.press('Escape')
    await page.evaluate(() => {
      window.addEventListener('pageshow', event => {
        document.documentElement.dataset.historyRestored = String(event.persisted)
      })
    })
    for (let cycle = 0; cycle < 2; cycle++) {
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
      await page.locator('.nav').getByRole('link', { name: 'Catalog', exact: true }).click()
      await expect(page).toHaveURL(/\/catalog$/)
      await page.evaluate(() => history.back())
      await expect(page.locator('html')).toHaveAttribute('data-history-restored', 'true')
      await expect(page.locator('.gateway-webgl')).toBeVisible()
      await expect(page.locator('.gateway-webgl')).toHaveCount(1)
      await expect(page.locator('.hero-particles')).toHaveCount(1)
      await expect(page.locator('.command-beam')).toHaveCount(1)
      await page.evaluate(() => window.scrollTo({ top: document.querySelector<HTMLElement>('#catalog')!.offsetTop + 200, behavior: 'instant' }))
      const track = page.locator('#catalog .catwrap')
      await expect.poll(() => track.evaluate(el => getComputedStyle(el).transform)).not.toBe('none')
      const before = await track.evaluate(el => getComputedStyle(el).transform)
      await page.mouse.wheel(0, 450)
      await expect.poll(() => track.evaluate(el => getComputedStyle(el).transform)).not.toBe(before)
    }
  })
})

test('the hero does not flash a placeholder while the 3D module loads', async ({ page }) => {
  let release!: () => void
  const loading = new Promise<void>(resolve => { release = resolve })
  await page.route('**/media/landing/gateway-3d.js', async route => {
    await loading
    await route.continue()
  })
  try {
    await page.goto('/', { waitUntil: 'commit' })
    await expect(page.locator('.gateway-sculpture')).toBeAttached()
    await expect(page.locator('.hcore')).toBeHidden()
  } finally {
    release()
  }
  await expect(page.locator('.gateway-sculpture')).toHaveAttribute('data-model-state', 'ready', { timeout: 20000 })
})

test('landing renders its CDN-backed 3D scene', async ({ page }) => {
  test.setTimeout(120000)
  const errors: string[] = []
  const failedAssets: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => {
    if ((response.url().includes('/media/landing/') || response.url().includes('cdn.jsdelivr.net/npm/')) && !response.ok()) failedAssets.push(response.url())
  })
  await page.goto('/')
  await expect(page.locator('.gateway-sculpture')).toHaveAttribute('data-model-state', 'ready', { timeout: 20000 })
  await expect(page.locator('.gateway-webgl')).toBeVisible()
  expect(failedAssets).toEqual([])
  expect(errors).toEqual([])
})

test('landing copies the serving-origin setup command and changes agent scenario', async ({ page, context }) => {
  // Keep the interaction contract independent of software WebGL's continuous render loop.
  // The preceding test covers the real CDN-backed scene with motion enabled.
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/')
  await page.bringToFront()
  await expect(page.locator('html')).not.toHaveClass(/opening-stage/)
  // Record the short-lived announcement in the page, so a busy software WebGL renderer
  // cannot make the test runner miss it between protocol round trips.
  await page.getByRole('status').evaluate(status => {
    new MutationObserver(() => {
      if (status.textContent === 'Copied') status.setAttribute('data-copy-announced', 'true')
    }).observe(status, { childList: true, characterData: true, subtree: true })
  })
  await page.getByRole('button', { name: 'Copy agent command', exact: true }).click()
  await expect(page.getByRole('status')).toHaveAttribute('data-copy-announced', 'true')
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('set up treg - http://127.0.0.1:18791/llms.txt')
  await page.getByRole('button', { name: 'Next agent scenario' }).click()
  await expect(page.locator('#sc-tools button')).toHaveCount(6)
})

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

test('mobile reduced-motion landing remains usable when WebGL is unavailable', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(() => {
    const getContext = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: string, ...args: unknown[]) {
      if (type.includes('webgl')) return null
      return getContext.apply(this, [type, ...args] as Parameters<typeof getContext>)
    } as typeof getContext
  })
  await page.goto('/')
  await expect(page.locator('.gateway-sculpture')).toHaveAttribute('data-model-state', 'fallback')
  await expect(page.locator('.hcore')).toBeVisible()
  await expect(page.locator('html')).not.toHaveClass(/opening-stage/)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {
    configurable: true, value: { writeText: () => Promise.reject(new Error('denied')) },
  }))
  await page.getByRole('button', { name: 'Copy agent command', exact: true }).click()
  await expect(page.getByRole('status')).toHaveText('Copy failed, please retry')
  await page.locator('footer').scrollIntoViewIfNeeded()
  await expect(page.getByRole('link', { name: 'Enrich Arena' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('landing-mobile-fallback.png'), fullPage: true })
})


test('landing keeps native scrolling and sign-in when the library CDN is unavailable', async ({ page }) => {
  await page.route('https://cdn.jsdelivr.net/npm/**', route => route.abort('failed'))
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('.gateway-sculpture')).toHaveAttribute('data-model-state', 'fallback')
  await expect(page.locator('.hcore')).toBeVisible()
  await expect(page.locator('html')).not.toHaveClass(/opening-stage/)
  await page.mouse.wheel(0, 450)
  await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(0)
  await page.getByRole('link', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Sign in', exact: true })).toBeVisible()
})
