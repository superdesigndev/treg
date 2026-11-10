import { expect, test, type Page, type Route } from '@playwright/test'
import { json, signIn } from './helpers'

async function switchTo(page: Page, team: string) {
  await page.getByRole('button', { name: 'Teams' }).click()
  const switchButton = page.getByRole('button', { name: 'Switch', exact: true })
  // The innermost block holding both the team's name and a Switch button is that team's row.
  await page.locator('div').filter({ has: page.getByText(team, { exact: true }) }).filter({ has: switchButton })
    .last().getByRole('button', { name: 'Switch', exact: true }).click()
}

test('a slow answer for the team you left never replaces the team you switched to', async ({ page }) => {
  await signIn(page, 'teams', 'First team')
  await page.evaluate(() => fetch('/orgs', {
    method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ name: 'Second team' }),
  }).then(r => r.json()))
  const first = (await page.evaluate(() => fetch('/orgs', { credentials: 'include' }).then(r => r.json())))
    .find((o: { name: string }) => o.name === 'First team')
  await page.reload()
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Your own tools', exact: true }).click()
  await switchTo(page, 'Second team')
  await expect(page.getByRole('button', { name: 'Teams' })).toContainText('Second team')

  // The first team's tool list answers late, and names a tool only that team has.
  let released = false
  await page.route('**/tools', async route => {
    if (route.request().headers()['x-treg-org'] !== first.slug) return route.fallback()
    await new Promise(resolve => setTimeout(resolve, 1500))
    released = true
    await route.fulfill(json([{ id: 990001, name: 'first-team-only', base_url: 'https://first.example', bindings: [], bundle_id: null, cli: null }]))
  })
  await switchTo(page, 'First team')
  await switchTo(page, 'Second team')
  await expect.poll(() => released, { timeout: 5000 }).toBe(true)
  await page.waitForTimeout(300)
  await expect(page.getByRole('button', { name: 'Teams' })).toContainText('Second team')
  await expect(page.getByText('first-team-only')).toHaveCount(0)
})

// The suite's sign-ins sit at the per-IP code limit, so the owner tests below share one.
let owner: Page
test.beforeAll(async ({ browser }, info) => {
  owner = await browser.newPage({ baseURL: info.project.use.baseURL, viewport: info.project.use.viewport })
  await signIn(owner, 'team-cap')
})
test.afterAll(async () => { await owner?.close() })

test('a member with no daily cap reads as no limit, a cap can be set and cleared, spend waits for the rollup, and tabs live in the address', async () => {
  const page = owner
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Team', exact: true }).click()
  const cap = page.getByRole('spinbutton', { name: /^Daily cap for / })
  await expect(cap).toHaveValue('')
  await expect(cap).toHaveAttribute('placeholder', 'No limit')
  await page.getByRole('button', { name: '＋ Add agent' }).click()
  await expect(page.getByRole('spinbutton', { name: 'Daily call cap' })).toHaveValue('')
  for (const value of ['25', '']) {
    await cap.fill(value)
    await cap.press('Tab')
    await page.waitForLoadState('networkidle')
    await page.reload()
    await expect(cap).toHaveValue(value)
  }

  // Spend per API key, on this signed-in session: the suite's sign-ins sit at the per-IP code limit.
  // Both the API Keys column and the Usage chart wait for the rollup, then read it.
  let ready = false
  const asked: string[] = []
  await page.route(url => url.pathname.endsWith('/api-keys/spend'), async route => {
    if (!ready) return route.fulfill(json({ ready: false, days: 30, since: '2026-09-13', keys: [], unattributed: null }))
    return route.fulfill(json({ ready: true, days: 30, since: '2026-09-13', keys: [], unattributed: { spent_micro: 1_250_000, charges: 3 } }))
  })
  await page.route(url => url.pathname.endsWith('/usage/spend'), async route => {
    asked.push(new URL(route.request().url()).search)
    // A month answer takes a moment, as a real one can: long enough to point at the old bars.
    if (new URL(route.request().url()).searchParams.get('group') === 'month') await new Promise(r => setTimeout(r, 700))
    if (!ready) return route.fulfill(json({ ready: false, history_from: '2026-09-11' }))
    return route.fulfill(json({
      ready: true, history_from: '2026-09-11', from: '2026-09-12', to: '2026-10-11', stack: 'key',
      group: new URL(route.request().url()).searchParams.get('group') || 'day',
      spent_micro: 1_504_321, charges: 4,
      series: [{ id: '7', name: 'Laptop · dev', spent_micro: 1_000_000, slot: 0 }, { id: 'none', name: 'No API key', spent_micro: 500_000, slot: 3 }],
      // By day, thirty bars; by month, two. A bar remembered from the day view must not break the month view.
      buckets: new URL(route.request().url()).searchParams.get('group') === 'month'
        ? [{ start: '2026-09-01', parts: {}, charges: 0 }, { start: '2026-10-01', parts: { 7: 1_000_000, none: 500_000 }, charges: 4 }]
        : Array.from({ length: 30 }, (_, i) => ({ start: new Date(Date.UTC(2026, 8, 12 + i)).toISOString().slice(0, 10),
            parts: i === 28 ? { 7: 1_000_000 } : i === 29 ? { none: 500_000 } : {}, charges: i === 28 ? 3 : i === 29 ? 1 : 0 })),
      ranking: [{ id: '7', name: 'Laptop · dev', spent_micro: 1_000_000, charges: 3 }, { id: 'none', name: 'No API key', spent_micro: 500_000, charges: 1 }],
      options: { keys: [{ id: 7, name: 'Laptop · dev', spent_micro: 1_000_000 }], providers: [{ id: 'exa', name: 'Exa', spent_micro: 1_500_000 }] },
    }))
  })
  const nav = page.getByRole('navigation', { name: 'Primary navigation' })

  // Not ready: no numbers anywhere, only the being-prepared message.
  await nav.getByRole('button', { name: 'Team', exact: true }).click()
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await expect(page.getByRole('columnheader', { name: 'Spent last 30 days' }).first()).toBeVisible()
  await expect(page.locator('.key-table .key-spend').first()).toHaveText('Preparing…')
  await expect(page.locator('.key-table .key-spend span').first()).toHaveAttribute('title', 'Spend history is being prepared. It will appear here within a few days.')
  await expect(page.getByText('No API key')).toHaveCount(0)
  await nav.getByRole('button', { name: 'Activity', exact: true }).click()
  await page.getByRole('tab', { name: 'Usage' }).click()
  await expect(page.locator('.us-card').getByText('Spend history is being prepared. It will appear here within a few days.')).toBeVisible()
  await expect(page.getByRole('button', { name: /^Date range:/ })).toHaveCount(0)

  ready = true
  await page.getByRole('tab', { name: 'Calls' }).click()
  await page.getByRole('tab', { name: 'Usage' }).click()
  await expect(page.getByText(/History starts on/)).toBeVisible()
  await expect(page.getByRole('img', { name: /Billed spend by day: \$1\.50/ })).toBeVisible()
  await expect(page.locator('.us-totals')).toContainText('4charges')
  // The card reads to the cent; the exact amount appears at once on hover, and on keyboard focus.
  const total = page.locator('.us-totals b').first()
  await expect(total).toContainText('$1.50')
  const exact = total.getByRole('tooltip')
  await expect(exact).toBeHidden()
  await total.locator('.money').hover()
  await expect(exact).toBeVisible()
  await expect(exact).toHaveText('$1.504321')
  await page.mouse.move(0, 0)
  await expect(exact).toBeHidden()
  await total.locator('.money').focus()
  await expect(exact).toBeVisible()
  await total.locator('.money').blur()
  await expect(page.locator('.us-totals')).not.toContainText(/calls?/i)   // a count of charges, never of calls
  // The picker offers nothing older than the history: no "Last 3 months", no day before it.
  await page.getByRole('button', { name: /^Date range:/ }).click()
  await expect(page.getByRole('button', { name: 'Last 30 days', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Last 3 months', exact: true })).toHaveCount(0)
  expect(asked.at(-1)).toMatch(/^\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}&group=day&stack=key$/)

  await nav.getByRole('button', { name: 'Team', exact: true }).click()
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await expect(page.getByText('Preparing…')).toHaveCount(0)
  await expect(page.locator('.key-table .key-spend').first()).toHaveText('$0.00')
  await expect(page.getByText('Charges on calls made without an API key')).toBeVisible()
  await expect(page.getByText('$1.25')).toBeVisible()

  // Tabs live in the address: a reload keeps the tab, and a key's Activity opens its calls by id.
  await expect(page).toHaveURL(/#orgs\/keys$/)
  await page.reload()
  await expect(page.getByRole('columnheader', { name: 'Spent last 30 days' }).first()).toBeVisible()
  await page.locator('.key-table').getByRole('button', { name: 'Activity', exact: true }).first().click()
  await expect(page).toHaveURL(/#activity\/calls\?key=\d+$/)
  await expect(page.getByRole('tab', { name: 'Calls' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('button', { name: /^API key: Default key · / })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: /^API key: Default key · / })).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/#orgs\/keys$/)

  // The counts: every section loads on its own; Top tools and Per day page ten rows at a time.
  await page.route(url => /\/orgs\/\d+\/usage$/.test(url.pathname), route => route.fulfill(json({
    days: 30, since: '2026-09-12T00:00:00',
    totals: { call: 60, local_run: 0, server_run: 0, total: 60 },
    by_user: [{ user_email: 'dev@example.com', call: 60, local_run: 0, server_run: 0, total: 60 }],
    by_tool: Array.from({ length: 23 }, (_, i) => ({ name: `tool${String(i).padStart(2, '0')}`, total: 30 - i })),
    by_day: Array.from({ length: 12 }, (_, i) => ({ day: `2026-10-${String(i + 1).padStart(2, '0')}`, total: i + 1 })),
    spend: { spend_micro: 0, spend_usd: 0, billed_calls: 0 },
  })))
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  // An old '#usage' link lands on the Usage tab and names it (it once left a blank page).
  await page.goto('/app#usage')
  await expect(page).toHaveURL(/#activity\/usage$/)
  await expect(page.getByRole('tab', { name: 'Usage' })).toHaveAttribute('aria-selected', 'true')
  const tools = page.locator('section').filter({ has: page.getByRole('heading', { name: /Top tools/ }) })
  await expect(tools.getByText('1–10 of 23')).toBeVisible()
  await expect(tools.getByRole('row')).toHaveCount(11)   // the header and ten rows
  await tools.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(tools.getByText('11–20 of 23')).toBeVisible()
  await expect(tools.getByText('tool10', { exact: true })).toBeVisible()
  const days = page.locator('section').filter({ has: page.getByRole('heading', { name: /Per day/ }) })
  await expect(days.getByRole('row').nth(1)).toContainText('2026-10-12')   // newest first
  await days.getByRole('button', { name: 'Older', exact: true }).click()
  await expect(days.getByText('11–12 of 12')).toBeVisible()

  // The chart keeps drawing when the bars change under a hovered one: pick By month, and while
  // its answer loads, point at the day view's last bar; then the two month bars arrive.
  await page.getByRole('button', { name: 'Group by: By day' }).click()
  await page.getByRole('option', { name: 'By month' }).click()
  const plot = page.locator('.sb-plot')
  const box = (await plot.boundingBox())!
  await plot.hover({ position: { x: box.width - 4, y: box.height / 2 } })
  await expect(page.getByRole('img', { name: /Billed spend by month: \$1\.50/ })).toBeVisible()
  await expect(plot.locator('rect').first()).toBeVisible()
  expect(errors).toEqual([])

  // The chart's key filter belongs to the chart alone: the address does not carry it.
  await page.getByRole('button', { name: /^API key: All API keys/ }).click()
  await page.getByRole('option', { name: 'Laptop · dev' }).click()
  await expect(page.getByRole('button', { name: /^API key: Laptop · dev/ })).toBeVisible()
  await expect(page).toHaveURL(/#activity\/usage$/)

  // And '#billing', the name 402 bodies and emails use, lands on Team's Billing tab and says so.
  await page.goto('/app#billing')
  await expect(page).toHaveURL(/#orgs\/billing$/)
})

// The browser-test server is not the hosted service, so /showcase answers 404 there and picture
// uploads are off: these routes stand in for both with the shapes the server returns.
const PIXEL = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
const hex = (c: string) => c.repeat(64)
const rev = (name: string) => ({
  name, url: 'https://' + name.toLowerCase().replace(/\s/g, '') + '.example', tagline: name + ' does a job', description: '',
  category: 'other', maker_name: '', x_handle: '', launch_post_url: '', icon_key: null, shot_keys: [],
  show_calls: false, show_streak: false, show_providers: false, show_categories: false, icon_url: null, shot_urls: [],
})
const entry = (id: number, name: string, over: object) => ({
  id, org_id: 1, status: 'pending', version: 1, live: null, pending: rev(name), reject_reason: '',
  approved_at: null, created_at: '2026-10-01T00:00:00', updated_at: '2026-10-01T00:00:00', ...over,
})
// One entry in each state, to check every label (the list itself does not enforce the limit).
const STATES = [
  entry(1, 'Waiting app', {}),
  entry(2, 'Live app', { status: 'approved', approved_at: '2026-10-02T00:00:00', live: rev('Live app'), pending: null }),
  entry(3, 'Changing app', { status: 'approved', approved_at: '2026-10-02T00:00:00', live: rev('Changing app') }),
  entry(4, 'Refused app', { status: 'rejected', reject_reason: 'the link does not open' }),
  entry(5, 'Hidden app', { status: 'hidden', approved_at: '2026-10-02T00:00:00', live: rev('Hidden app'), pending: null }),
  entry(6, 'Another app', {}),
]

async function uploads(page: Page, on: boolean) {
  await page.unroute('**/auth/me')
  await page.route('**/auth/me', async (route: Route) => {
    const real = await route.fetch()
    if (!real.ok()) return route.fulfill({ response: real })
    return route.fulfill(json({ ...(await real.json()), uploads: on }))
  })
}

test('an owner lists the team\'s showcase entries, applies with pictures, meets the limit, and works without uploads', async () => {
  const page = owner
  let apps: object[] = []
  const posted: any[] = []
  await page.route(/\/showcase\/teams\/\d+\/apps$/, async route => {
    if (route.request().method() === 'POST') {
      posted.push(route.request().postDataJSON())
      return route.fulfill(json(entry(9, posted[0].name, { pending: { ...rev(posted[0].name), ...posted[0] } })))
    }
    return route.fulfill(json(apps))
  })
  const uploaded: { kind: string, type: string | undefined }[] = []
  await page.route(/\/images\/showcase_(icon|shot)$/, async route => {
    const kind = new URL(route.request().url()).pathname.split('/').pop()!
    uploaded.push({ kind, type: route.request().headers()['content-type'] })
    const key = kind === 'showcase_icon' ? `showcase_icon/${hex('a')}.png` : `showcase_shot/${hex(String(uploaded.length))}.webp`
    return route.fulfill(json({ key, kind, preview_url: PIXEL }))
  })

  await test.step('applying sends the form as the server takes it', async () => {
    await uploads(page, true)
    await page.goto('/app#orgs/showcase')
    await page.reload()  // the hash alone does not boot again, and boot reads /auth/me
    await expect(page.getByText('0 of 5 used')).toBeVisible()
    await page.getByRole('button', { name: '＋ Apply with an app' }).click()
    const form = page.getByRole('form', { name: 'Apply with an app' })
    await form.getByLabel('App name').fill('Lead scout')
    await form.getByLabel('Link (https)').fill('https://scout.example')
    await form.getByLabel('Tagline').fill('Finds leads while you sleep')
    await form.getByRole('combobox', { name: 'Category' }).selectOption('sales')
    await form.getByLabel('X handle').fill('@scout_ai')
    await form.getByRole('checkbox', { name: /^Call tier/ }).check()
    await form.getByRole('checkbox', { name: /^Categories/ }).check()
    const files = form.locator('input[type=file]')
    const png = (name: string) => ({ name, mimeType: 'image/png', buffer: Buffer.from('png') })
    await files.nth(0).setInputFiles(png('icon.png'))
    await expect(form.getByRole('img', { name: 'App icon' })).toBeVisible()
    await files.nth(1).setInputFiles(png('one.png'))
    await files.nth(1).setInputFiles(png('two.png'))
    await expect(form.getByRole('img', { name: /^Screenshot/ })).toHaveCount(2)
    await form.getByRole('button', { name: 'Move screenshot 2 up' }).click()
    await form.getByRole('button', { name: 'Send for review' }).click()
    await expect(page.getByText('Sent for review. Nothing is public until treg approves it.')).toBeVisible()
    expect(uploaded).toEqual([
      { kind: 'showcase_icon', type: 'image/png' }, { kind: 'showcase_shot', type: 'image/png' }, { kind: 'showcase_shot', type: 'image/png' }])
    expect(posted).toEqual([{
      name: 'Lead scout', url: 'https://scout.example', tagline: 'Finds leads while you sleep', description: '',
      category: 'sales', maker_name: '', x_handle: '@scout_ai', launch_post_url: '',
      icon_key: `showcase_icon/${hex('a')}.png`, shot_keys: [`showcase_shot/${hex('3')}.webp`, `showcase_shot/${hex('2')}.webp`],
      show_calls: true, show_streak: false, show_providers: false, show_categories: true,
    }])
  })

  await test.step('each entry says where it is', async () => {
    apps = STATES
    await page.reload()
    const list = page.getByRole('list', { name: 'Your showcase entries' })
    for (const chip of ['In review', 'Live', 'Live (change in review)', 'Rejected: the link does not open', 'Hidden'])
      await expect(list.getByText(chip, { exact: true }).first()).toBeVisible()
  })

  await test.step('five places is the limit; a rejected entry does not count', async () => {
    apps = STATES
    await page.reload()
    await expect(page.getByText('5 of 5 used')).toBeVisible()
    await expect(page.getByRole('button', { name: '＋ Apply with an app' })).toBeDisabled()
    await expect(page.getByText(/Your team uses all 5 places/)).toBeVisible()
  })

  await test.step('choosing API keys is saved at once, and a live app can be removed', async () => {
    const live = { ...STATES[1], usage: { source: 'team' } }
    apps = [live]
    const usage: any[] = [], patched: any[] = [], removed: string[] = []
    await page.route(/\/showcase\/teams\/\d+\/keys$/, route => route.fulfill(json([
      { id: 11, name: 'prod', prefix: 'treg_ab12', last_used_at: '2026-10-10T00:00:00', app_id: null },
      { id: 12, name: 'other app', prefix: 'treg_cd34', last_used_at: null, app_id: 99 }])))
    await page.route(/\/showcase\/apps\/\d+\/usage$/, route => {
      usage.push(route.request().postDataJSON())
      return route.fulfill(json({ ...live, usage: { source: 'keys', keys: [{ id: 11, name: 'prod', prefix: 'treg_ab12', last_used_at: null }] } }))
    })
    await page.route(/\/showcase\/apps\/\d+$/, route => { patched.push(route.request().method()); return route.fulfill(json(live)) })
    await page.route(/\/showcase\/apps\/\d+\/remove$/, route => { removed.push(route.request().method()); return route.fulfill(json({ ...live, status: 'hidden' })) })
    await page.reload()
    await expect(page.getByText('Activity: the whole team')).toBeVisible()
    await page.getByRole('button', { name: 'Edit' }).click()
    const form = page.getByRole('form', { name: 'Edit showcase entry' })
    await form.getByRole('radio', { name: /Only some API keys/ }).check()
    await expect(form.getByRole('checkbox', { name: 'API key other app' })).toBeDisabled()   // counts for another app
    await form.getByRole('checkbox', { name: 'API key prod' }).check()
    await form.getByRole('button', { name: 'Send for review' }).click()
    await expect(page.getByText('Saved. The activity boards use it from the next hourly count.')).toBeVisible()
    expect(usage).toEqual([{ source: 'keys', key_ids: [11] }])
    expect(patched).toEqual([])                                     // nothing to review: no PATCH
    await page.getByRole('button', { name: 'Remove from showcase' }).click()
    await page.getByRole('button', { name: 'Confirm remove' }).click()
    await expect(page.getByText('Removed from the showcase. Ask treg if you want it shown again.')).toBeVisible()
    expect(removed).toEqual(['POST'])
    for (const p of [/\/showcase\/teams\/\d+\/keys$/, /\/showcase\/apps\/\d+\/usage$/, /\/showcase\/apps\/\d+$/, /\/showcase\/apps\/\d+\/remove$/])
      await page.unroute(p)
  })

  await test.step('without uploads the form still works, with no picture controls', async () => {
    apps = []
    await uploads(page, false)
    await page.reload()
    await page.getByRole('button', { name: '＋ Apply with an app' }).click()
    const form = page.getByRole('form', { name: 'Apply with an app' })
    await expect(form.getByText('Image uploads are not enabled on this registry. You can apply without images.')).toBeVisible()
    await expect(form.getByRole('button', { name: 'Upload icon' })).toHaveCount(0)
    await expect(form.getByRole('button', { name: 'Add screenshot' })).toHaveCount(0)
    await expect(form.getByRole('button', { name: 'Send for review' })).toBeEnabled()
  })

  await test.step('a registry that refuses uploads hides the controls after the first try', async () => {
    await uploads(page, true)
    await page.route(/\/images\/showcase_icon$/, route => route.fulfill({
      status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'picture uploads are not enabled on this registry' }) }))
    await page.reload()
    await page.getByRole('button', { name: '＋ Apply with an app' }).click()
    const form = page.getByRole('form', { name: 'Apply with an app' })
    await form.locator('input[type=file]').nth(0).setInputFiles({ name: 'icon.png', mimeType: 'image/png', buffer: Buffer.from('png') })
    await expect(form.getByText('Image uploads are not enabled on this registry. You can apply without images.')).toBeVisible()
    await expect(form.getByRole('button', { name: 'Upload icon' })).toHaveCount(0)
  })
})

test('an owner sets and removes the team picture and their own, and initials stand in without one', async () => {
  const page = owner
  await uploads(page, true)
  const set: { path: string, key: string | null }[] = []
  await page.route(/\/images\/(team|user)_avatar$/, route => {
    const kind = new URL(route.request().url()).pathname.split('/').pop()!
    return route.fulfill(json({ key: `${kind}/${hex('b')}.png`, kind, preview_url: PIXEL }))
  })
  await page.route(/\/(me|orgs\/\d+)\/avatar$/, route => {
    const key = route.request().postDataJSON().key
    set.push({ path: new URL(route.request().url()).pathname.replace(/\d+/, 'N'), key })
    return route.fulfill(json({ avatar_url: key ? PIXEL : null }))
  })
  const png = { name: 'me.png', mimeType: 'image/png', buffer: Buffer.from('png') }

  await page.goto('/app#orgs/settings')
  await page.reload()
  await page.getByLabel('Team avatar file').setInputFiles(png)
  await expect(page.getByRole('button', { name: 'Remove avatar' })).toBeVisible()
  await page.getByRole('button', { name: 'Remove avatar' }).click()
  await expect(page.getByRole('button', { name: 'Remove avatar' })).toHaveCount(0)

  await page.locator('summary[aria-label^="Account:"]').click()
  await page.getByRole('button', { name: 'Your avatar' }).click()
  const dialog = page.getByRole('dialog', { name: 'Your avatar' })
  await dialog.getByLabel('Your avatar file').setInputFiles(png)
  await expect(dialog.getByRole('button', { name: 'Remove avatar' })).toBeVisible()
  await expect(page.locator('.rd-avatar img')).toHaveAttribute('src', PIXEL)
  await dialog.getByRole('button', { name: 'Remove avatar' }).click()
  await expect(page.locator('.rd-avatar img')).toHaveCount(0)
  expect(set).toEqual([
    { path: '/orgs/N/avatar', key: `team_avatar/${hex('b')}.png` }, { path: '/orgs/N/avatar', key: null },
    { path: '/me/avatar', key: `user_avatar/${hex('b')}.png` }, { path: '/me/avatar', key: null }])
})
