import { expect, test } from '@playwright/test'

const MARKUP = '<img src=x onerror="window.__ran=1">'
const QUESTION = `GET https://api.example.com/v1/items/${MARKUP}`
const ENDPOINT = `example.items${MARKUP}`

const report = {
  mode: 'serve', worker_on: false, refresh_daily_cap: 10, keys: 1, snapshots: 1, kept_bytes: 10,
  bodies_kept: 1, hits_today: 0, refreshes_today: 0,
  endpoints: [{ endpoint_id: ENDPOINT, provider: MARKUP, policy: 'transient', change_ratio: null,
    keys: 1, refetches: 0, hits: 0, kept_bytes: 10 }],
}
const keys = {
  endpoint_id: ENDPOINT,
  keys: [{ question: QUESTION, key_hash: MARKUP, ttl_s: 60, fetched_at: '2026-01-01T00:00:00',
    stable: 0, changed: 0, last_requested_at: null,
    versions: [{ version: 1, origin: MARKUP, stored: 'body', size_bytes: 10 }] }],
  events: [{ at: '2026-01-01T00:00:00', cached: false, status: MARKUP, charged_micro: null }],
}

test('archive panel renders stored values as text and keeps the token for the tab only', async ({ page }) => {
  await page.route(url => url.pathname.startsWith('/admin/archive') && url.pathname !== '/admin/archive/panel',
    route => {
      const { pathname } = new URL(route.request().url())
      return route.fulfill({ json: pathname === '/admin/archive/keys' ? keys : report })
    })
  await page.goto('/admin/archive/panel')
  await page.locator('#tok').fill('admin-token')
  await page.locator('#go').click()

  const row = page.locator('#eptable tr[data-ep]')
  await expect(row).toHaveAttribute('data-ep', ENDPOINT)
  await expect(row.locator('.prov')).toHaveText(MARKUP)
  await row.click()
  await expect(page.locator('#i-body .mono').first()).toHaveText(QUESTION)
  await expect(page.locator('#i-body .v')).toHaveAttribute('data-key', MARKUP)
  await expect(page.locator('#feed .w')).toHaveText(`HTTP ${MARKUP}`)

  expect(await page.locator('#eptable img, #i-body img, #feed img').count()).toBe(0)
  expect(await page.evaluate(() => (window as any).__ran)).toBeUndefined()
  expect(await page.evaluate(() => [sessionStorage.getItem('treg_admin_token'), localStorage.getItem('treg_admin_token')]))
    .toEqual(['admin-token', null])
})
