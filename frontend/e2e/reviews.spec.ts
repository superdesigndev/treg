import { expect, test, type Page } from '@playwright/test'
import { fitsWidth, json, textCollisions } from './helpers'

// The browser-test server has no reviews; answer the shelf with two rated providers of one job: one
// scored (five teams, a share) and one early (fewer teams, quotes only), the rest unrated.
const SCORED = { teams: 5, share: { useful: 0.8, partly: 0.2, not_useful: 0 },
  samples: [{ usefulness: 'useful', reason: 'Returned the firmographics the task needed on the first call.', client: 'codex', month: '2026-09' }] }
const EARLY = { teams: 0,
  samples: [{ usefulness: 'partly', reason: 'Found the company but the headcount was a stale figure.', client: 'claude-code', month: '2026-09' }] }

async function withReviews(page: Page) {
  await page.route('**/catalog/platforms/companies?*', async route => {
    const body = await (await route.fetch()).json()
    const cap = body.capabilities.find((c: { id: string }) => c.id === 'companies.enrich')
    const [scored, early] = cap.endpoints.filter((e: { kind?: string }) => e.kind !== 'routed').map((e: { id: string }) => e.id)
    const patch = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(patch)
      else if (node && typeof node === 'object') {
        const o = node as Record<string, unknown>
        if (o.id === scored) o.reviews = SCORED
        if (o.id === early) o.reviews = EARLY
        Object.values(o).forEach(patch)
      }
    }
    patch(body)
    await route.fulfill(json(body))
  })
}

for (const [width, height] of [[1440, 1000], [390, 844]] as const) {
  test(`early reviews show as quotes without a score, below the scored rows, at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height })
    await withReviews(page)
    await page.goto('/catalog/companies/enrich')
    const rows = page.locator('.ui-data-table .ui-tbody .ui-tr')
    await expect(rows.first()).toBeVisible()
    const scored = rows.filter({ hasText: 'Positive' }), early = rows.filter({ hasText: 'Early' })
    await expect(scored).toHaveCount(1)
    await expect(early).toHaveCount(1)
    await expect(early).toContainText('under 5 teams')
    expect(await fitsWidth(page)).toBe(true)
    expect(await textCollisions(page, '.ui-tr')).toEqual([])

    // Sorted by Reviews either way: scored, then early, then the unrated. (A phone shows the rows as
    // cards with no heading to sort by.)
    if (width >= 760) for (const _ of [0, 1]) {
      await page.getByRole('columnheader', { name: /Reviews/ }).click()
      const texts = await rows.allInnerTexts()
      const at = (word: string) => texts.findIndex(t => t.includes(word))
      expect([at('Positive'), at('Early')]).toEqual([0, 1])
    }

    await early.click()
    const drawer = page.getByRole('complementary', { name: 'Tool details' })
    await expect(drawer).toBeVisible()
    await expect(drawer.locator('.td-stats')).toContainText('Early')
    await expect(drawer.locator('.td-stats')).toContainText('under 5 teams')
    await drawer.getByRole('tab', { name: 'Reviews' }).click()
    await expect(drawer.getByText('Early reviews')).toBeVisible()
    await expect(drawer.getByText(EARLY.samples[0]!.reason)).toBeVisible()
    await expect(drawer.locator('.td-rev-bar')).toHaveCount(0)
    expect(await fitsWidth(page)).toBe(true)
    expect(await textCollisions(page, '.td-rev, .td-stats')).toEqual([])
  })
}
