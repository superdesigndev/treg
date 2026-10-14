import { expect, test, type Page } from '@playwright/test'
import { fitsWidth, sidewaysCulprits, signIn, textCollisions } from './helpers'

// Every main page, at desktop and phone width: the page never scrolls sideways, and no text in a
// row, card or cell paints over other text or past its edge. This is the class of bug every table
// here kept shipping (a fixed column and a price that would not wrap), caught by rendering, not by
// reading CSS.
const ROWS = 'tr, .pl-card, .cat-find-suggest, .pl-autocard, .td-stats > div, .td-params > div'

async function expectClean(page: Page, where: string) {
  if (!(await fitsWidth(page))) expect(await sidewaysCulprits(page), `${where}: scrolls sideways`).toEqual([])
  expect(await textCollisions(page, ROWS), where).toEqual([])
}

test('the collision check sees text painted over text, and past its row', async ({ page }) => {
  // The checker's own test: a fixed grid column too narrow for a price that will not wrap, the shape
  // of the bug a table cannot have (its column grows to the content) but a hand-made grid row can.
  await page.setContent(`<div class="row" style="display:grid;grid-template-columns:120px 60px;width:180px;font:12px monospace">
    <span style="white-space:nowrap">$0.025/started 10 emails</span><span>fit</span></div>
    <div class="row" style="width:100px"><span style="white-space:nowrap">a line far wider than its row</span></div>
    <div class="row" style="width:100px"><span style="display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">clipped, so it only counts as far as it shows</span></div>`)
  const found = await textCollisions(page, '.row')
  expect(found).toContain('$0.025/started 10 emails / fit')
  expect(found).toContain('past its row: a line far wider than its row')
  expect(found.some(f => f.includes('clipped'))).toBe(false)
})

for (const [width, height] of [[1440, 1000], [390, 844]] as const) {
  test.describe(`at ${width}px`, () => {
    test.use({ viewport: { width, height } })

    test('the public catalog, a shelf, a comparison and a tool lay out cleanly', async ({ page }) => {
      await page.goto('/catalog')
      await expect(page.locator('.cat-find')).toBeVisible()
      await expectClean(page, '/catalog')
      for (const slug of ['companies', 'people']) {
        await page.goto('/catalog/' + slug)
        await expect(page.locator('.pl-cmp').first()).toBeVisible()
        await expectClean(page, '/catalog/' + slug)
      }
      await page.locator('.pl-cmp').first().click()
      await expect(page.locator('.ui-data-table .ui-tbody .ui-tr').first()).toBeVisible()
      await expectClean(page, 'a comparison page')
      await page.locator('.ui-data-table .ui-tbody .ui-tr').first().click()
      await expect(page.getByRole('complementary', { name: 'Tool details' })).toBeVisible()
      await expectClean(page, 'the tool drawer')
    })

    test('a search answer keeps a long price in its column', async ({ page }) => {
      // The browser-test server has no judge key; answer /catalog/find with real catalog rows, one of
      // them priced "per started 10 emails", the rate that once painted over the fit bar.
      const detail = await (await page.request.get('/catalog/endpoints/hunter.companies.emails')).json()
      const ep = detail.endpoint
      const rows = [ep, ...detail.siblings.slice(0, 3)].map((e: any, i: number) => ({
        id: e.id, name: e.name || e.summary, provider: e.provider, provider_display: e.provider_display,
        platform: e.platform || 'people', platform_label: 'People & contact data', capability: e.capability,
        capability_description: e.name, cost: e.cost, p: 0.9 - i * 0.1 }))
      await page.route('**/catalog/find?**', route => route.fulfill({ status: 200, contentType: 'application/x-ndjson',
        body: JSON.stringify({ event: 'candidates', candidates: [] }) + '\n'
            + JSON.stringify({ event: 'judged', verdict: 'strong', named: '', read: 4, high: 0.8, rows }) + '\n' }))
      await page.goto('/catalog')
      await page.locator('.cat-find input').fill('list every email address at a company')
      await page.keyboard.press('Enter')
      await expect(page.locator('.fa .ui-tbody .ui-tr').first()).toBeVisible()
      await expectClean(page, 'a search answer')
    })

    test('the signed-in pages with tables lay out cleanly', async ({ page }) => {
      await signIn(page, `layout-${width}`)
      for (const hash of ['activity', 'orgs', 'tools', 'catalog', 'connections']) {
        await page.goto('/app#' + hash)
        await page.waitForLoadState('networkidle')
        await expectClean(page, '/app#' + hash)
      }
    })
  })
}

// The top bar is one row down to 1121px. Every width in that range holds the brand, the team, every
// nav destination (the hub's too) and the account strip, none painted over another or scrolled away.
test('the top bar never paints one item over another', async ({ page }) => {
  await signIn(page, 'top-bar')
  await page.getByRole('navigation', { name: 'Primary navigation' }).evaluate(nav => {
    // The browser-test server runs with the hub off: add its button, the widest the bar gets.
    const hub = nav.lastElementChild!.cloneNode(true) as HTMLElement
    hub.lastChild!.textContent = 'Hub'
    nav.insertBefore(hub, nav.lastElementChild)
  })
  for (const width of [1121, 1200, 1300, 1301, 1366, 1440, 1600, 1920]) {
    await page.setViewportSize({ width, height: 800 })
    const found = await page.evaluate(() => {
      const nav = document.querySelector('.rd-navs')!
      const items = [...document.querySelectorAll('.rd-brand, .rd-top .orgblock, .rd-nav, .rd-referral, .rd-social a, .rd-balance, .rd-account-menu')]
        .filter(e => e.getClientRects().length).map(e => ({ e, r: e.getBoundingClientRect() }))
      const out: string[] = nav.scrollWidth > nav.clientWidth + 1 ? ['the nav scrolls'] : []
      for (let i = 0; i < items.length; i++) for (let j = i + 1; j < items.length; j++) {
        const a = items[i].r, b = items[j].r
        if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1)
          out.push(`${items[i].e.textContent!.trim() || items[i].e.className} over ${items[j].e.textContent!.trim() || items[j].e.className}`)
      }
      return out
    })
    expect(found, `${width}px`).toEqual([])
  }
})
