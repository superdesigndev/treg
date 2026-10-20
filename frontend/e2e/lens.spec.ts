import { test, expect, type Page } from '@playwright/test'
import { signIn, json, fitsWidth, textCollisions } from './helpers'

const created = '2026-01-01T12:00:00Z'
const profile = { id: 'brand-1', brand: 'Acme', url: 'https://acme.example', description: 'Project tools for growing teams.', competitors: [{ name: 'Contoso', domain: 'contoso.example' }], questions: ['Which project tools help growing teams?', 'Acme or Contoso for a small team?'], engines: ['chatgpt', 'perplexity', 'gemini', 'google'], revision: 1, can_edit: true, created_at: created, updated_at: created }
const result = {
  kind: 'audit', input: profile, completed_at: created, known_cost_micro: 12000, total_cost_micro: 12000, cost_known: true, errors: [], attempts: [],
  results: [{ id: 'answer-1', engine: 'chatgpt', question: profile.questions[0], answer: 'Acme and Contoso are useful project tools for a growing team.', citations: [{ title: 'Acme product guide', url: 'https://acme.example/guide', domain: 'acme.example' }], mentioned_brands: ['Acme', 'Contoso'], brand_mentioned: true, position: 1, charged_micro: 6000, cost_known: true, source_kind: 'web-interface', provider: 'dataforseo', completed_at: created },
    { id: 'answer-2', engine: 'perplexity', question: profile.questions[1], answer: 'Contoso offers a focused project workspace.', citations: [], mentioned_brands: ['Contoso'], brand_mentioned: false, position: null, charged_micro: 6000, cost_known: true, source_kind: 'model-api', provider: 'perplexity', completed_at: created }],
}
const research = { kind: 'onboarding', input: { url: profile.url }, completed_at: created, known_cost_micro: 5000, total_cost_micro: 5000, cost_known: true, errors: [], results: [], attempts: [], research: { brand: profile.brand, description: profile.description, competitors: [{ ...profile.competitors[0], reason: 'A competing project workspace.' }], questions: profile.questions, sources: [{ title: 'About Acme', url: 'https://acme.example/about' }] } }

async function fixture(page: Page, options: { existing?: boolean; readOnly?: boolean; viewer?: boolean; lostResearch?: boolean; unknownAudit?: boolean; expiredQuote?: boolean; staleEdit?: boolean; off?: boolean } = {}) {
  let saved: any = options.existing ? { ...profile, can_edit: !options.readOnly && !options.viewer } : null
  const runs: any[] = []
  const calls = { quotes: 0, paid: 0, saves: 0, requestIds: [] as string[], headers: [] as string[], puts: 0 }
  let currentQuote: any = null
  let active: any = null
  await page.route('**/auth/me', async route => { const response = await route.fetch(); if (!response.ok()) return route.fulfill({ response }); await route.fulfill(json({ ...(await response.json()), lens_enabled: !options.off })) })
  await page.route('**/lens/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname; const method = request.method()
    calls.headers.push(request.headers()['x-treg-org'] || '')
    if (path === '/lens/projects' && method === 'GET') return route.fulfill(json({ projects: saved ? [saved] : [] }))
    if (path === '/lens/projects' && method === 'POST') {
      const body = request.postDataJSON(); calls.saves++; calls.requestIds.push(body.request_id)
      saved = { ...profile, ...body }; return route.fulfill(json(saved))
    }
    if (path === '/lens/projects/brand-1' && method === 'PUT') {
      calls.puts++
      if (options.staleEdit) return route.fulfill({ ...json({ detail: 'This project changed in another session.' }), status: 409 })
      saved = { ...saved, ...request.postDataJSON(), revision: saved.revision + 1 }; return route.fulfill(json(saved))
    }
    if (path === '/lens/projects/brand-1') return route.fulfill(json({ project: saved, runs }))
    if (path === '/lens/quotes') {
      calls.quotes++; const body = request.postDataJSON()
      currentQuote = { id: `run-${calls.quotes}`, run_id: `run-${calls.quotes}`, kind: body.kind, input: body, total_cost_micro: body.kind === 'onboarding' ? 5000 : 12000, expires_at: new Date(Date.now() + (options.expiredQuote ? -1000 : 60000)).toISOString(), note: 'Actual provider charges may vary. Own provider keys are billed by the provider.', currency: 'USD', project_id: body.project_id || null, project_revision: body.project_revision || null }
      return route.fulfill(json(currentQuote))
    }
    if (path === '/lens/runs' && method === 'POST') {
      calls.paid++
      const onboarding = currentQuote.kind === 'onboarding'
      active = { id: currentQuote.id, kind: currentQuote.kind, state: options.unknownAudit && !onboarding ? 'interrupted' : 'completed', project_id: onboarding ? null : saved.id, created_at: created, completed_at: created,
        result: onboarding ? research : options.unknownAudit ? { ...result, total_cost_micro: null, cost_known: false, errors: [{ engine: 'gemini', question: profile.questions[0], detail: 'The upstream reply was interrupted; its charge is unconfirmed.' }] } : result,
        error: options.unknownAudit && !onboarding ? 'Some requests could not be confirmed. They were not retried.' : null }
      if (!onboarding) runs.unshift(active)
      if (options.lostResearch && onboarding) return route.abort('connectionfailed')
      return route.fulfill(json(active))
    }
    if (path.startsWith('/lens/runs/')) return route.fulfill(json(active || { id: currentQuote.id, kind: currentQuote.kind, state: 'quoted', result: null, created_at: created, quote: currentQuote }))
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Unknown fixture route' }) })
  })
  await signIn(page, 'lens')
  if (options.viewer) {
    await page.route('**/orgs', async route => { const response = await route.fetch(); const rows = await response.json(); await route.fulfill(json(rows.map((row: any) => ({ ...row, role: 'viewer' })))) })
    await page.reload()
  }
  return calls
}
const open = async (page: Page) => { await page.getByRole('button', { name: 'Lens', exact: true }).click(); await expect(page.getByRole('heading', { name: 'Lens', exact: true })).toBeVisible() }
async function manual(page: Page) {
  await page.getByLabel('Website URL', { exact: true }).fill('acme.example')
  await page.getByRole('button', { name: 'Set up manually — free' }).click()
  await page.getByLabel('Brand name', { exact: true }).fill('Acme')
  await page.getByRole('button', { name: 'Choose buyer questions' }).click()
  await page.getByLabel('Buyer question 1', { exact: true }).fill('Which project tools help growing teams?')
}

test('URL research requires explicit estimate approval, then saves a reviewed profile and runs a separately approved audit', async ({ page }) => {
  const calls = await fixture(page); await open(page)
  await page.getByLabel('Website URL', { exact: true }).fill('acme.example')
  await page.getByRole('button', { name: 'Get research estimate', exact: true }).click()
  await expect(page.getByLabel('Research estimate')).toContainText('$0.005')
  expect(calls.paid).toBe(0)
  await page.getByRole('button', { name: 'Research my brand — $0.005' }).click()
  await expect(page.getByLabel('Brand name', { exact: true })).toHaveValue('Acme')
  await expect(page.getByLabel('Competitor 1', { exact: true })).toHaveValue('Contoso')
  await page.getByText('Research sources and receipt', { exact: true }).click()
  await expect(page.getByRole('link', { name: 'About Acme' })).toHaveAttribute('href', 'https://acme.example/about')
  await page.getByRole('button', { name: 'Choose buyer questions' }).click()
  await expect(page.getByText('Suggestions are generated ideas', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: 'Save brand profile' }).click()
  await expect(page.getByRole('heading', { name: 'Review your audit' })).toBeVisible()
  expect(calls.paid).toBe(1); expect(calls.saves).toBe(1)
  await page.getByRole('button', { name: 'Get audit estimate' }).click()
  await expect(page.getByText('Actual provider charges may vary', { exact: false }).first()).toBeVisible()
  expect(calls.paid).toBe(1)
  await page.getByRole('button', { name: 'Run audit — estimate $0.012' }).click()
  await expect(page.getByRole('heading', { name: 'Audit results · Acme' })).toBeVisible()
  await expect(page.locator('.lens-metrics').getByText('50%', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Acme product guide' })).toHaveAttribute('href', 'https://acme.example/guide')
  await expect(page.getByRole('heading', { name: 'Audit history' })).toBeVisible()
  expect(calls.paid).toBe(2); expect(calls.headers.every(Boolean)).toBe(true)
})

test('manual setup remains free and browser reload preserves the draft and question choices', async ({ page }) => {
  const calls = await fixture(page); await open(page); await manual(page)
  await page.getByRole('button', { name: 'Add a question' }).click()
  await page.getByLabel('Buyer question 2', { exact: true }).fill('Which project tool is best for remote teams?')
  await page.getByLabel('Track buyer question 2', { exact: true }).uncheck()
  await page.reload()
  await expect(page.getByLabel('Buyer question 1', { exact: true })).toHaveValue('Which project tools help growing teams?')
  await expect(page.getByLabel('Track buyer question 2', { exact: true })).not.toBeChecked()
  await page.getByRole('button', { name: 'Save brand profile' }).click()
  await expect(page.getByText('1 buyer questions', { exact: false }).first()).toBeVisible()
  expect(calls.quotes).toBe(0); expect(calls.paid).toBe(0); expect(calls.saves).toBe(1)
})

test('a lost paid response recovers by reading the durable run, including across reload', async ({ page }) => {
  const calls = await fixture(page, { lostResearch: true }); await open(page)
  await page.getByLabel('Website URL', { exact: true }).fill('acme.example')
  await page.getByRole('button', { name: 'Get research estimate', exact: true }).click()
  await page.getByRole('button', { name: 'Research my brand — $0.005' }).click()
  await expect(page.getByRole('button', { name: 'Check request status' })).toBeVisible()
  expect(calls.paid).toBe(1)
  await page.reload()
  await expect(page.getByLabel('Brand name', { exact: true })).toHaveValue('Acme')
  expect(calls.paid).toBe(1)
})

test('expired research estimate requires a fresh estimate and never starts the paid run', async ({ page }) => {
  const calls = await fixture(page, { expiredQuote: true }); await open(page)
  await page.getByLabel('Website URL', { exact: true }).fill('acme.example')
  await page.getByRole('button', { name: 'Get research estimate', exact: true }).click()
  await page.getByRole('button', { name: 'Research my brand — $0.005' }).click()
  await expect(page.getByRole('alert')).toContainText('expired')
  await expect(page.getByRole('button', { name: 'Get research estimate', exact: true })).toBeVisible()
  expect(calls.paid).toBe(0)
})

test('editing the research URL invalidates its quote', async ({ page }) => {
  const calls = await fixture(page); await open(page)
  await page.getByLabel('Website URL', { exact: true }).fill('acme.example')
  await page.getByRole('button', { name: 'Get research estimate', exact: true }).click()
  await expect(page.getByLabel('Research estimate')).toBeVisible()
  await page.getByLabel('Website URL', { exact: true }).fill('contoso.example')
  await expect(page.getByLabel('Research estimate')).toBeHidden()
  expect(calls.paid).toBe(0)
})

test('partial audit keeps answers and unknown charges inspectable', async ({ page }) => {
  const calls = await fixture(page, { existing: true, unknownAudit: true }); await open(page)
  await page.getByRole('button', { name: 'Review your first audit' }).click()
  await page.getByRole('button', { name: 'Get audit estimate' }).click()
  await page.getByRole('button', { name: 'Run audit — estimate $0.012' }).click()
  await expect(page.getByText('Charge unconfirmed', { exact: false })).toBeVisible()
  await expect(page.getByText('The upstream reply was interrupted; its charge is unconfirmed.', { exact: false })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Acme product guide' })).toBeVisible()
  expect(calls.paid).toBe(1)
})

test('project edits use revisions and a conflict keeps the draft until explicit replacement', async ({ page }) => {
  const calls = await fixture(page, { existing: true, staleEdit: true }); await open(page)
  await page.getByRole('button', { name: 'Edit brand', exact: true }).click()
  await page.getByLabel('Brand name', { exact: true }).fill('New Acme')
  await page.getByRole('button', { name: 'Choose buyer questions' }).click()
  await page.getByRole('button', { name: 'Save brand profile' }).click()
  await expect(page.getByRole('alert')).toContainText('changed')
  expect(calls.puts).toBe(1)
  await page.getByRole('button', { name: 'Reload saved profile' }).click()
  await page.getByRole('button', { name: 'Replace edits', exact: true }).click()
  await expect(page.getByLabel('Brand name', { exact: true })).toHaveValue('Acme')
  expect(calls.puts).toBe(1)
})

test('read-only projects hide editing and the Lens hash survives Back and reload', async ({ page }) => {
  await fixture(page, { existing: true, readOnly: true }); await open(page)
  await expect(page.getByRole('button', { name: 'Edit brand', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Getting started', exact: true }).click()
  await page.goBack()
  await expect(page.getByRole('heading', { name: 'Lens', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('combobox', { name: 'Brand project', exact: true })).toHaveValue('brand-1')
})

test('Lens stays hidden when its deployment flag is off', async ({ page }) => {
  await fixture(page, { off: true })
  await expect(page.getByRole('button', { name: 'Lens', exact: true })).toHaveCount(0)
  await page.goto('/app#lens')
  await expect(page.getByRole('heading', { name: 'Lens is not enabled' })).toBeVisible()
})

test('setup and results fit a narrow phone without overlapping text', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await fixture(page); await open(page)
  expect(await fitsWidth(page)).toBe(true)
  await manual(page)
  expect(await fitsWidth(page)).toBe(true)
  await page.getByRole('button', { name: 'Save brand profile' }).click()
  await page.getByRole('button', { name: 'Get audit estimate' }).click()
  await page.getByRole('button', { name: 'Run audit — estimate $0.012' }).click()
  await expect(page.getByRole('heading', { name: 'Audit results · Acme' })).toBeVisible()
  expect(await fitsWidth(page)).toBe(true)
  expect(await textCollisions(page, '.lens-header, .lens-metrics>div, .lens-answer summary')).toEqual([])
})


test('viewers can read team projects but have no creation, research or audit controls', async ({ page }) => {
  const calls = await fixture(page, { existing: true, viewer: true }); await open(page)
  await expect(page.getByText('You have read-only access to this team.', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Add a brand', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Edit brand', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Review your first audit', exact: true })).toHaveCount(0)
  expect(calls.paid).toBe(0); expect(calls.quotes).toBe(0)
})


test('blocked browser storage prevents paid submission and preserves the estimate', async ({ page }) => {
  const calls = await fixture(page); await open(page)
  await page.getByLabel('Website URL', { exact: true }).fill('acme.example')
  await page.getByRole('button', { name: 'Get research estimate', exact: true }).click()
  await page.evaluate(() => { Storage.prototype.setItem = () => { throw new DOMException('Quota exceeded', 'QuotaExceededError') } })
  await page.getByRole('button', { name: 'Research my brand — $0.005' }).click()
  await expect(page.getByRole('alert')).toContainText('No paid request was sent')
  await expect(page.getByLabel('Research estimate')).toBeVisible()
  expect(calls.paid).toBe(0)
})

test('changing websites retains the original research receipt and labels its provenance after reload', async ({ page }) => {
  const calls = await fixture(page); await open(page)
  await page.getByLabel('Website URL', { exact: true }).fill('acme.example')
  await page.getByRole('button', { name: 'Get research estimate', exact: true }).click()
  await page.getByRole('button', { name: 'Research my brand — $0.005' }).click()
  await expect(page.getByLabel('Brand name', { exact: true })).toHaveValue('Acme')
  await page.getByLabel('Brand website', { exact: true }).fill('https://different.example')
  await expect(page.getByText('Your current website has not been researched;', { exact: false })).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('Brand website', { exact: true })).toHaveValue('https://different.example')
  await page.getByText('Research sources and receipt', { exact: true }).click()
  await expect(page.getByRole('link', { name: 'About Acme' })).toBeVisible()
  await expect(page.getByText('Researched website: https://acme.example', { exact: true })).toBeVisible()
  expect(calls.paid).toBe(1)
})
