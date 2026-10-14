import { expect, test } from '@playwright/test'
import { json, signIn } from './helpers'

test('sign in, create team, switch pages, refresh and navigate back', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await signIn(page)
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  for (const name of ['Catalog', 'Connections', 'Your own tools', 'Activity', 'Team']) {
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
  const referral = page.getByRole('link', { name: 'Referral: Give $5, get $5, or become an affiliate partner', exact: true })
  await expect(referral).toHaveText('Referral')
  await referral.click()
  await expect(page).toHaveURL(/#referrals$/)
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

test('the dashboard and catalog ask for nothing that is not there', async ({ page }) => {
  const missing: string[] = []
  page.on('response', response => { if (response.status() === 404) missing.push(new URL(response.url()).pathname) })
  await signIn(page, 'no-404')
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  for (const name of ['Catalog', 'Your own tools', 'Activity', 'Team', 'Getting started']) {
    await navigation.getByRole('button', { name, exact: true }).click()
    await expect(navigation.getByRole('button', { name, exact: true })).toHaveAttribute('aria-current', 'page')
  }
  // Every platform tile on the catalog asks for its logo.
  await page.goto('/catalog')
  await page.waitForLoadState('networkidle')
  expect(missing).toEqual([])
})

test('Help renders the shared tutorials, which no other view downloads', async ({ page }) => {
  const scripts: string[] = []
  page.on('request', request => { if (request.resourceType() === 'script') scripts.push(new URL(request.url()).pathname) })
  await signIn(page, 'help')
  await page.waitForLoadState('networkidle')
  expect(scripts).not.toContain('/tutorial.js')
  await page.goto('/app#help')
  await expect(page.getByText(/The whole registry from your terminal.* [1-9]\d* steps\./)).toBeVisible()
  await page.getByRole('heading', { name: '▤ CLI tutorial' }).click()
  await expect(page.locator('.explain').first()).not.toBeEmpty()
})

test('a dialog takes its first field and hands focus back, even when its code arrives late', async ({ page }) => {
  await signIn(page, 'late-dialog')
  // Hold the dialog's code so it mounts well after the click that opened it.
  await page.route(/RequestToolDialog-[^/]*\.js$/, async route => {
    await new Promise(resolve => setTimeout(resolve, 1500))
    await route.continue()
  })
  await page.goto('about:blank')
  await page.goto('/app#catalog')
  const opener = page.getByRole('button', { name: 'Request a tool', exact: true })
  await opener.click()
  const dialog = page.getByRole('dialog', { name: 'Request a tool' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByPlaceholder('e.g. Ahrefs backlinks, flight prices, HN comments')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(opener).toBeFocused()
})

test('a routed tool can be tried by hand, and says who serves it in one line', async ({ page }) => {
  await signIn(page, 'routed-try')
  // The browser-test server holds no platform keys; answer the access dry-run the way a deployment
  // with two providers keyed would.
  await page.route('**/catalog/endpoints/treg.companies.enrich/access', route => route.fulfill(json({
    tier: 'routed', detail: 'routed',
    plan: [{ endpoint_id: 'dropleads.companies.enrich', provider: 'dropleads', tier: 'platform' },
           { endpoint_id: 'hunter.companies.enrich', provider: 'hunter', tier: 'platform' }],
    dropped: [{ endpoint_id: 'apollo.companies.enrich', why: 'no apollo key on this deployment and no own key' }],
  })))
  await page.goto('/app#platform/companies/enrich')
  await page.locator('.pl-autocard').getByRole('button', { name: 'Try it', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: /treg\.companies\.enrich/ })
  await expect(drawer.getByText(/One call: 2 providers can serve it for this team now/)).toBeVisible()
  await drawer.getByRole('button', { name: 'Manual', exact: true }).click()
  await expect(drawer.getByRole('button', { name: /Run/ })).toBeVisible()
  await expect(drawer.getByText(/needs a key/)).toHaveCount(0)
})

test("a tool drawer's primary button is the next step to an agent using the tool", async ({ page }) => {
  const drawer = page.getByRole('complementary', { name: 'Tool details' })
  const primary = () => drawer.locator('.td-act .pl-btn:not(.ghost)').first()
  await page.goto('/catalog/companies/enrich')
  await page.getByRole('table').getByRole('row').filter({ hasText: 'Crustdata' }).first().click()
  await expect(primary()).toHaveText('Copy for your agent')        // treg's key serves it: hand it over
  await expect(drawer.getByRole('button', { name: 'Try it' })).toHaveClass(/ghost/)
  await page.getByRole('table').getByRole('row').filter({ hasText: 'Ocean' }).first().click()
  await expect(primary()).toHaveText(/^Add your .+ key$/)          // only your own key can call it
  await page.goto('/catalog/google-analytics')
  await page.locator('button.pl-job').first().click()
  await expect(primary()).toHaveText(/^Connect /)                  // an account to connect first
  await expect(drawer.locator('.td-stats')).toContainText('your account')
})

test('a provider key saved as a secret is a connection: listed on Connections, not among Secrets', async ({ page }) => {
  await signIn(page, 'named-key')
  await page.goto('/app#secrets')
  await page.getByPlaceholder('name, e.g. STRIPE_KEY').fill('apollo')
  await page.getByPlaceholder('value (encrypted server-side)').fill('not-a-real-key')
  await page.getByRole('button', { name: 'Add secret', exact: true }).click()
  await expect(page.getByText('Keys for catalog providers live on')).toContainText('(1 there now)')
  await expect(page.locator('.ttable')).toHaveCount(0)
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Connections' }).click()
  const card = page.locator('.cn-card').filter({ hasText: 'Apollo.io' })
  await expect(card).toContainText('saved as a secret')
  await card.getByRole('button', { name: 'Remove', exact: true }).click()
  await card.getByRole('button', { name: 'Click again to remove', exact: true }).click()
  await expect(page.locator('.cn-card')).toHaveCount(0)
})

test('Bring your own key lands on Connections, the provider it names in view', async ({ page }) => {
  await signIn(page, 'byok-jump')
  await page.goto('/catalog/companies/enrich')
  await page.getByRole('table').getByRole('row').filter({ hasText: 'Ocean' }).first().click()
  await page.getByRole('complementary', { name: 'Tool details' }).getByRole('button', { name: /^Add your .+ key$/ }).click()
  await expect(page).toHaveURL(/#connections$/)
  await expect(page.locator('.cn-prov.on')).toBeInViewport()
})

test('Activity pages the merged feed with the cursor the server returns', async ({ page }) => {
  const at = (minutes: number) => new Date(Date.UTC(2026, 8, 30, 12) - minutes * 60_000).toISOString().slice(0, 23)
  const call = (i: number) => ({ source: 'call', id: 5000 - i, kind: 'http', tool_name: 'serp', method: 'GET', path: '/search',
    status_code: 200, user_email: 'a@example.com', client: 'cli', credential_tier: 'platform', cost_charged_micro: 1000, created_at: at(i) })
  const run = (i: number) => ({ source: 'run', id: 'l' + i, where: 'local', tool: 'gh', argv: ['pr', 'list'], exit_code: null,
    user_email: 'a@example.com', client: 'cli', created_at: at(i + 0.5) })
  const rows = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => from + i).flatMap(i => [call(i), run(i)])
  const asked: string[] = []
  await page.route(url => url.pathname === '/activity', route => {
    const before = new URL(route.request().url()).searchParams.get('before') ?? ''
    asked.push(before)
    route.fulfill(json(before ? { rows: rows(50, 60), next: null } : { rows: rows(0, 50), next: 'cursor~l~49' }))
  })
  await signIn(page)
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Activity', exact: true }).click()
  const all = page.getByRole('radio', { name: /^All/ })
  await expect(all).toHaveText('All 100')
  await page.getByRole('button', { name: 'Load older activity' }).click()
  await expect(all).toHaveText('All 120')
  await expect(page.getByRole('button', { name: 'Load older activity' })).toHaveCount(0)
  expect(asked).toEqual(['', 'cursor~l~49'])
})

test('Activity offers no older page while a new filter is loading', async ({ page }) => {
  // The old cursor belongs to the rows being replaced: an older page fetched with it would mix two
  // filters, and would supersede the reload.
  const at = (minutes: number) => new Date(Date.UTC(2026, 8, 30, 12) - minutes * 60_000).toISOString().slice(0, 23)
  const row = (i: number) => ({ source: 'call', id: 5000 - i, kind: 'http', tool_name: 'serp', method: 'GET', path: '/search',
    status_code: 200, user_email: 'a@example.com', client: 'cli', credential_tier: 'platform', cost_charged_micro: 1000, created_at: at(i) })
  const rows = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => row(from + i))
  let release = () => {}
  const filtered = new Promise<void>(resolve => { release = resolve })
  await page.route(url => url.pathname === '/activity', async route => {
    const q = new URL(route.request().url()).searchParams
    if (q.get('api_key_id')) { await filtered; return route.fulfill(json({ rows: rows(200, 203), next: null })) }
    if (q.get('before')) return route.fulfill(json({ rows: rows(100, 150), next: null }))
    return route.fulfill(json({ rows: rows(0, 100), next: 'cursor~c~4900' }))
  })
  await page.route(url => url.pathname.endsWith('/api-keys'), route => route.fulfill(json([
    { id: 7, name: 'ci', identity: 'bot', assigned_type: 'agent', prefix: 'tr_x' }])))
  await signIn(page)
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Activity', exact: true }).click()
  const all = page.getByRole('radio', { name: /^All/ })
  await expect(all).toHaveText('All 100')
  await page.getByRole('combobox', { name: 'API key' }).selectOption('7')
  await expect(page.getByRole('button', { name: 'Load older activity' })).toHaveCount(0)
  release()
  await expect(all).toHaveText('All 3')
})
