<script setup lang="ts">
import '@fontsource-variable/geist/index.css'
// The first-run flow behind `onboarding_v2` (docs/context/interface/onboarding.md): about a minute
// from sign-in to a connected agent, in three sheets.
//   1. Setting up: the server makes the team and looks the user up (GitHub, company, site), row by row.
//   2. What should your agent do first: five ranked tasks, each a sentence with one editable blank.
//   3. Your first call: the chosen task runs for real on the team's credit, its answer drawn the way
//      it is read, and the same tool handed to the agent.
// The dock under the sheets shows the step, the team's credit and the step's buttons.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useDashboard } from '../state/context'
import ConnectBlock from './ConnectBlock.vue'
import ResultView from './ResultView.vue'
import { buildCalls, cliLine, floor2, fmtCost, runCall, runPreviewCall, type CallResult, type LibTask, type RankedTask } from './calls'
import { extract, type Extracted } from './extract'
import './onboarding.css'

const d = useDashboard()
const emit = defineEmits<{ done: []; fallback: [] }>()
// Preview (`/app#onboarding-preview`): the same flow for any email, for the people who tune it. No
// team is made and nobody is marked onboarded; the lookup runs for that email and the first call is
// a house call, against a simulated $1 of credit.
const props = defineProps<{ preview?: boolean }>()
const previewEmail = ref('')
const previewErr = ref('')
const previewBusy = ref(false)
const previewSpent = ref(0)
// A preview is not a signup: it reports nothing to product analytics.
const track = (name: string, props_: Record<string, unknown>) => { if (!props.preview) d.track(name, props_) }

type Step = { key: string; state: string; detail: string }
type ViewResp = {
  status: 'none' | 'running' | 'ready' | 'done' | 'failed'; team?: { slug: string; name: string } | null; steps?: Step[]
  id?: string; email?: string
  tasks?: RankedTask[]; shown?: number; preselect?: string | null; library: LibTask[]; default_rank: string[]
  pending?: string[]
  ask?: boolean; here_for?: string | null; use_cases?: { key: string; label: string }[]
}

const SEQ = ['diag', 'task', 'go'] as const
type Sheet = typeof SEQ[number]
const STEP_LABEL: Record<Sheet, string> = { diag: 'Setting up', task: 'Your first task', go: 'Your first call' }
const ROWS: [string, string][] = [
  ['github', 'Looking for your GitHub'], ['company', 'Looking up your company'],
  ['homepage', 'Reading your site'], ['personal', 'Tailoring your first tasks'],
]
const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches

const sheet = ref<Sheet>('diag')
const data = ref<ViewResp | null>(null)
const skipped = ref(false)
const picked = ref<string | null>(null)
const edits = reactive<Record<string, string>>({})
const showAll = ref(false)
let pollTimer = 0

// ------------------------------------------------------------------------------- the lookup
async function begin() {
  try {
    let v: ViewResp = await d.api('/onboarding')
    if (v.status === 'none') {
      v = await d.api('/onboarding/start', { method: 'POST' })
      track('onboarding_team_created', { team: v.team?.slug, flow: 'v2' })
      // Google Ads `treg Signup (web)` (gtag.js), as welcomeCreate fires it. This response carries
      // the slug, not the org id; a team goes through one flow only, so either key is stable per team.
      try { (window as any).tregSignupConversion?.(v.team?.slug) } catch { /* never break onboarding */ }
    }
    data.value = v
    await d.loadAll()
    if (v.team && d.activeSlugNow !== v.team.slug) d.switchOrg({ slug: v.team.slug })
    d.analyticsIdentify?.()
    poll()
  } catch (e: any) {
    if (e?.status === 409) { finish(false); return }   // an account that already has a team
    // Anything else (a refused sign-up, a network error): the team-name modal, which says why and
    // lets the user name the team, never a screen with no way out.
    const w = window as unknown as { posthog?: { captureException?: (e: Error, p: Record<string, unknown>) => void } }
    w.posthog?.captureException?.(new Error('onboarding start failed: ' + (e?.detail || e?.status || 'network')), {})
    emit('fallback')
  }
}

async function startPreview() {
  previewErr.value = ''; previewBusy.value = true
  try {
    data.value = await d.api('/onboarding/preview', { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email: previewEmail.value.trim() }) })
    poll()
  } catch (e: any) {
    previewErr.value = e?.detail || 'Could not start the preview (HTTP ' + (e?.status || '?') + ').'
  } finally { previewBusy.value = false }
}

function poll() {
  clearTimeout(pollTimer)
  // `ready`: the tasks can be shown, and their inputs keep improving until `done`
  if (data.value?.status !== 'running' && data.value?.status !== 'ready') return
  const path = props.preview ? '/onboarding/preview/' + encodeURIComponent(data.value.id || '') : '/onboarding'
  pollTimer = window.setTimeout(async () => {
    try { data.value = await d.api(path) } catch { /* the next poll tries again */ }
    poll()
  }, 400)
}

const lookupDone = computed(() => !!data.value && data.value.status !== 'running' && data.value.status !== 'none')
// What we already know: one card per fact, in the order they land (the server shapes them, see
// first_run._facts), and one quiet card for the step still running.
type Fact = { key: string; label: string; value: string; note: string; avatar?: string }
const facts = computed<Fact[]>(() => (data.value as any)?.facts || [])
const looking = computed(() => {
  const steps = data.value?.steps || []
  const s = ROWS.find(([key]) => { const st = steps.find((x) => x.key === key); return !st || st.state === 'running' })
  if (!s || (lookupDone.value && s[0] !== 'personal')) return ''
  return lookupDone.value ? 'Checking ideas against real results' : s[1]
})
// One object per kind of fact (media/onboarding/<key>.webp), generated once for every user.
// The company's mark is its site's own icon, from its host; one that fails to load falls back to the letter.
const brokenMark = ref(false)
const ART = new Set(['size', 'location', 'industry', 'founded', 'site', 'search', 'competitor', 'tiktok', 'company'])
const nothingFound = computed(() => lookupDone.value && !facts.value.length && !data.value?.ask)
// Nothing public grounded a task: one question, and the answer leads the tasks.
const answering = ref('')
async function answer(key: string) {
  if (answering.value || data.value?.here_for === key) return
  answering.value = key
  try {
    const path = props.preview ? `/onboarding/preview/${data.value?.id}/answer` : '/onboarding/answer'
    data.value = await d.api(path, { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ here_for: key }) })
    picked.value = data.value?.preselect || picked.value
    track('onboarding_v2_here_for', { here_for: key })
  } catch { /* the tasks keep their order; the question stays */ }
  answering.value = ''
}
// A quoted phrase (a search term, a topic) reads on one line only across two tiles.
const wide = (f: Fact) => ['intro', 'search', 'tiktok', 'site', 'company'].includes(f.key) || f.value.length > 16 || f.note.length > 48
// No row ends in a gap: place the tiles as the grid's dense flow does, then widen the last tile
// of each row over whatever it left empty. Worked out for the 4-column grid and the
// 2-column one, each read by its own media rule.
function fill(spans: number[], cols: number): number[] {
  const rows: boolean[][] = []
  const at = spans.map((s) => {
    for (let r = 0; ; r++) {
      const row = (rows[r] ||= Array(cols).fill(false))
      for (let c = 0; c + s <= cols; c++) {
        if (row.slice(c, c + s).every((x) => !x)) { row.fill(true, c, c + s); return { r, c } }
      }
    }
  })
  const out = [...spans]
  rows.forEach((row, r) => {
    const end = at.map((p, i) => ({ ...p, i })).filter((p) => p.r === r).sort((a, b) => b.c - a.c)[0]
    if (end && row.lastIndexOf(true) === end.c + spans[end.i] - 1) out[end.i] = cols - end.c
  })
  return out
}
const spans = computed(() => {
  const s = facts.value.map((f) => (wide(f) ? 2 : 1))
  if (looking.value || !lookupDone.value) return s.map((n) => ({ '--c4': `span ${n}`, '--c2': `span ${n}` }))
  const four = fill(s, 4), two = fill(s, 2)
  return s.map((_, i) => ({ '--c4': `span ${four[i]}`, '--c2': `span ${two[i]}` }))
})

// ------------------------------------------------------------------------------- the tasks
const library = computed(() => Object.fromEntries((data.value?.library || []).map((t) => [t.id, t])) as Record<string, LibTask>)
// The order is fixed when the task sheet opens: inputs keep landing while it is read, the cards
// never move under the reader.
const frozenOrder = ref<string[] | null>(null)
const ranked = computed<RankedTask[]>(() => {
  const v = data.value
  if (v?.tasks?.length && !skipped.value) {
    const order = frozenOrder.value
    return order ? [...v.tasks].sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id)) : v.tasks
  }
  return (v?.default_rank || []).filter((id) => library.value[id]).map((id) => {
    const t = library.value[id]
    return { id, value: t.example.value, note: t.example.note, example: true, score: null, recommended: false }
  })
})
const SHOWN = computed(() => data.value?.shown || 5)
const rankedOf = (id: string) => ranked.value.find((r) => r.id === id)
// What the input shows: the user's text as typed (even empty), else the filled-in value.
const rawSlot = (id: string) => edits[id] ?? rankedOf(id)?.value ?? ''
const slot = (id: string) => (edits[id] ?? rankedOf(id)?.value ?? '').trim() || rankedOf(id)?.value || ''
const edited = (id: string) => slot(id) !== rankedOf(id)?.value
const tplParts = (id: string) => (library.value[id]?.tpl || '{}').split('{}')
const parses = (id: string) => !!library.value[id] && !!buildCalls(library.value[id], slot(id))
// While the checks still run (`ready`), a task still on its example may yet get the user's own input.
const finding = (r: RankedTask) =>
  data.value?.status === 'ready' && r.example && !!data.value.pending?.includes(r.id) && !edited(r.id)
// The note under an input says where it came from and that it is the user's to change.
const capital = (t: string) => t.charAt(0).toUpperCase() + t.slice(1)
const noteFor = (r: RankedTask) => !parses(r.id) ? 'Write it like: ' + library.value[r.id]?.example.value
  : edited(r.id) ? 'Your own input.' : finding(r) ? 'Finding one for you…'
  : r.example ? 'An example. Type your own to try it.' : capital(r.note) + '. Change it to try another.'

function choose(id: string, how: 'click' | 'key' | 'arrow') {
  picked.value = id
  if (how === 'key') advance()
}
function onCardKey(e: KeyboardEvent, id: string) {
  if ((e.target as HTMLElement).closest('.oe-input')) return
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(id, 'key'); return }
  if (['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft'].includes(e.key)) {
    e.preventDefault()
    const list = ranked.value.slice(0, showAll.value ? undefined : SHOWN.value)
    const i = list.findIndex((r) => r.id === id)
    const next = list[(i + (e.key === 'ArrowDown' || e.key === 'ArrowRight' ? 1 : list.length - 1)) % list.length]
    choose(next.id, 'arrow')
    nextTick(() => document.querySelector<HTMLElement>(`.ob2 [data-opt="${next.id}"]`)?.focus())
  }
}

// ------------------------------------------------------------------------------- the sheets
const sheetIdx = computed(() => SEQ.indexOf(sheet.value))
function goTo(s: Sheet) {
  sheet.value = s
  nextTick(() => {
    document.querySelector<HTMLElement>('.ob2 .sheet.cur .sh-title')?.focus({ preventScroll: true })
    document.querySelector('.ob2')?.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' })
  })
}
function diagContinue(skip = false) {
  skipped.value = skip && !lookupDone.value
  track('onboarding_v2_setup_done', { skipped: skipped.value, steps: (data.value?.steps || []).map((s) => s.key + ':' + s.state).join(',') })
  if (!picked.value && !skipped.value && data.value?.preselect) picked.value = data.value.preselect
  frozenOrder.value = ranked.value.map((r) => r.id)
  goTo('task')
}
function advance() {
  const id = picked.value
  if (sheet.value !== 'task' || !id || !parses(id)) return
  const r = rankedOf(id)
  track('onboarding_v2_task_picked', { task: id, edited: edited(id), example: !!r?.example,
    rank: ranked.value.findIndex((x) => x.id === id) + 1, score: r?.score ?? null })
  clearTimeout(pollTimer)   // the input is settled now: later checks must not change what was run
  goTo('go')
  if (!running.value && (ranId.value !== id || runInput.value !== slot(id))) run()
}

// ------------------------------------------------------------------------------- the first call
type Line = { text: string; typed: string; state: 'typing' | 'wait' | 'ok' | 'err'; note: string }
const lines = ref<Line[]>([])
const running = ref(false)
const ran = ref(false)
const summary = ref('')
const result = ref<Extracted>(null)
const runErr = ref('')
const runInput = ref('')
const ranId = ref('')       // the task the last run was for: Back then Continue does not run it twice
const dirty = computed(() => !!picked.value && ran.value && slot(picked.value) !== runInput.value)
const ownDomain = computed(() => {
  const c = data.value?.tasks?.find((t) => t.id === 'company')
  return c && !c.example && /\./.test(c.value) ? c.value : ''
})
let typeTimers: number[] = []
const sleep = (ms: number) => new Promise<void>((r) => typeTimers.push(window.setTimeout(r, ms)))

async function typeLine(line: Line) {
  if (reduced) { line.typed = line.text; return }
  const dur = Math.min(300, line.text.length * 6)
  const step = Math.max(8, dur / line.text.length)
  for (let i = 1; i <= line.text.length; i += Math.max(1, Math.round(16 / step))) {
    line.typed = line.text.slice(0, i)
    await sleep(16)
  }
  line.typed = line.text
}

async function run() {
  const id = picked.value
  if (!id || running.value) return
  const task = library.value[id]
  const calls = buildCalls(task, slot(id))
  if (!calls) return
  running.value = true; ran.value = false; result.value = null; runErr.value = ''; summary.value = ''; lines.value = []
  runInput.value = slot(id); ranId.value = id
  const ctl = new AbortController()
  const timer = window.setTimeout(() => ctl.abort(), 60000)
  const t0 = performance.now()
  const results: CallResult[] = []
  for (const c of calls) {
    // the call starts as the line starts typing
    const pending = props.preview ? runPreviewCall(data.value?.id || '', c, d.headers()) : runCall(c, d.headers(), ctl.signal)
    const line = reactive<Line>({ text: cliLine(c), typed: '', state: 'typing', note: '' })
    lines.value.push(line)
    await typeLine(line)
    line.state = 'wait'
    const r = await pending
    results.push(r)
    line.state = r.error ? 'err' : 'ok'
    if (!r.error) line.note = [r.servedBy ? 'served by ' + r.servedBy.split('.')[0] : '', (r.ms / 1000).toFixed(1) + ' s'].filter(Boolean).join(' · ')
  }
  clearTimeout(timer)
  const cost = results.reduce((s, r) => s + r.costMicro, 0)
  const ok = results.filter((r) => !r.error)
  summary.value = [((performance.now() - t0) / 1000).toFixed(1) + ' s', fmtCost(cost), 'no API keys'].join(' · ')
  if (props.preview) previewSpent.value += cost
  else d.loadBilling?.()
  track('onboarding_v2_first_call', { task: id, endpoint: calls[0].endpoint, cost_micro: cost, ok: ok.length > 0, status: results[0]?.status })
  if (ok.length) {
    result.value = extract(task.view, results.map((r) => (r.error ? null : r.body)), slot(id), ownDomain.value)
    if (!result.value) runErr.value = 'answered, but nothing to show'
  } else {
    runErr.value = results[0]?.error || 'no answer'
  }
  // A first call should almost never fail: the page shows no error, the failure is reported instead.
  if (runErr.value) {
    const w = window as unknown as { posthog?: { captureException?: (e: Error, p: Record<string, unknown>) => void } }
    w.posthog?.captureException?.(new Error('onboarding first call: ' + runErr.value), {
      task: id, endpoint: calls[0].endpoint, status: results[0]?.status, preview: !!props.preview })
  }
  running.value = false; ran.value = true
}

// The input is right above the result: a failed run is fixed there, not by going back a sheet.

// ------------------------------------------------------------------------------- the dock
const balance = computed<number | null>(() =>
  props.preview ? (data.value ? 1_000_000 - previewSpent.value : null) : (d.billing ? Number(d.billing.balance_micro) : null))
const shownBalance = ref<number | null>(null)
const tick = ref(false)
watch(balance, (to, from) => {
  if (to == null) return
  if (from == null || shownBalance.value == null || reduced) { shownBalance.value = to; return }
  tick.value = false; nextTick(() => { tick.value = true })
  const start = performance.now(), a = shownBalance.value
  const f = (now: number) => {
    const k = Math.min(1, (now - start) / 700)
    shownBalance.value = a + (to - a) * (1 - Math.pow(1 - k, 4))
    if (k < 1) requestAnimationFrame(f)
  }
  requestAnimationFrame(f)
}, { immediate: true })
const moneyText = computed(() => {
  const b = shownBalance.value
  if (b == null) return ''
  // whole cents read as money; a sub-cent call shows its real digits ($0.9998), never a rounded $1.000
  return b % 10000 === 0 ? '$' + (b / 1e6).toFixed(2) : '$' + (b / 1e6).toFixed(4).replace(/0+$/, '')
})
const covers = computed(() => {
  const id = picked.value
  const t = id ? library.value[id] : null
  if (!t || !t.unit_usd || balance.value == null) return ''
  return floor2(balance.value / 1e6 / t.unit_usd).toLocaleString('en-US') + ' ' + t.unit
})

// ------------------------------------------------------------------------------- leaving
function previewAgain() {
  clearTimeout(pollTimer); typeTimers.forEach(clearTimeout)
  data.value = null; picked.value = null; skipped.value = false; showAll.value = false; previewSpent.value = 0
  frozenOrder.value = null
  for (const k of Object.keys(edits)) delete edits[k]
  lines.value = []; result.value = null; runErr.value = ''; summary.value = ''; ran.value = false
  goTo('diag')
}

async function finish(record = true) {
  clearTimeout(pollTimer)
  if (props.preview) {
    history.replaceState(history.state, '', location.pathname + location.search)
    emit('done')
    return
  }
  if (record) track('onboarding_finished', { flow: 'v2', task: picked.value, ran: ran.value, step: sheet.value })
  d.onboarded = true
  try { await d.api('/onboard/skip', { method: 'POST' }) } catch { /* not re-offered either way once onboarded */ }
  if (d.view !== 'platform') d.go('start')
  emit('done')
}

// ------------------------------------------------------------------------------- the background
const stage = ref<HTMLElement | null>(null)
let fieldRaf = 0
function mountField() {
  const mount = () => {
    const w = window as any
    const field = stage.value && w.tregMountField?.(stage.value)
    if (!field) return
    const loop = (t: number) => { field.tick(t); fieldRaf = requestAnimationFrame(loop) }
    fieldRaf = requestAnimationFrame(loop)
  }
  if ((window as any).tregMountField) return mount()
  const s = document.createElement('script')
  s.src = '/media/landing/hero-particles.js'; s.onload = mount
  document.head.appendChild(s)
}

onMounted(() => { if (!props.preview) begin(); mountField(); document.documentElement.style.overflow = 'hidden' })
onBeforeUnmount(() => {
  clearTimeout(pollTimer); typeTimers.forEach(clearTimeout); cancelAnimationFrame(fieldRaf)
  document.documentElement.style.overflow = ''
})
</script>

<template>
<div class="ob2" role="dialog" aria-modal="true" aria-label="Set up treg">
  <div ref="stage" class="ob-field" aria-hidden="true"></div>

  <main class="wrap">
    <div class="ob-stack" :data-left="Math.min(2, SEQ.length - 1 - sheetIdx)" aria-live="polite">
      <Transition name="ob-sheet" @before-leave="el => ((el as HTMLElement).inert = true)">
        <!-- 1. Setting up -->
        <section v-if="sheet==='diag'" key="diag" class="sheet cur" aria-labelledby="ob-t-diag">
          <span v-if="preview" class="ob-preview-chip st" style="--i:0">Preview<template v-if="data?.email"> of {{data.email}}</template></span>
          <h2 id="ob-t-diag" class="sh-title st" style="--i:1" tabindex="-1">{{facts.length ? "Here's what we found" : data?.ask ? 'One quick question' : 'Looking up your information'}}</h2>
          <form v-if="preview && !data" class="sh-body ob-preview" @submit.prevent="startPreview">
            <label class="oe-row"><span class="oe-k">Email</span>
              <input id="ob-preview-email" v-model="previewEmail" class="oe-input" type="email" required autocomplete="off"
                spellcheck="false" placeholder="someone@company.com"></label>
            <p class="oe-note">Runs the real setup for this address: GitHub, company, site, then the tasks. No team is made; lookups and the first call are billed to the house team.</p>
            <p v-if="previewErr" class="oe-note ob-preview-err" role="alert">{{previewErr}}</p>
            <div class="sh-foot"><button type="submit" class="candy" :disabled="previewBusy || !previewEmail.trim()">Start <span class="arrow" aria-hidden="true">→</span></button></div>
          </form>
          <div v-else class="sh-body"><div class="bento" role="list" aria-live="polite">
            <article v-for="(f, i) in facts" :key="f.key" class="bcard" :class="['b-' + f.key, {'b-wide': wide(f)}]"
              :style="{'--n': i, ...spans[i]}" role="listitem">
              <template v-if="f.key === 'intro'">
                <span class="b-mark" :class="{'b-mark-img': f.avatar && !brokenMark}" aria-hidden="true"><img v-if="f.avatar && !brokenMark" :src="f.avatar" alt="" referrerpolicy="no-referrer" @error="brokenMark = true"><template v-else>{{(f.label || f.value || '?')[0].toUpperCase()}}</template></span>
                <span class="b-text">
                  <span v-if="f.label" class="b-label">{{f.label}}</span>
                  <b class="b-name">{{f.value ? 'Welcome, ' + f.value : 'Welcome'}}</b>
                  <span v-if="f.note" class="b-about">{{f.note}}</span>
                </span>
              </template>
              <template v-else>
                <img v-if="f.avatar" class="b-avatar" :src="f.avatar" alt="" referrerpolicy="no-referrer">
                <img v-else-if="ART.has(f.key)" class="b-art" :src="'/media/onboarding/' + f.key + '.webp'" alt="" loading="eager">
                <span class="b-text">
                  <span class="b-label">{{f.label}}</span>
                  <span class="b-value">{{f.value}}</span>
                  <span v-if="f.note" class="b-note">{{f.note}}</span>
                </span>
              </template>
            </article>
            <article v-for="n in (looking ? (facts.length ? 1 : 3) : 0)" :key="'looking' + n" class="bcard b-looking" :class="{'b-wide': !facts.length && n === 1}"
              :style="{'--n': facts.length + n - 1}" role="listitem" :aria-label="n === 1 ? looking : undefined"></article>
            <article v-if="nothingFound" key="none" class="bcard b-wide b-looking" role="listitem"><span>Nothing public to go on yet. You'll start from the tasks new teams pick first.</span></article>
            <article v-if="lookupDone && data?.ask" key="ask" class="bcard b-ask" role="listitem" :style="{'--n': facts.length}">
              <span class="b-text">
                <b class="b-value" id="ob-ask">What will your agent do first?</b>
              </span>
              <span class="b-chips" role="radiogroup" aria-labelledby="ob-ask">
                <button v-for="u in data.use_cases" :key="u.key" type="button" class="b-chip" role="radio"
                  :aria-checked="data.here_for === u.key" :disabled="!!answering" @click="answer(u.key)">{{u.label}}</button>
              </span>
            </article>
          </div></div>
        </section>

        <!-- 2. What should your agent do first -->
        <section v-else-if="sheet==='task'" key="task" class="sheet cur" aria-labelledby="ob-t-task">
          <span v-if="preview" class="ob-preview-chip st" style="--i:0">Preview of {{data?.email}}</span>
          <h2 id="ob-t-task" class="sh-title st" style="--i:1" tabindex="-1">A task treg could help your agent with</h2>
          <div class="sh-body"><div class="q-opts" role="radiogroup" aria-labelledby="ob-t-task">
            <div v-for="(r, i) in ranked" v-show="showAll || i < SHOWN" :key="r.id" class="opt st" :style="{'--i': i < SHOWN ? i + 3 : i - SHOWN}"
              role="radio" :aria-checked="picked===r.id" :data-opt="r.id"
              :tabindex="picked===r.id || (!picked && i===0) ? 0 : -1"
              @click="!($event.target as HTMLElement).closest('.oe-input') && picked!==r.id && choose(r.id, 'click')" @keydown="onCardKey($event, r.id)">
              <span class="opt-t">
                <span class="opt-l">{{tplParts(r.id)[0]}}<span class="slotv" :class="{'ob-pending': finding(r)}" :title="finding(r) ? 'Finding one for you' : undefined">{{slot(r.id)}}</span>{{tplParts(r.id)[1]}}</span>
                <span v-if="r.recommended" class="guess">Highly recommended</span>
                <span class="opt-edit"><span class="oe-in">
                  <label class="oe-row"><span class="oe-k">{{library[r.id]?.slot}}</span>
                    <input :id="'ob-fill-'+r.id" class="oe-input" :value="rawSlot(r.id)" autocomplete="off" spellcheck="false" :tabindex="picked===r.id ? 0 : -1"
                      @input="edits[r.id]=($event.target as HTMLInputElement).value"
                      @keydown.enter.prevent="advance()"></label>
                  <span class="oe-note" :class="{'ob-finding': finding(r)}">{{noteFor(r)}}</span>
                </span></span>
              </span>
              <span class="radio" aria-hidden="true"></span>
            </div>
          </div></div>
          <div v-if="!showAll && ranked.length > SHOWN" class="sh-foot ob-more st" style="--i:9">
            <button type="button" class="linkbtn" @click="showAll=true">Show {{ranked.length - SHOWN}} more</button>
          </div>
        </section>

        <!-- 3. Your first call -->
        <section v-else key="go" class="sheet cur" aria-labelledby="ob-t-go">
          <span v-if="preview" class="ob-preview-chip st" style="--i:0">Preview of {{data?.email}}</span>
          <h2 id="ob-t-go" class="sh-title st" style="--i:1" tabindex="-1">Try your agent's first call</h2>
          <section class="go-sec st" style="--i:2">
            <form v-if="picked" class="run-edit" @submit.prevent="run()">
              <label class="oe-row"><span class="oe-k">{{library[picked]?.slot}}</span>
                <input id="ob-run-input" class="oe-input" :value="rawSlot(picked)" autocomplete="off" spellcheck="false"
                  @input="edits[picked!] = ($event.target as HTMLInputElement).value">
                <button v-if="dirty || runErr" type="submit" class="ob-rerun" :disabled="running || !parses(picked)">Run again</button></label>
            </form>
            <div class="ob-term">
              <div class="term-h"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span><span>treg · first call</span>
                <span v-if="running || !runErr" class="ob-btn oneclick ob-run" :class="{'ob-done': ran && !runErr}" role="status">
                  <template v-if="running"><span class="ob-spin"></span> Running</template>
                  <template v-else-if="!runErr">Done ✓</template>
                </span>
              </div>
              <div class="term-body" aria-live="polite">
                <div v-for="(l, i) in lines" :key="i" class="tline"><span class="ps">$ </span><span class="typed">{{l.typed}}</span>
                  <span v-if="l.state==='wait'" class="tstat"><span class="ob-spin"></span></span>
                  <span v-else-if="l.state==='ok'" class="tstat ob-ok">✓ 200 <span class="ob-tn">· {{l.note}}</span></span>
                  <span v-else-if="l.state==='err'" class="tstat ob-err" aria-label="No answer">×</span>
                </div>
                <p v-if="summary" class="tsum">{{summary}}</p>
              </div>
            </div>
            <div class="run-results">
              <ResultView v-if="result" :data="result" />
            </div>
          </section>
          <section class="go-sec st" style="--i:3" aria-label="Give it to your agent">
            <ConnectBlock :base="d.proxy" :team="d.activeSlugNow || data?.team?.slug || ''" :token="d.myToken || ''"
              :say="picked ? library[picked]?.say || '' : ''" :value="picked ? rawSlot(picked) : ''"
              @update:value="v => { if (picked) edits[picked] = v }" @copied="m => track('onboarding_v2_connect_copied', {method: m, task: picked})" />
          </section>
        </section>
      </Transition>
    </div>
  </main>

  <div class="dock" role="region" aria-label="Progress and credit">
    <div class="dock-paper"><div class="dock-in">
      <div class="dock-prog">
        <div class="dock-seg" aria-hidden="true"><i v-for="(s, i) in SEQ" :key="s" :class="{'ob-done': i < sheetIdx, cur: i === sheetIdx}"><b></b></i></div>
        <p class="dock-step"><b>{{sheetIdx + 1}} / {{SEQ.length}}</b> {{STEP_LABEL[sheet]}}</p>
      </div>
      <div v-if="moneyText" class="dock-credit">
        <p class="dock-amt">
          <span class="glyph" aria-hidden="true"><svg viewBox="0 0 290 290"><rect width="140.5" height="140.5" rx="20"/><rect x="149.5" y="149.5" width="140.5" height="140.5" rx="20"/></svg></span>
          <b class="money" :class="{tick}">{{moneyText}}</b>
        </p>
        <!-- A new team's balance is its signup grant: "free credit", the word the rest of treg uses. -->
        <p class="covers">{{(balance ?? 0) > 0 ? 'Free credit' : 'Your credit'}}<template v-if="covers"> · covers about <b>{{covers}}</b></template></p>
      </div>
      <!-- The step's buttons live here, not at the end of the sheet: a tall sheet never hides them. -->
      <div class="dock-act">
        <template v-if="sheet === 'diag'">
          <button v-if="data" type="button" class="candy ob-next" @click="diagContinue(!lookupDone)">{{lookupDone ? 'Continue' : 'Skip'}} <span class="arrow" aria-hidden="true">→</span></button>
        </template>
        <template v-else-if="sheet === 'task'">
          <button type="button" class="linkbtn ob-back" @click="goTo('diag')"><span aria-hidden="true">←</span> Back</button>
          <button type="button" class="candy ob-next" :disabled="!picked || !parses(picked)" @click="advance()">Continue <span class="arrow" aria-hidden="true">→</span></button>
        </template>
        <template v-else>
          <button type="button" class="linkbtn ob-back" @click="goTo('task')"><span aria-hidden="true">←</span> Back</button>
          <button v-if="preview" type="button" class="linkbtn" @click="previewAgain">Try another email</button>
          <button type="button" class="candy ob-next" @click="finish()">{{preview ? 'Close the preview' : 'Open the dashboard'}} <span class="arrow" aria-hidden="true">→</span></button>
        </template>
      </div>
    </div></div>
  </div>
</div>
</template>
