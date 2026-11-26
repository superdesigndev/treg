import { expect, test, type Page } from '@playwright/test'
import { signIn } from './helpers'

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
    else if (action === 'rotate') {
      await page.getByRole('row').filter({ hasText: 'retained-bot' }).getByRole('button', { name: 'More actions for retained-bot' }).click()
      await page.getByRole('menuitem', { name: 'Rotate token' }).click()
      await page.getByRole('dialog').getByRole('button', { name: 'Rotate token' }).click()
    } else {
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
