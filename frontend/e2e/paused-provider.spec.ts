import { expect, test } from '@playwright/test'
import { json, signIn } from './helpers'

const GBP = 'google-business-profile'
const MESSAGE = 'Google Business Profile is temporarily paused on treg. Your existing connection is saved and starts to work again when it is resumed. No action is needed from you.'

// The shapes a deployment with TREG_PAUSED_PROVIDERS=google-business-profile answers with: the
// provider is out of the listing, and its kept connection is marked paused.
test('a paused provider loses its chip, and its connection shows paused instead of vanishing', async ({ page }) => {
  await page.route(url => url.pathname === '/oauth/providers', async route => {
    const listing = await (await route.fetch()).json()
    await route.fulfill(json(listing.filter((p: { service: string }) => p.service !== GBP)))
  })
  await page.route(url => url.pathname === '/connections', route => route.fulfill(json([{
    id: 41, name: GBP, kind: 'oauth', provider: GBP, authorization_method: 'default', resource_name: '',
    needs_reconnect: false, resource_ref: '', scopes: [], health: 'invalid', health_detail: 'HTTP 429',
    refreshable: true, expiry_state: 'ok', expires_at: null, last_refresh_at: null, last_error: '',
    owner: 'a@example.com', created_at: '2026-09-01T00:00:00', paused: true, paused_message: MESSAGE,
    provider_display_name: 'Google Business Profile',
  }])))
  await page.route(url => url.pathname === '/meta', async route => route.fulfill(json({
    ...(await (await route.fetch()).json()),
    paused_providers: { [GBP]: { display_name: 'Google Business Profile', message: MESSAGE } },
  })))
  await signIn(page)

  await page.goto('/app#connections')
  const card = page.locator('.cn-card').filter({ hasText: 'Google Business Profile' })
  await expect(card.locator('.cn-st')).toHaveText('Paused')
  await expect(card).toContainText(MESSAGE)
  await expect(card.getByRole('button', { name: 'Reconnect' })).toHaveCount(0)
  await expect(page.locator('#prov-' + GBP)).toHaveCount(0)

  // The provider page says why instead of rendering nothing; the kept connection is listed there.
  await page.goto('/app/marketplace/' + GBP)
  await expect(page.getByRole('heading', { name: 'Google Business Profile' })).toBeVisible()
  await expect(page.getByText(MESSAGE).first()).toBeVisible()
  await expect(page.getByRole('button', { name: /^(Connect( |$)|Add account)/ })).toHaveCount(0)

  // The catalog tile reads Paused; its shelf and every tool panel say why, with nothing to connect.
  await page.goto('/app#catalog')
  await expect(page.getByRole('button', { name: 'Open Google Business Profile' })).toContainText('Paused')
  await page.getByRole('button', { name: 'Open Google Business Profile' }).click()
  await expect(page.locator('.pl-hero .mk-notice')).toHaveText(MESSAGE)
  await page.locator('.pl-tool, .pl-card').filter({ hasText: 'your account' }).first().click()
  const drawer = page.getByRole('complementary', { name: 'Tool details' })
  await expect(drawer.getByText(MESSAGE)).toBeVisible()
  await expect(drawer.getByRole('button', { name: /^(Connect( |$)|Copy for your agent|Try it)/ })).toHaveCount(0)

  await page.goto('/app#start')
  const chips = page.locator('.prov-chip')
  await expect(chips.filter({ hasText: 'Search Console' })).toBeVisible()
  await expect(chips.filter({ hasText: 'Business Profile' })).toHaveCount(0)
})
