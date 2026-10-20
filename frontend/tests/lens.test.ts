import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ApiError } from '../src/api'
import { lensRequest, microUsd, publicSource } from '../src/lens/client'
import { blankProfile, cleanProfile, createLensState, draftKey, profileProblem, readDraft } from '../src/lens/state'
import type { LensProject, LensQuote, LensRun, LensScope } from '../src/lens/types'

const profile = () => ({ ...blankProfile(), brand: 'Acme', url: 'https://acme.example', questions: ['Which tools help growing teams?'] })
const project = (id = 'project-1'): LensProject => ({ ...profile(), id, revision: 1, can_edit: true, created_at: '2026-01-01T12:00:00Z', updated_at: '2026-01-01T12:00:00Z' })
const quote = (kind: 'audit' | 'onboarding' = 'onboarding'): LensQuote => ({ id: 'run-1', run_id: 'run-1', kind, total_cost_micro: 5000, expires_at: new Date(Date.now() + 60_000).toISOString(), input: {}, note: 'Estimate only.', currency: 'USD' })
const run = (state: LensRun['state'] = 'running'): LensRun => ({ id: 'run-1', kind: 'onboarding', state, created_at: '2026-01-01T12:00:00Z', result: null })
let scope: LensScope
let stored: Map<string, string>
beforeEach(() => {
  stored = new Map()
  vi.stubGlobal('window', { localStorage: { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => stored.set(key, value), removeItem: (key: string) => stored.delete(key) } })
  scope = { user: 'person@example.com', team: 'alpha', enabled: true, headers: { 'X-Treg-Org': 'alpha' } }
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

test('micro prices preserve exact precision and unknown charges never appear as zero', () => {
  expect(microUsd(5_001)).toBe('$0.005001'); expect(microUsd(1_234_567)).toBe('$1.234567')
  expect(microUsd(0)).toBe('$0.00'); expect(microUsd(null)).toBe('Unconfirmed')
  expect(microUsd(Number.NaN)).toBe('Unconfirmed')
})
test('source links allow only ordinary web URLs without embedded credentials', () => {
  expect(publicSource('javascript:alert(1)')).toBeUndefined(); expect(publicSource('https://user:secret@example.com')).toBeUndefined()
  expect(publicSource('https://example.com/article')).toBe('https://example.com/article')
})
test('profile saves strip research metadata and keep question selection bounded', () => {
  const form = { ...profile(), competitors: [{ name: ' Other ', domain: 'other.example', reason: 'A suggestion' }], questions: ['A?', ' A? ', 'B?'] }
  expect(cleanProfile(form).competitors).toEqual([{ name: 'Other', domain: 'other.example' }])
  expect(cleanProfile(form).questions).toEqual(['A?', 'B?']); expect(profileProblem({ ...form, questions: [] })).toContain('at least one')
})
test('drafts are scoped to both user and team and contain no request headers', async () => {
  const request = vi.fn().mockResolvedValue({ projects: [] })
  const lens = createLensState(() => scope, request)
  await lens.load(); lens.state.draft.form.brand = 'Private draft'; lens.persist()
  expect(readDraft(draftKey(scope))?.form.brand).toBe('Private draft')
  expect(readDraft(draftKey({ ...scope, team: 'beta' }))).toBeNull()
  expect(readDraft(draftKey({ ...scope, user: 'other@example.com' }))).toBeNull()
  expect(stored.get(draftKey(scope))).not.toContain('X-Treg')
  lens.pause()
})
test('a disabled feature makes no Lens requests', async () => {
  scope.enabled = false; const request = vi.fn(); const lens = createLensState(() => scope, request)
  await lens.load(); expect(request).not.toHaveBeenCalled(); expect(lens.state.loaded).toBe(false)
})
test('manual setup saves exactly one profile without research or audit calls', async () => {
  const request = vi.fn().mockResolvedValueOnce({ projects: [] }).mockResolvedValueOnce(project())
  const lens = createLensState(() => scope, request); await lens.load(); lens.manual()
  lens.state.draft.form = profile(); await lens.save()
  expect(request.mock.calls.map(call => call[0])).toEqual(['/lens/projects', '/lens/projects'])
  expect(request.mock.calls[1]![2]).toMatchObject({ brand: 'Acme', request_id: expect.any(String) })
  expect(lens.state.draft.mode).toBe('workspace'); expect(lens.state.auditOpen).toBe(true)
})
test('lost create acknowledgement preserves an idempotent save and does not mark setup completed', async () => {
  const request = vi.fn().mockResolvedValueOnce({ projects: [] }).mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValueOnce(project())
  const lens = createLensState(() => scope, request); await lens.load(); lens.state.draft.form = profile(); await lens.save()
  expect(lens.state.draft.uncertainSave).toBe(true); expect(lens.state.draft.mode).toBe('setup')
  const first = request.mock.calls[1]![2]; await lens.save()
  expect(request.mock.calls[2]![2]).toEqual(first); expect(lens.state.draft.uncertainSave).toBe(false)
})
test('stale profile edits retain the draft and never retry with a newer revision', async () => {
  const request = vi.fn().mockResolvedValueOnce({ projects: [project()] }).mockResolvedValueOnce({ project: project(), runs: [] })
    .mockResolvedValueOnce({ project: { ...project(), revision: 2, brand: 'Their change' }, runs: [] })
  const lens = createLensState(() => scope, request); await lens.load(); lens.begin(lens.state.project!); lens.state.draft.form.brand = 'My change'; await lens.save()
  expect(lens.state.error).toContain('changed'); expect(lens.state.draft.form.brand).toBe('My change')
  expect(request.mock.calls.some(call => call[3] === 'PUT')).toBe(false)
})
test('editing inputs clears any previous quote', async () => {
  const request = vi.fn().mockResolvedValueOnce({ projects: [] }).mockResolvedValueOnce(quote())
  const lens = createLensState(() => scope, request); await lens.load(); lens.state.draft.form.url = 'acme.example'; await lens.estimate('onboarding')
  expect(lens.state.quote).not.toBeNull(); lens.state.draft.form.url = 'other.example'; lens.changed(); expect(lens.state.quote).toBeNull()
})
test('paid submission records the recovery id before POST and lost replies never trigger another POST', async () => {
  const request = vi.fn().mockImplementation(async (path, _headers, _body, method) => {
    if (path === '/lens/projects') return { projects: [] }
    if (path === '/lens/quotes') return quote()
    if (method === 'POST') { expect(readDraft(draftKey(scope))?.pending?.id).toBe('run-1'); throw new Error('Connection lost') }
    return { ...run('quoted'), quote: quote() }
  })
  const lens = createLensState(() => scope, request); await lens.load(); await lens.estimate('onboarding'); await lens.start()
  expect(lens.state.draft.pending?.id).toBe('run-1'); expect(lens.state.error).toContain('status')
  const resumed = createLensState(() => scope, request); await resumed.load()
  expect(request.mock.calls.filter(call => call[0] === '/lens/runs' && call[3] === 'POST')).toHaveLength(1)
  expect(resumed.state.quote?.id).toBe('run-1'); expect(resumed.state.draft.pending).toBeNull()
  lens.pause(); resumed.pause()
})
test('a late project response from the prior team cannot replace the new team', async () => {
  let resolveOld: (data: unknown) => void = () => {}
  const request = vi.fn().mockImplementation((_path, headers) => headers['X-Treg-Org'] === 'alpha'
    ? new Promise(resolve => { resolveOld = resolve }) : Promise.resolve({ projects: [] }))
  const lens = createLensState(() => scope, request); const old = lens.load()
  scope = { ...scope, team: 'beta', headers: { 'X-Treg-Org': 'beta' } }; await lens.load()
  resolveOld({ projects: [project()] }); await old
  expect(lens.state.projects).toEqual([]); expect(lens.state.project).toBeNull()
})
test('partial onboarding results retain errors and unconfirmed billing for review', async () => {
  const partial: LensRun = { ...run('failed'), result: { kind: 'onboarding', input: { url: 'https://acme.example' }, results: [],
    research: { brand: 'Acme', description: 'Widgets', competitors: [], questions: ['Which widgets?'], sources: [] },
    errors: [{ detail: 'The provider returned partial evidence.' }], total_cost_micro: null, known_cost_micro: 0, cost_known: false, completed_at: '2026-01-01T12:01:00Z' } }
  const request = vi.fn().mockResolvedValueOnce({ projects: [] }).mockResolvedValueOnce(quote()).mockResolvedValueOnce(partial)
  const lens = createLensState(() => scope, request); await lens.load(); await lens.estimate('onboarding'); await lens.start()
  expect(lens.state.draft.step).toBe('brand'); expect(lens.state.draft.receipt?.result?.cost_known).toBe(false)
  expect(lens.state.draft.receipt?.result?.errors).toHaveLength(1); expect(lens.state.draft.pending).toBeNull()
})
test('Lens transport never retries an HTML edge rejection of a mutation', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('<html>denied</html>', { status: 403, headers: { 'Content-Type': 'text/html' } }))
  vi.stubGlobal('fetch', fetch)
  await expect(lensRequest('/lens/runs', { 'X-Treg-Org': 'alpha' }, { quote_id: 'one' }, 'POST')).rejects.toBeInstanceOf(ApiError)
  expect(fetch).toHaveBeenCalledTimes(1)
})

test('an old run read cannot overwrite a newly selected project or clear another pending run', async () => {
  let resolveOld: (value: unknown) => void = () => {}
  const other = project('project-2')
  const request = vi.fn().mockImplementation(async path => {
    if (path === '/lens/projects') return { projects: [project(), other] }
    if (path === '/lens/projects/project-1') return { project: project(), runs: [] }
    if (path === '/lens/projects/project-2') return { project: other, runs: [] }
    return new Promise(resolve => { resolveOld = resolve })
  })
  const lens = createLensState(() => scope, request); await lens.load()
  const oldRead = lens.readRun('old-run'); await lens.openProject('project-2')
  lens.state.draft.pending = { id: 'new-run', kind: 'audit' }; lens.state.busy = 'run'
  resolveOld({ ...run('completed'), id: 'old-run', kind: 'audit', project_id: 'project-1' }); await oldRead
  expect(lens.state.project?.id).toBe('project-2'); expect(lens.state.activeRun).toBeNull(); expect(lens.state.runs).toEqual([])
  expect(lens.state.draft.pending?.id).toBe('new-run'); expect(lens.state.busy).toBe('run')
})
test('an older response cannot replace a history run selected while it was in flight', async () => {
  let resolveOld: (value: unknown) => void = () => {}
  const request = vi.fn().mockImplementation(async path => path === '/lens/projects' ? { projects: [project()] }
    : path === '/lens/projects/project-1' ? { project: project(), runs: [] } : new Promise(resolve => { resolveOld = resolve }))
  const lens = createLensState(() => scope, request); await lens.load()
  const oldRead = lens.readRun('old-run')
  lens.selectRun({ ...run('completed'), id: 'chosen-run', kind: 'audit', project_id: 'project-1' })
  resolveOld({ ...run('completed'), id: 'old-run', kind: 'audit', project_id: 'project-1' }); await oldRead
  expect(lens.state.activeRun?.id).toBe('chosen-run')
})
test('unavailable browser storage blocks paid submission before sending a request', async () => {
  const request = vi.fn().mockResolvedValueOnce({ projects: [] }).mockResolvedValueOnce(quote())
  const lens = createLensState(() => scope, request); await lens.load(); await lens.estimate('onboarding')
  window.localStorage.setItem = () => { throw new Error('Quota exceeded') }
  await lens.start()
  expect(request.mock.calls.map(call => call[0])).toEqual(['/lens/projects', '/lens/quotes'])
  expect(lens.state.error).toContain('No paid request was sent'); expect(lens.state.draft.pending).toBeNull(); expect(lens.state.quote?.id).toBe('run-1')
})
test('saved pending audits recover after their selected project has loaded', async () => {
  const completed = { ...run('completed'), kind: 'audit', project_id: 'project-1' }
  const request = vi.fn().mockImplementation(async path => path === '/lens/projects' ? { projects: [project()] }
    : path === '/lens/projects/project-1' ? { project: project(), runs: [] } : completed)
  const first = createLensState(() => scope, request); await first.load()
  first.state.draft.pending = { id: 'run-1', kind: 'audit' }; first.persist(); first.pause()
  const resumed = createLensState(() => scope, request); await resumed.load()
  expect(resumed.state.draft.pending).toBeNull(); expect(resumed.state.activeRun?.state).toBe('completed'); expect(resumed.state.runs).toHaveLength(1)
})
test('a lost PUT acknowledgement recognizes the server-normalized profile without resubmitting', async () => {
  const normalized = { ...project(), revision: 2, url: 'https://new.example/', description: 'Project tools', competitors: [{ name: 'Other', domain: 'other.example' }], questions: ['A question?'] }
  let writes = 0
  const request = vi.fn().mockImplementation(async (path, _headers, _body, method) => {
    if (path === '/lens/projects') return { projects: [project()] }
    if (method === 'PUT') { writes++; throw new Error('Connection lost') }
    return { project: writes ? normalized : project(), runs: [] }
  })
  const lens = createLensState(() => scope, request); await lens.load(); lens.begin(lens.state.project!)
  lens.state.draft.form = { ...profile(), url: 'new.example?campaign=one#section', description: 'Project   tools', competitors: [{ name: 'Other', domain: 'https://www.other.example/solutions' }], questions: ['A question?', 'a question?'] }
  await lens.save(); expect(lens.state.draft.uncertainSave).toBe(true)
  await lens.save(); expect(lens.state.draft.mode).toBe('workspace'); expect(writes).toBe(1); expect(lens.state.error).toBe('')
})
test('viewers cannot invoke Lens writes through state methods', async () => {
  scope.canWrite = false
  const request = vi.fn().mockResolvedValue({ projects: [] }); const lens = createLensState(() => scope, request)
  await lens.load(); lens.state.draft.form = profile(); lens.state.quote = quote()
  await lens.estimate('onboarding'); await lens.start(); await lens.save()
  expect(request).toHaveBeenCalledTimes(1)
})
