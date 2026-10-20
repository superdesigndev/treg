import { reactive } from 'vue'
import { ApiError } from '../api'
import { storageGet, storageSet, storageRemove } from '../state/storage.js'
import { lensRequest, lensError } from './client'
import { LENS_ENGINES, type LensDraft, type LensProfile, type LensProject, type LensQuote, type LensRun, type LensScope, type LensEngine } from './types'

export const blankProfile = (): LensProfile => ({ brand: '', url: '', description: '', competitors: [], questions: [''], engines: LENS_ENGINES.map(e => e.id) })
export const draftKey = (scope: LensScope) => `treg-lens:v1:${encodeURIComponent(scope.user)}:${encodeURIComponent(scope.team)}`
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
const draft = (): LensDraft => ({ version: 1, requestId: crypto.randomUUID(), mode: 'workspace', step: 'website', form: blankProfile(), selectedQuestions: [true], editingId: null, editingRevision: null, selectedId: null, appliedResearchId: null, pending: null, receipt: null, uncertainSave: false })
const terminal = (run: LensRun) => !['running', 'quoted'].includes(run.state)
export function canonicalWebsite(value: string): string {
  try { const url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(value.trim()) ? value.trim() : `https://${value.trim()}`); url.search = ''; url.hash = ''; url.hostname = url.hostname.replace(/\.$/, ''); return url.href } catch { return value.trim() }
}
function canonicalProfile(value: LensProfile) {
  const plain = (text: string) => text.replace(/<[^>]*>/g, ' ').replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim()
  const domain = (url: string) => { try { return new URL(canonicalWebsite(url)).hostname.replace(/^www\./, '') } catch { return url.trim().toLowerCase() } }
  const brand = plain(value.brand), seen = new Set([brand.toLowerCase()]), questions = new Set<string>()
  return { ...cleanProfile(value), brand, description: plain(value.description), url: canonicalWebsite(value.url),
    competitors: value.competitors.flatMap(c => { const name = plain(c.name); const host = c.domain ? domain(c.domain) : ''; if (!name || seen.has(name.toLowerCase()) || host === domain(value.url)) return []; seen.add(name.toLowerCase()); return [{ name, ...(host ? { domain: host } : {}) }] }),
    questions: value.questions.flatMap(q => { const text = plain(q); if (!text || questions.has(text.toLowerCase())) return []; questions.add(text.toLowerCase()); return [text] }), engines: [...new Set(value.engines)] }
}
const sameProfile = (a: LensProfile, b: LensProfile) => JSON.stringify(canonicalProfile(a)) === JSON.stringify(canonicalProfile(b))

export function cleanProfile(form: LensProfile): LensProfile {
  return {
    brand: form.brand.trim(), url: form.url.trim(), description: form.description.trim(),
    competitors: form.competitors.map(c => ({ name: c.name.trim(), ...(c.domain?.trim() ? { domain: c.domain.trim() } : {}) })).filter(c => c.name),
    questions: [...new Set(form.questions.map(q => q.trim()).filter(Boolean))], engines: [...form.engines],
  }
}
export function profileProblem(form: LensProfile): string {
  const clean = cleanProfile(form)
  if (!clean.url) return 'Enter your brand’s public website URL.'
  if (!clean.brand) return 'Enter the brand name buyers know you by.'
  if (!clean.questions.length) return 'Add at least one buyer question to track.'
  if (clean.questions.length > 8) return 'Choose up to eight buyer questions.'
  if (!clean.engines.length) return 'Select at least one answer engine.'
  return ''
}
export function readDraft(key: string): LensDraft | null {
  try {
    const text = storageGet(key)
    if (!text || text.length > 250_000) return null
    const value = JSON.parse(text) as LensDraft
    if (value.version !== 1 || typeof value.requestId !== 'string' || !['workspace', 'setup'].includes(value.mode) || !['website', 'brand', 'questions'].includes(value.step)) return null
    const f = value.form
    if (!f || typeof f.brand !== 'string' || typeof f.url !== 'string' || typeof f.description !== 'string' || !Array.isArray(f.competitors) || !Array.isArray(f.questions) || !Array.isArray(f.engines)) return null
    if (f.brand.length > 100 || f.url.length > 2048 || f.description.length > 1200 || f.competitors.length > 8 || f.questions.length > 8 || f.questions.some(q => typeof q !== 'string' || q.length > 500) || f.competitors.some(c => typeof c?.name !== 'string')) return null
    if (f.engines.some(e => !LENS_ENGINES.some(known => known.id === e))) return null
    if (typeof value.appliedResearchId !== 'string') value.appliedResearchId = null
    if (!Array.isArray(value.selectedQuestions)) value.selectedQuestions = f.questions.map(() => true)
    if (value.pending && (typeof value.pending.id !== 'string' || !['onboarding', 'audit'].includes(value.pending.kind))) return null
    return value
  } catch { return null }
}

/** One instance per mounted Lens page; no cross-team data or credential is held globally. */
export function createLensState(scope: () => LensScope, request = lensRequest, pollDelay = 1500) {
  const state = reactive({
    draft: draft(), projects: [] as LensProject[], project: null as LensProject | null, runs: [] as LensRun[],
    activeRun: null as LensRun | null, quote: null as LensQuote | null, engines: LENS_ENGINES.map(e => e.id) as LensEngine[],
    loaded: false, busy: '' as '' | 'loading' | 'quote' | 'run' | 'save', error: '', notice: '', auditOpen: false,
  })
  let generation = 0
  let key = ''
  let timer: ReturnType<typeof setTimeout> | undefined
  let readNumber = 0
  let runTicket = 0
  const current = (at: number) => at === generation
  function persist() { if (!key) return false; const value = JSON.stringify(state.draft); storageSet(key, value); return storageGet(key) === value }
  function clearQuote() { state.quote = null }
  function cancelRunReads() { runTicket++; clearTimeout(timer); timer = undefined }
  function pause() { generation++; cancelRunReads() }
  function snapshot() { return { generation, key, projectId: state.project?.id || null, headers: { ...scope().headers } } }
  function savedRun(run: LensRun, at: ReturnType<typeof snapshot>) {
    // Persist a completed response even when the user switched teams while the write was in flight.
    // It belongs to that old team only. A read on return can recover it if this tab closed first.
    if (!current(at.generation)) {
      const old = readDraft(at.key)
      if (old?.pending?.id === run.id) { old.receipt = run; if (terminal(run)) old.pending = null; storageSet(at.key, JSON.stringify(old)) }
      return
    }
    const ownsPending = state.draft.pending?.id === run.id
    const ownsProject = run.kind === 'onboarding' || (run.project_id === state.project?.id && at.projectId === state.project?.id)
    if (ownsProject) state.activeRun = run
    if (run.kind === 'onboarding' && (ownsPending || state.draft.receipt?.id === run.id)) state.draft.receipt = run
    if (terminal(run)) {
      if (ownsPending) state.draft.pending = null
      if (ownsPending || (!state.draft.pending && state.busy === 'run')) state.busy = ''
      if (run.kind === 'onboarding' && run.result?.research && (ownsPending || (state.draft.receipt?.id === run.id && state.draft.appliedResearchId !== run.id))) {
        const research = run.result.research
        state.draft.form = { ...state.draft.form, brand: research.brand, description: research.description,
          competitors: clone(research.competitors).slice(0, 8), questions: research.questions.length ? [...research.questions].slice(0, 8) : [''] }
        state.draft.appliedResearchId = run.id
        state.draft.selectedQuestions = state.draft.form.questions.map(() => true)
        state.draft.step = 'brand'; state.draft.mode = 'setup'
        state.notice = 'Research is ready. Check your brand and competitors before choosing questions.'
      }
      if (run.kind === 'audit' && ownsProject) {
        state.runs = [run, ...state.runs.filter(r => r.id !== run.id)]
        state.auditOpen = false
      }
      if (run.state !== 'completed' && (ownsPending || ownsProject)) state.error = run.error || 'This run could not finish. Review the available results and charge details below.'
    }
    persist()
  }
  async function readRun(id: string, at = snapshot(), count = 0, ticket = ++runTicket) {
    if (count === 0) { clearTimeout(timer); timer = undefined }
    try {
      const run = await request<LensRun>(`/lens/runs/${encodeURIComponent(id)}`, at.headers)
      if (!current(at.generation) || ticket !== runTicket || at.projectId !== (state.project?.id || null)) return
      savedRun(run, at)
      if (run.state === 'quoted') {
        state.busy = ''; state.notice = 'This request has not started. Review its estimate before submitting again.'
        if (state.draft.pending?.id === run.id) { state.draft.pending = null; state.quote = run.quote || null; persist() }
      } else if (run.state === 'running' && count < 100) {
        state.busy = 'run'; timer = setTimeout(() => { void readRun(id, at, count + 1, ticket) }, pollDelay)
      } else if (run.state === 'running') {
        state.busy = ''; state.notice = 'The run is still in progress. You can leave this page and check its status later.'
      }
    } catch (error) {
      if (!current(at.generation) || ticket !== runTicket || at.projectId !== (state.project?.id || null)) return
      state.busy = ''; state.error = `${lensError(error)} No new paid request was sent.`
    }
  }
  async function openProject(id: string, preserveDraft = false) {
    cancelRunReads()
    const at = snapshot(); const ticket = ++readNumber
    state.busy = 'loading'; state.error = ''; clearQuote()
    try {
      const data = await request<{ project: LensProject; runs: LensRun[] }>(`/lens/projects/${encodeURIComponent(id)}`, at.headers)
      if (!current(at.generation) || ticket !== readNumber) return
      state.project = data.project; state.runs = data.runs; state.activeRun = data.runs[0] || null
      state.engines = [...data.project.engines]; state.draft.selectedId = id
      if (!preserveDraft) { state.draft.mode = 'workspace'; state.draft.editingId = null }
      persist()
    } catch (error) { if (current(at.generation) && ticket === readNumber) state.error = lensError(error) }
    finally { if (current(at.generation) && ticket === readNumber) state.busy = '' }
  }
  async function load() {
    pause(); readNumber++; const now = scope(); key = draftKey(now)
    state.projects = []; state.project = null; state.runs = []; state.activeRun = null; state.quote = null
    state.error = ''; state.notice = ''; state.loaded = false; state.auditOpen = false; state.engines = LENS_ENGINES.map(e => e.id)
    state.draft = readDraft(key) || draft()
    if (!now.enabled || !now.user || !now.team) { state.busy = ''; return }
    state.busy = 'loading'; const at = snapshot()
    try {
      const data = await request<{ projects: LensProject[] }>('/lens/projects', at.headers)
      if (!current(at.generation)) return
      state.projects = data.projects; state.loaded = true
      const selected = data.projects.find(p => p.id === state.draft.selectedId) || data.projects[0]
      if (selected) await openProject(selected.id, state.draft.mode === 'setup')
      else if (state.draft.mode !== 'setup') { state.draft.mode = 'setup'; state.draft.step = 'website'; persist() }
      if (!current(at.generation)) return
      state.busy = ''
      if (state.draft.pending) { state.notice = 'A previous request needs a status check. It will not be submitted again.'; await readRun(state.draft.pending.id, snapshot()) }
      else if (state.draft.receipt?.result?.research && state.draft.step === 'website' && state.draft.appliedResearchId !== state.draft.receipt.id && canonicalWebsite(String(state.draft.receipt.result.input.url || '')) === canonicalWebsite(state.draft.form.url)) savedRun(state.draft.receipt, snapshot())
    } catch (error) { if (current(at.generation)) { state.error = lensError(error); state.busy = '' } }
  }
  function begin(project?: LensProject) {
    if (state.draft.pending || state.busy || scope().canWrite === false) return
    cancelRunReads()
    state.draft = { ...draft(), mode: 'setup', step: project ? 'brand' : 'website', selectedId: state.draft.selectedId,
      editingId: project?.id || null, editingRevision: project?.revision || null, form: project ? cleanProfile(project) : blankProfile(), selectedQuestions: project ? project.questions.map(() => true) : [true] }
    state.error = ''; state.notice = ''; clearQuote(); persist()
  }
  function changed() { clearQuote(); state.error = ''; persist() }
  function manual() { state.draft.step = 'brand'; clearQuote(); state.error = ''; persist() }
  async function estimate(kind: 'onboarding' | 'audit') {
    if (state.busy || state.draft.pending || scope().canWrite === false) return
    cancelRunReads()
    const at = snapshot(); state.busy = 'quote'; state.error = ''; clearQuote()
    const input = kind === 'onboarding' ? { kind, url: state.draft.form.url.trim() } : {
      kind, project_id: state.project?.id, project_revision: state.project?.revision, engines: [...state.engines],
    }
    try {
      const quote = await request<LensQuote>('/lens/quotes', at.headers, input, 'POST')
      if (current(at.generation)) state.quote = quote
    } catch (error) { if (current(at.generation)) state.error = lensError(error) }
    finally { if (current(at.generation)) state.busy = '' }
  }
  async function start() {
    const quote = state.quote
    if (!quote || state.busy || state.draft.pending || scope().canWrite === false) return
    if (Date.parse(quote.expires_at) <= Date.now()) { clearQuote(); state.error = 'This estimate has expired. Get a new estimate before continuing.'; return }
    cancelRunReads()
    const at = snapshot(); state.busy = 'run'; state.error = ''; state.notice = ''
    state.draft.pending = { id: quote.run_id || quote.id, kind: quote.kind }
    if (!persist()) {
      state.draft.pending = null; state.busy = ''
      state.error = 'Your browser could not save this request’s recovery ID. Enable site storage or free some space, then try again. No paid request was sent.'
      return
    }
    clearQuote()
    try {
      const run = await request<LensRun>('/lens/runs', at.headers, { quote_id: quote.id }, 'POST')
      savedRun(run, at)
      if (current(at.generation) && run.state === 'running') await readRun(run.id, at)
    } catch (error) {
      if (!current(at.generation)) return
      // Even an error response is resolved by the durable run record, never by a second POST.
      state.busy = ''; state.error = `${lensError(error)} Check this request’s status before starting anything else.`
    }
  }
  async function save() {
    if (state.busy || state.draft.pending || scope().canWrite === false) return
    const problem = profileProblem(state.draft.form)
    if (problem) { state.error = problem; return }
    const at = snapshot(); const editing = state.draft.editingId; const profile = cleanProfile(state.draft.form)
    state.busy = 'save'; state.error = ''; state.draft.uncertainSave = true; persist()
    try {
      let project: LensProject
      if (editing) {
        // A lost PUT acknowledgement is resolved with a read before any deliberate retry.
        const latest = await request<{ project: LensProject; runs: LensRun[] }>(`/lens/projects/${encodeURIComponent(editing)}`, at.headers)
        if (!current(at.generation)) return
        if (latest.project.revision !== state.draft.editingRevision) {
          if (!sameProfile(latest.project, profile)) throw new ApiError(409)
          project = latest.project
        } else project = await request<LensProject>(`/lens/projects/${encodeURIComponent(editing)}`, at.headers,
          { ...profile, revision: state.draft.editingRevision }, 'PUT')
      } else project = await request<LensProject>('/lens/projects', at.headers, { ...profile, request_id: state.draft.requestId }, 'POST')
      if (!current(at.generation)) return
      state.draft.uncertainSave = false; state.draft.mode = 'workspace'; state.draft.editingId = null; state.draft.selectedId = project.id
      state.project = project; state.engines = [...project.engines]; state.projects = [project, ...state.projects.filter(p => p.id !== project.id)]
      state.runs = editing ? state.runs : []; state.activeRun = editing ? state.activeRun : null
      state.auditOpen = !editing; state.notice = 'Brand profile saved. Review an estimate whenever you are ready for an audit.'; persist()
    } catch (error) {
      if (current(at.generation)) { state.error = lensError(error); if (error instanceof ApiError && [400, 403, 409, 422].includes(error.status)) state.draft.uncertainSave = false; persist() }
    } finally { if (current(at.generation)) state.busy = '' }
  }
  function selectRun(run: LensRun) {
    if (state.busy || state.draft.pending || run.project_id !== state.project?.id) return
    cancelRunReads(); state.activeRun = run
    if (run.state === 'running') void readRun(run.id)
  }
  function leaveSetup() {
    if (state.busy || state.draft.pending || !state.project) return
    state.draft.mode = 'workspace'; state.draft.editingId = null; state.draft.uncertainSave = false; state.error = ''; persist()
  }
  function discardDraft() { storageRemove(key); state.draft = draft(); state.quote = null; state.error = ''; void load() }
  return { state, load, pause, persist, changed, begin, manual, estimate, start, save, openProject, readRun, leaveSetup, discardDraft, clearQuote, selectRun }
}
