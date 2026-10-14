import { expect, test, type Page } from '@playwright/test'

// The Catalog box (state/find.js): a typing pause of two characters or more asks the finder; its
// answer sits above the still-filtered shelves, and an empty one is a single line. Enter asks for
// the full answer, as before. The browser-test server has no judge key, so /catalog/find is
// answered here and every request to it is counted.

const row = (id: string, p: number) => ({
  id, name: id, provider: id.split('.')[0], provider_display: id.split('.')[0], platform: 'people',
  platform_label: 'People & contact data', capability: 'people.email.find',
  capability_description: 'Find a work email', cost: { type: 'per_call', usd: 0.01 }, p, fit_from: 'job' })

async function answer(page: Page, judged: object) {
  const asked: string[] = []
  await page.route('**/catalog/find?**', route => {
    asked.push(new URL(route.request().url()).searchParams.get('q') || '')
    return route.fulfill({ status: 200, contentType: 'application/x-ndjson',
      body: JSON.stringify({ event: 'candidates', candidates: [] }) + '\n'
          + JSON.stringify({ event: 'judged', named: '', read: 3, high: 0.7, rows: [], ...judged }) + '\n' })
  })
  await page.goto('/catalog')
  await expect(page.locator('.pl-sec').first()).toBeVisible()
  // The shelves paint from inline data before the session check and the connections request
  // answer; type only once the boot has settled, so what is measured is the box, not the boot.
  await page.waitForLoadState('networkidle')
  return asked
}

const box = (page: Page) => page.locator('.cat-find input')

test('a typing pause answers above the shelves and leaves them in place', async ({ page }) => {
  const asked = await answer(page, { verdict: 'strong', rows: [row('hunter.people.email.find', 0.9)] })
  const shelves = await page.locator('.pl-sec').count()
  // One input event and then a pause: the gate reads the finished sentence (per-keystroke states
  // are the third case's business). Each step below fails with its own message.
  const request = page.waitForRequest(/\/catalog\/find\?/, { timeout: 5000 })
  await box(page).fill('find the work email of a dentist')
  await expect(box(page), 'the box holds the whole sentence').toHaveValue('find the work email of a dentist')
  await request.catch(() => { throw new Error('the typing pause never asked /catalog/find') })
  await expect(page.locator('.fa .ui-tbody .ui-tr'), 'the answer is drawn above the shelves').toHaveCount(1)
  expect(asked).toEqual(['find the work email of a dentist'])
  await expect(page.locator('.pl-sec')).toHaveCount(shelves)                 // the shelves stay
  await expect(page.locator('.cat-card.find-dim')).toHaveCount(0)            // and are not re-lit
  await expect(page.locator('.cat-find-suggest')).toBeVisible()              // Enter still offers the full answer
})

test('an empty auto answer is one line, never a page', async ({ page }) => {
  await answer(page, { verdict: 'none', reason: 'gap' })
  await box(page).fill('book a table for two tonight')
  const line = page.locator('.fa .fa-line')
  await expect(line).toContainText('treg does not have this kind of data or action yet')
  await expect(page.locator('.fa .fa-note, .fa .ui-table')).toHaveCount(0)
  expect((await line.boundingBox())!.height).toBeLessThan(40)
  await expect(page.locator('.pl-sec').first()).toBeVisible()
})

test('a platform name being typed filters the shelves and gets an answer above them; one character does not ask', async ({ page }) => {
  const asked = await answer(page, { verdict: 'strong', rows: [row('hunter.people.email.find', 0.9)] })
  const everyCard = await page.locator('.cat-card').count()
  await box(page).fill('t')
  await page.waitForTimeout(1200)
  expect(asked, 'one character does not ask').toEqual([])
  await box(page).fill('tiktok')
  await expect.poll(() => asked).toEqual(['tiktok'])
  await expect(page.locator('.fa .ui-tbody .ui-tr')).toHaveCount(1)
  const names = await page.locator('.cat-card .cat-name b').allTextContents()
  expect(names, 'the shelves stay filtered to the name').toContain('TikTok')
  expect(names.length).toBeLessThan(everyCard)
  await box(page).press('Enter')                                            // Enter still asks, for the full answer
  await expect.poll(() => asked).toEqual(['tiktok', 'tiktok'])
})

test('an empty answer on a shelf offers the whole catalog, and asks it with the same words', async ({ page }) => {
  const asked: { q: string, platform: string | null }[] = []
  await page.route('**/catalog/find?**', route => {
    const url = new URL(route.request().url())
    const platform = url.searchParams.get('platform')
    asked.push({ q: url.searchParams.get('q') || '', platform })
    const judged = platform
      ? { verdict: 'none', reason: 'scope', rows: [] }
      : { verdict: 'strong', rows: [row('hunter.people.email.find', 0.9)] }
    return route.fulfill({ status: 200, contentType: 'application/x-ndjson',
      body: JSON.stringify({ event: 'candidates', candidates: [] }) + '\n'
          + JSON.stringify({ event: 'judged', named: '', read: 3, high: 0.7, reason: '', ...judged }) + '\n' })
  })
  await page.goto('/catalog/companies')
  const shelfBox = page.locator('.pl-hero .cat-find input')
  await expect(shelfBox).toBeVisible()
  await page.waitForLoadState('networkidle')
  await shelfBox.fill('verify an email before sending')
  await shelfBox.press('Enter')
  const line = page.locator('.fa .fa-line')
  await expect(line).toContainText('Nothing in')
  await expect(line).toContainText('verify an email before sending')
  await expect(page.locator('.fa').getByText('Request it')).toHaveCount(0)   // a shelf cannot call it a gap
  await line.getByRole('button', { name: 'Search all tools' }).click()
  await expect(page).toHaveURL(/\/catalog$/)
  await expect(box(page)).toHaveValue('verify an email before sending')
  await expect(page.locator('.fa .ui-tbody .ui-tr')).toHaveCount(1)
  expect(asked.at(-1)).toEqual({ q: 'verify an email before sending', platform: null })
})

test('a result opened from the Catalog page leads to its job, and Back returns to the Catalog page', async ({ page }) => {
  const asked = await answer(page, { verdict: 'strong', rows: [row('hunter.people.email.find', 0.9)] })
  await box(page).fill('find a work email')
  await expect.poll(() => asked).toEqual(['find a work email'])
  await page.locator('.fa .ui-tbody .ui-tr').first().click()
  // the job's own comparison on its platform, every provider of it
  await expect(page).toHaveURL(/\/catalog\/people\/[^/]+$/)
  await expect(page.locator('h1')).toContainText('work email')
  await page.goBack()
  await expect(page).toHaveURL(/\/catalog$/)
  await expect(page.locator('.pl.cat h1')).toContainText('tools for agents')      // the Catalog page, not "Your own tools"
  await expect(box(page)).toHaveValue('find a work email')
  await expect(page.locator('.fa .ui-tbody .ui-tr')).toHaveCount(1)                 // the answer is still there
})
