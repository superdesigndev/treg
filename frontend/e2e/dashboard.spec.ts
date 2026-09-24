import { expect, test, type Page } from '@playwright/test'

async function signIn(page: Page) {
  await page.goto('/app?ref=frontend-test')
  await page.getByPlaceholder('you@work.com').fill(`browser-${Date.now()}@example.com`)
  await page.getByRole('button', { name: 'Email me a sign-in code' }).click()
  const code = await page.getByText(/dev code \d{6}/).innerText()
  await page.getByPlaceholder('6-digit code').fill(code.match(/\d{6}/)![0])
  await page.getByRole('dialog', { name: 'Sign in' }).getByRole('button', { name: 'Sign in', exact: true }).click()
  await page.getByPlaceholder('Team name, e.g. Superdesign').fill('Browser test team')
  await page.getByRole('button', { name: 'Create team →', exact: true }).click()
  await expect(page.getByText('Which agent are you using?', { exact: true })).toBeVisible()
  await page.getByRole('link', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
}

test('sign in, create team, switch pages, refresh and navigate back', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await signIn(page)
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  for (const name of ['Catalog', 'Your own tools', 'Activity', 'Team']) {
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
  await expect(page).toHaveURL(/#orgs$/)
  expect(errors).toEqual([])
})

test('signed-in users can visit the homepage and return to the dashboard', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await signIn(page)
  await page.getByRole('link', { name: 'treg home' }).click()
  await expect(page).toHaveURL('http://127.0.0.1:18791/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('OpenRouter for agent tools')
  await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toHaveCount(0)
  await page.locator('.nav').getByRole('button', { name: 'Open dashboard' }).click()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
})

test('onboarding controls and images work on mobile and dark theme', async ({ page }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await signIn(page)
  await page.getByRole('button', { name: 'Getting started', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('.rd-start')).toBeVisible()
  const trigger = page.locator('[aria-controls="rd-agent-options"]')
  await trigger.click()
  await page.locator('#rd-agent-options').getByRole('button', { name: 'Codex', exact: true }).click()
  await expect(trigger).toContainText('Codex')
  await page.getByRole('button', { name: 'Show key', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Hide key', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Hide key', exact: true }).click()
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {
    configurable: true, value: { writeText: () => Promise.reject(new Error('denied')) },
  }))
  await page.locator('.rd-setup-panel').first().getByRole('button', { name: 'Copy', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Could not copy')
  await page.getByRole('button', { name: 'Dismiss', exact: true }).click()
  await page.locator('.rd-account-menu summary').click()
  await page.getByRole('button', { name: 'Dark appearance' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.waitForFunction(() => [...document.querySelectorAll<HTMLImageElement>('.rd-try .try-ico')].every(img => img.complete && img.naturalWidth > 0))
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-dark.png'), fullPage: true })
  expect(errors).toEqual([])
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
  await expect(page.locator('.plat-head')).toBeVisible()
  await page.reload()
  await expect(page.locator('.plat-head')).toBeVisible()
  await page.goto('/app/tools/shared-example')
  await expect(page.getByRole('heading', { name: /shared-example/ })).toBeVisible()
  await expect(page.getByRole('dialog', { name: 'Sign in' })).toBeVisible()
  expect(errors).toEqual([])
})

test('session initialization never flashes the old signed-out landing page', async ({ page }) => {
  await signIn(page)
  let releaseSession!: () => void
  const sessionGate = new Promise<void>(resolve => { releaseSession = resolve })
  await page.route('**/auth/me', async route => { await sessionGate; await route.continue() })
  await page.reload()
  await expect(page.getByRole('status')).toHaveText('Loading treg…')
  await expect(page.getByText('Sign in to treg', { exact: true })).toHaveCount(0)
  releaseSession()
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
})

test('mainline team resources survive navigation and open the voice tools', async ({ page }) => {
  await signIn(page)
  await page.route('**/provider-resources?source=platform', route => route.fulfill({
    json: [{ id: 1, provider: 'fishaudio', kind: 'voice', upstream_id: 'test-private-voice', display_name: 'Test voice', status: 'active' }],
  }))
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Your own tools', exact: true }).click()
  await page.getByRole('button', { name: 'Team resources', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Team resources', exact: true })).toBeVisible()
  await expect(page.getByText('Test voice', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Team resources', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Rename', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Rename voice', exact: true })
  await expect(dialog.getByRole('textbox')).toHaveValue('Test voice')
  await expect(dialog.getByRole('textbox')).toBeFocused()
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('button', { name: 'Use in TTS', exact: true }).click()
  // The disposable server has no provider credentials; verify the prepared request
  // through the API tab, which is available without enabling paid execution.
  const drawer = page.getByRole('dialog').filter({ hasText: 'Try “fishaudio.tts.s2-1-pro”' })
  await drawer.getByRole('button', { name: 'API', exact: true }).click()
  await expect(drawer.locator('pre')).toContainText('"reference_id": "test-private-voice"')
})

// /catalog/find needs a relevance judge the disposable server does not have, so the stream is
// mocked here: the browser tests pin the pages' handling of the two events, not the judge.
async function mockFind(page: Page, rows: object[], verdict = 'strong') {
  await page.route('**/catalog/find?**', route => route.fulfill({
    contentType: 'application/x-ndjson',
    body: JSON.stringify({ event: 'candidates', candidates: [
      { id: 'google-search-console.performance', platform: 'search-console' },
      { id: 'reddit.search', platform: 'reddit' }] }) + '\n'
      + JSON.stringify({ event: 'judged', verdict, read: 2, high: 0.7, rows }) + '\n',
  }))
}
const consoleRow = { id: 'google-search-console.performance', name: 'Search performance',
  provider: 'google-search-console', provider_display: 'Google Search Console', platform: 'search-console',
  platform_label: 'Google Search Console', capability: 'search-console.performance',
  capability_description: 'Clicks, impressions, CTR & top queries', cost: { type: 'free' }, p: 0.84 }

test('the catalog search box answers a described job and lights the shelves', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await signIn(page)
  await mockFind(page, [consoleRow])
  await page.goto('/app#connections')
  const box = page.getByRole('textbox', { name: 'Search' })
  await box.fill('tiktok')
  await expect(page.getByRole('button', { name: /Search all tools for/ })).toBeVisible()   // a name filters, Enter still searches
  await box.fill('why is my blog losing google traffic')
  await expect(page.getByRole('button', { name: /Find tools for/ })).toBeVisible()   // a job, not a name
  await expect(page.getByText(/No catalogued platforms/)).toHaveCount(0)
  await box.press('Enter')
  await expect(page.getByText('Clicks, impressions, CTR & top queries')).toBeVisible()
  await expect(page.getByText(/1 tool for/)).toBeVisible()
  await expect(page.locator('.pt-card.find-hit')).toHaveCount(1)
  await page.getByRole('button', { name: 'Clear the search' }).click()
  await expect(page.locator('.pt-card.find-hit')).toHaveCount(0)
  expect(errors).toEqual([])
})

test('the public search page lands the fitting platforms in their cards', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await mockFind(page, [consoleRow])
  await page.goto('/search')
  await expect(page.getByRole('heading', { name: /What does your agent/ })).toBeVisible()
  await expect(page.locator('.sp-tile').first()).toBeVisible()
  await page.getByLabel('Describe the job').press('Enter')                          // empty: the placeholder is the query
  await expect(page.getByLabel('Describe the job')).toHaveValue(/Find the emails of CTOs/)
  await expect(page).toHaveURL(/\/search\?q=Find/)
  await page.getByLabel('Describe the job').fill('why is my blog losing google traffic')
  await page.getByLabel('Describe the job').press('Enter')
  await expect(page.getByRole('button', { name: 'Google Search Console' })).toBeVisible()
  await expect(page).toHaveURL(/\/search\?q=why/)
  await expect(page.locator('.sp-tile.landed')).toHaveCount(1)
  await page.getByRole('button', { name: 'Google Search Console' }).click()          // signed out: sign in first
  await expect(page.getByRole('dialog', { name: 'Sign in' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Copy for your agent' })).toBeVisible()
  await mockFind(page, [], 'none')
  await page.getByLabel('Describe the job').fill('wire money to my landlord')
  await page.getByLabel('Describe the job').press('Enter')
  await expect(page.getByText('Nothing in the catalog does this yet.')).toBeVisible()
  expect(errors).toEqual([])
})
