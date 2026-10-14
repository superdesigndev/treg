import { expect, test, type Page } from '@playwright/test'
import { signIn } from './helpers'

test('agent setup polls only connection status across return and both rotation controls', async ({ page }) => {
  await signIn(page, 'agent-poll')
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' })
  await navigation.getByRole('button', { name: 'Team', exact: true }).click()
  await page.getByRole('button', { name: 'Team settings', exact: true }).click()
  const archiveSetting = page.waitForResponse(r => /\/orgs\/\d+\/settings$/.test(r.url()) && r.request().method() === 'PATCH')
  await page.getByTitle('archive on — click to opt out', { exact: true }).getByRole('checkbox').uncheck()
  expect((await (await archiveSetting).json()).archive).toBe(false)
  await page.getByRole('button', { name: 'Members', exact: true }).click()
  await page.getByRole('button', { name: '＋ Add agent' }).click()
  await page.getByPlaceholder('ci-bot').fill('poll-bot')
  await page.getByText('All tools', { exact: true }).first().click()
  const created = page.waitForResponse(r => /\/orgs\/\d+\/agents$/.test(r.url()) && r.request().method() === 'POST')
  await page.getByRole('button', { name: 'Create', exact: true }).click()
  const agent = await (await created).json()
  await expect(page.getByText('Token for poll-bot', { exact: false })).toBeVisible()

  await navigation.getByRole('button', { name: 'Catalog', exact: true }).click()
  await navigation.getByRole('button', { name: 'Team', exact: true }).click()
  // Let the page's own roster load on return finish first, so only the poll can see the check-in.
  await page.waitForLoadState('networkidle')
  const requests: string[] = []
  page.on('request', request => {
    if (request.method() === 'GET' && /\/orgs\/\d+\//.test(request.url())) requests.push(new URL(request.url()).pathname)
  })
  const checkin = await page.request.post('/agents/checkin', { headers: { 'X-Treg-Token': agent.token } })
  expect(checkin.ok()).toBe(true)
  await expect(page.getByText(/connected .*poll-bot called in as itself/)).toBeVisible({ timeout: 8000 })
  expect(requests.length).toBeGreaterThan(0)
  expect(requests.every(path => path.endsWith(`/agents/${agent.user_id}/connection`))).toBe(true)
  await page.getByRole('button', { name: 'Done', exact: true }).click()

  // An existing lifetime check-in cannot confirm a freshly rotated credential.
  await page.getByRole('button', { name: 'Rotate', exact: true }).click()
  const rotated = page.waitForResponse(r => /\/orgs\/\d+\/agents$/.test(r.url()) && r.request().method() === 'POST')
  await page.getByRole('button', { name: 'Confirm rotate', exact: true }).click()
  const replacement = await (await rotated).json()
  await expect(page.getByText('waiting for its first check-in…', { exact: true })).toBeVisible()
  await page.waitForResponse(r => r.url().includes(`/connection?api_key_id=${replacement.api_key_id}`))
  await expect(page.getByText(/connected .*poll-bot called in as itself/)).not.toBeVisible()
  expect((await page.request.post('/agents/checkin', { headers: { 'X-Treg-Token': replacement.token } })).ok()).toBe(true)
  await expect(page.getByText(/connected .*poll-bot called in as itself/)).toBeVisible({ timeout: 8000 })

  await page.getByRole('button', { name: 'I’ve updated poll-bot', exact: true }).click()
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await page.getByRole('row').filter({ hasText: 'poll-bot' }).getByRole('button', { name: 'Rotate', exact: true }).click()
  const keyRotation = page.waitForResponse(r => /\/api-keys\/\d+\/rotate$/.test(r.url()) && r.request().method() === 'POST')
  await page.getByRole('dialog').getByRole('button', { name: 'Confirm rotate', exact: true }).click()
  const key = await (await keyRotation).json()
  await expect(page.getByText('New key for poll-bot', { exact: true })).toBeVisible()
  await page.waitForResponse(r => r.url().includes(`/connection?api_key_id=${key.id}`))
  await expect(page.getByText('waiting for its first check-in…', { exact: true })).toBeVisible()
  expect((await page.request.post('/agents/checkin', { headers: { 'X-Treg-Token': key.secret } })).ok()).toBe(true)
  await expect(page.getByText(/connected .*poll-bot called in as itself/)).toBeVisible({ timeout: 8000 })
  await page.getByRole('button', { name: 'Team settings', exact: true }).click()
  await expect(page.getByTitle('archive off — click to opt back in', { exact: true }).getByRole('checkbox')).not.toBeChecked()

  // Reuse this signed-in flow so the full suite stays within the local OTP rate window.
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await page.getByRole('button', { name: 'I’ve updated poll-bot', exact: true }).click()
  const org = (await (await page.request.get('/orgs')).json()).find((org: any) => org.name === 'Browser test team')
  const headers = { 'X-Treg-Org': org.slug }
  const path = `/orgs/${org.org_id}/api-keys`
  const active = await (await page.request.post(path, { headers, data: { name: 'menu-active' } })).json()
  const retired = await (await page.request.post(path, { headers, data: { name: 'menu-retired' } })).json()
  expect((await page.request.post(`${path}/${retired.id}/revoke`, { headers })).ok()).toBe(true)
  await page.getByRole('button', { name: 'API Keys', exact: true }).click()
  await page.getByPlaceholder('New key name').fill('retained-result')
  await page.getByRole('button', { name: 'Create key', exact: true }).click()
  const card = page.locator('.card').filter({ hasText: 'retained-result — copy it now' })
  await expect(card).toBeVisible()

  const mutations: string[] = []
  page.on('request', request => {
    if (request.method() === 'POST' && /\/api-keys\/\d+\/(disable|enable|revoke|hide)$/.test(new URL(request.url()).pathname)) mutations.push(request.url())
  })
  await page.getByRole('button', { name: 'More actions for menu-active', exact: true }).click()
  const menu = page.getByRole('menu')
  await expect(menu).toContainText("Handle this team's retained key result first")
  await expect(menu.getByRole('menuitem', { name: 'Disable', exact: true })).toBeDisabled()
  await expect(menu.getByRole('menuitem', { name: 'Revoke', exact: true })).toBeDisabled()
  await expect(menu.getByRole('menuitem', { name: 'Rename', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'More actions for menu-active', exact: true }).click()
  await expect(menu).not.toBeVisible()
  await page.getByRole('button', { name: 'More actions for menu-retired', exact: true }).click()
  await expect(menu.getByRole('menuitem', { name: 'Hide', exact: true })).toBeDisabled()
  await expect(menu).toContainText("Handle this team's retained key result first")
  expect(mutations).toEqual([])

  await card.getByRole('button', { name: 'Done', exact: true }).click()
  await page.getByRole('button', { name: 'More actions for menu-active', exact: true }).click()
  await menu.getByRole('menuitem', { name: 'Disable', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Disable “menu-active”?' })
  await expect(dialog).toBeVisible()
  const disabled = page.waitForResponse(r => new URL(r.url()).pathname === `${path}/${active.id}/disable`)
  await dialog.getByRole('button', { name: 'Confirm disable', exact: true }).click()
  expect((await disabled).ok()).toBe(true)
  expect(mutations).toHaveLength(1)
})

async function switchTeam(page: Page, name: string) {
  await page.getByRole('button', { name: 'Teams' }).click()
  const button = page.getByRole('button', { name: 'Switch', exact: true })
  await page.locator('div').filter({ has: page.getByText(name, { exact: true }) }).filter({ has: button })
    .last().getByRole('button', { name: 'Switch', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Teams' })).toContainText(name)
}

async function waitForConsumedCredential(page: Page, orgId: number, keyId: number) {
  // waitForResponse only observes the network. Inspect the mounted Vue instance (no production
  // test hook) to require application consumption, then drain its pending render before asserting.
  await expect.poll(() => page.evaluate(async ({ orgId, keyId }) => {
    const vm = (document.querySelector('#app') as any)?._vnode?.component?.proxy
    if (!vm) throw new Error('The dashboard Vue instance is not mounted')
    const issue = vm.credentialIssues[orgId]
    if (issue?.status !== 'held' || (issue.result?.api_key_id ?? issue.result?.id) !== keyId) return false
    await vm.$nextTick() // after consumption, so its queued render is included in this barrier
    return vm.credentialIssues[orgId] === issue && issue.status === 'held' && vm.activeOrgId !== orgId
  }, { orgId, keyId }), { message: 'the issuing org has consumed the delayed result while another org is active' }).toBe(true)
}

for (const action of ['create', 'rotate', 'keyAction'] as const) {
  test(`${action}: a committed response arriving in another team remains recoverable`, async ({ page }) => {
    await signIn(page, `issued-${action}`, 'Issuing team')
    const orgs = await (await page.request.get('/orgs')).json()
    const first = orgs.find((org: any) => org.name === 'Issuing team')
    await page.request.post('/orgs', { data: { name: 'Other team' } })
    let old: any
    if (action !== 'create') {
      old = await (await page.request.post(`/orgs/${first.org_id}/agents`, {
        headers: { 'X-Treg-Org': first.slug }, data: { name: 'retained-bot' },
      })).json()
    }
    await page.reload()
    await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Team', exact: true }).click()
    if (action === 'keyAction') await page.getByRole('button', { name: 'API Keys', exact: true }).click()
    else if (action === 'create') {
      await page.getByRole('button', { name: '＋ Add agent' }).click()
      await page.getByPlaceholder('ci-bot').fill('retained-bot')
      await page.getByText('All tools', { exact: true }).first().click()
    }

    const path = action === 'keyAction' ? `/orgs/${first.org_id}/api-keys/${old.api_key_id}/rotate` : `/orgs/${first.org_id}/agents`
    let release!: () => void, committed!: () => void, result: any, postCount = 0
    const gate = new Promise<void>(resolve => { release = resolve })
    const wrote = new Promise<void>(resolve => { committed = resolve })
    await page.route(`**${path}`, async route => {
      if (route.request().method() !== 'POST') return route.fallback()
      postCount++
      const response = await route.fetch() // server commits before navigation; delay only delivery
      expect(response.ok()).toBe(true)
      result = await response.json()
      committed()
      await gate
      await route.fulfill({ response })
    })
    const received = page.waitForResponse(r => new URL(r.url()).pathname === path && r.request().method() === 'POST')
    if (action === 'create') await page.getByRole('button', { name: 'Create', exact: true }).click()
    else {
      await page.getByRole('row').filter({ hasText: 'retained-bot' }).getByRole('button', { name: 'Rotate', exact: true }).click()
      await page.getByRole('button', { name: 'Confirm rotate', exact: true }).click()
    }
    await wrote
    if (old) expect((await page.request.post('/agents/checkin', { headers: { 'X-Treg-Token': old.token } })).status()).toBe(401)
    await switchTeam(page, 'Other team')
    release(); await received
    await waitForConsumedCredential(page, first.org_id, result.api_key_id ?? result.id)
    const secret = result.token || result.secret
    await expect(page.locator('body')).not.toContainText(secret)
    expect(await page.locator('input').evaluateAll((inputs, value) => inputs.some(input => input.value === value), secret)).toBe(false)
    expect(await page.locator('pre, code').evaluateAll((snippets, value) => snippets.some(snippet => snippet.textContent?.includes(value)), secret)).toBe(false)
    expect(await page.evaluate(value => {
      const vm = (document.querySelector('#app') as any)._vnode.component.proxy
      return vm.agentSnippet.includes(value) || vm.newAgent?.token === value || vm.newApiKey?.secret === value
    }, secret)).toBe(false) // also cover the values wired to the copy buttons

    await switchTeam(page, 'Issuing team')
    const card = page.locator('.card').filter({ hasText: action === 'keyAction' ? 'New key for retained-bot' : 'Token for retained-bot' })
    await expect(card.getByRole('textbox')).toHaveValue(secret)
    expect(postCount).toBe(1)
    expect(await page.evaluate(value => [...Object.values(localStorage), ...Object.values(sessionStorage)]
      .some(stored => stored.includes(value)), secret)).toBe(false)
    await card.getByRole('button', { name: action === 'create' ? 'Done' : 'I’ve updated retained-bot', exact: true }).click()
    await switchTeam(page, 'Other team'); await switchTeam(page, 'Issuing team')
    await expect(card).not.toBeVisible()
    expect(postCount).toBe(1)
  })
}
