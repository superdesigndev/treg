<script setup lang="ts">
// Vibe-it (docs/context/architecture/vibe-it.md): the maker talks with treg's agent, which searches
// the catalog, writes the hub tool's files and asks before anything costs money or changes the team.
// The answer streams in; each step is a card; the maker's buttons (test, publish, app, password) run
// the hub's own routes and land in the conversation. The files are beside the chat, every version kept.
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ApiError, api, me, setTeam, signInUrl, stream, teams, usd, type Team } from '../standalone/api'
import BrandMark from '../components/BrandMark.vue'
import AskCard from './AskCard.vue'
import Composer from './Composer.vue'
import EmptyState from './EmptyState.vue'
import EventCard from './EventCard.vue'
import FilesPanel from './FilesPanel.vue'
import SessionList from './SessionList.vue'
import StatusBar from './StatusBar.vue'
import StepCard from './StepCard.vue'
import StepGroup from './StepGroup.vue'
import { LIMITS, useLayout } from './layout'
import { useStickToBottom } from './stickToBottom'
import { plain } from './errors'
import { onCopyClick, render } from './markdown'
import { STEP_WORDS, type Attachment, type Conversation, type Draft, type Msg, type Pending, type Session, type ToolStatus, type Warning } from './types'

const state = ref<'loading' | 'signedout' | 'off' | 'noteam' | 'ready'>('loading')
const email = ref('')
const myTeams = ref<Team[]>([])
const team = ref('')
const budget = ref({ budget: 0, left: 0 })
const sessions = ref<Session[]>([])
const conv = ref<Conversation | null>(null)
const text = ref('')
const streaming = ref(false)
const acting = ref(false)
const liveText = ref('')
const liveStep = ref<{ name: string, args: any } | null>(null)
const err = ref<{ title: string, text: string } | null>(null)
const tool = ref<ToolStatus | null>(null)
const warnings = ref<Warning[]>([])
const myTools = ref<{ tool_id: string, version: number, status: string, summary: string }[]>([])
const localAsk = ref<Pending | null>(null)
const agentProblem = ref<{ at: number, problem: { field: string, rule: string } | null } | undefined>()
const chatEl = ref<HTMLElement | null>(null)
const threadEl = ref<HTMLElement | null>(null)
const ui = useLayout()
const { pinned, follow } = useStickToBottom(chatEl, threadEl, () => !!conv.value?.messages.length)
const files = ref<InstanceType<typeof FilesPanel> | null>(null)
const composer = ref<InstanceType<typeof Composer> | null>(null)
let poll: number | undefined
let statusTimer: number | undefined

const busy = computed(() => streaming.value || acting.value || !!conv.value?.busy)
const messages = computed<Msg[]>(() => conv.value?.messages || [])
const ask = computed<Pending | null>(() => localAsk.value || conv.value?.pending || null)
const outputFields = computed<string[]>(() => {
  const o = conv.value?.draft?.manifest?.output
  return o && Array.isArray(o.fields) ? o.fields : o ? Object.keys(o) : []
})
const lastWriteId = computed(() => [...messages.value].reverse().find(m => m.role === 'tool' && m.name === 'write_files' && m.version)?.id)
const lastEventId = computed(() => { const l = messages.value[messages.value.length - 1]; return l?.role === 'event' ? l.id : null })
const lastAgentId = computed(() => [...messages.value].reverse().find(m => m.role === 'assistant' && m.text)?.id)

// Each answer's model cost: the assistant turns between one maker message and the next.
const turnCost = computed(() => {
  const out: Record<number, number> = {}
  let sum = 0, lastText: number | null = null
  const close = () => { if (lastText !== null && sum) out[lastText] = sum; sum = 0; lastText = null }
  for (const m of messages.value) {
    if (m.role === 'user') close()
    if (m.role === 'assistant') { sum += m.cost_micro || 0; if (m.text) lastText = m.id }
  }
  close()
  return out
})
// What the chat shows: messages, with runs of routine steps folded into one line. A file change
// (its diff) and a test the agent ran (its table) stay cards of their own.
type Item = { kind: 'msg', m: Msg } | { kind: 'group', key: string, steps: Msg[] }
const items = computed<Item[]>(() => {
  const out: Item[] = []
  let run: Msg[] = []
  const flush = () => {
    if (run.length > 1) out.push({ kind: 'group', key: `g${run[0].id}`, steps: run })
    else if (run.length === 1) out.push({ kind: 'msg', m: run[0] })
    run = []
  }
  for (const m of messages.value) {
    const routine = m.role === 'tool' && !(m.name === 'write_files' && m.version) && m.name !== 'test_run'
      && !['publish', 'app_on', 'app_password'].includes(m.name || '')
    if (routine) { run.push(m); continue }
    flush(); out.push({ kind: 'msg', m })
  }
  flush()
  return out
})
const drafts = computed(() => sessions.value.filter(s => !s.tool_id && s.has_files))
const budgetPct = computed(() => budget.value.budget ? Math.max(0, Math.min(100, Math.round(100 * budget.value.left / budget.value.budget))) : 0)
const lastTurnCost = computed(() => { const id = lastAgentId.value; return id ? turnCost.value[id] || 0 : 0 })

// Following the conversation is the stick-to-bottom observer's job; `force` is "jump down now and
// follow from here" (opening a conversation, sending, a button the maker pressed).
function scrollDown(force = false) { if (force) nextTick(() => follow()) }

async function loadState() {
  try {
    const s = await api('/vibe/state')
    budget.value = { budget: s.budget_micro, left: s.left_micro }
    sessions.value = s.sessions
    state.value = 'ready'
  } catch { state.value = 'off' }
}

async function loadMyTools() {
  try {
    const rows: any[] = await api('/hub/tools/mine')
    const seen = new Map<string, any>()
    for (const r of rows) if (!seen.has(r.tool_id)) seen.set(r.tool_id, r)
    myTools.value = [...seen.values()].filter(t => t.status !== 'retired').map(t => ({ tool_id: t.tool_id, version: t.version, status: t.status, summary: t.summary }))
  } catch { myTools.value = [] }
}

async function loadStatus() {
  if (!conv.value) { tool.value = null; warnings.value = []; return }
  try {
    const s = await api(`/vibe/sessions/${conv.value.id}/status`)
    tool.value = s.tool; warnings.value = s.warnings
  } catch { /* the header is a convenience */ }
}
function statusSoon() { clearTimeout(statusTimer); statusTimer = window.setTimeout(loadStatus, 700) }

async function refresh() {
  if (!conv.value) return
  conv.value = await api(`/vibe/sessions/${conv.value.id}`)
  watchBusy()
}

// Busy here but not streaming to this tab (a reload mid-run, another tab): follow it until it ends.
function watchBusy() {
  clearInterval(poll)
  if (!conv.value?.busy || streaming.value) return
  poll = window.setInterval(async () => {
    if (!conv.value) return clearInterval(poll)
    const c = await api(`/vibe/sessions/${conv.value.id}`).catch(() => null)
    if (!c) return
    conv.value = c; scrollDown()
    if (!c.busy) { clearInterval(poll); loadState(); loadStatus() }
  }, 2000)
}

async function open(id: number) {
  err.value = null; localAsk.value = null; liveText.value = ''; liveStep.value = null
  conv.value = await api(`/vibe/sessions/${id}`)
  history.replaceState(null, '', `/vibe-it#${id}`)
  scrollDown(true); loadStatus(); watchBusy()
}

async function newSession(from: { tool?: string, draft?: number } = {}) {
  err.value = null; localAsk.value = null
  try {
    conv.value = await api('/vibe/sessions', { method: 'POST',
      json: from.tool ? { from_tool: from.tool } : from.draft ? { from_draft: from.draft } : {} })
  } catch (e) { err.value = e instanceof ApiError ? plain(e.status, e.detail) : { title: 'Could not start', text: String(e) }; return }
  history.replaceState(null, '', `/vibe-it#${conv.value!.id}`)
  await loadState(); loadStatus()
}

// "New chat" opens the empty page; the conversation is made with the first message (or a pick).
function newChat() {
  conv.value = null; tool.value = null; warnings.value = []; err.value = null; localAsk.value = null
  history.replaceState(null, '', '/vibe-it')
  if (ui.narrow.value) ui.tab.value = 'chat'
  nextTick(() => composer.value?.focus())
}

// "Edit a tool": the same empty page, at its list of the team's tools and drafts.
function editATool() {
  newChat()
  nextTick(() => document.getElementById('vb-change')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

async function remove(s: Session) {
  if (!confirm(`Delete “${s.title}”? The published tool, if any, stays.`)) return
  try { await api(`/vibe/sessions/${s.id}`, { method: 'DELETE' }) }
  catch (e) { err.value = e instanceof ApiError ? plain(e.status, e.detail) : null; return }
  if (conv.value?.id === s.id) { conv.value = null; history.replaceState(null, '', '/vibe-it') }
  await loadState()
}

async function rename(s: Session, title: string) {
  await api(`/vibe/sessions/${s.id}`, { method: 'PATCH', json: { title } })
  if (conv.value?.id === s.id) conv.value.title = title
  await loadState()
}

async function pin(s: Session) {
  await api(`/vibe/sessions/${s.id}`, { method: 'PATCH', json: { pinned: !s.pinned } })
  await loadState()
}

function onEvent(e: any) {
  const c = conv.value
  if (!c) return
  if (e.type === 'delta') { liveText.value += e.text; scrollDown() }
  else if (e.type === 'reset') liveText.value = ''
  else if (e.type === 'step') { liveStep.value = { name: e.name, args: e.args }; scrollDown() }
  else if (e.type === 'message') {
    const m: Msg = e.message
    if (m.role === 'user') c.messages = c.messages.filter(x => x.id !== -1)
    if (m.role === 'assistant') liveText.value = ''
    if (m.role === 'tool') liveStep.value = null
    if (!c.messages.some(x => x.id === m.id)) c.messages.push(m)
    scrollDown()
  } else if (e.type === 'draft') { c.draft = e.draft; agentProblem.value = { at: Date.now(), problem: e.problem }; statusSoon() }
  else if (e.type === 'pending') c.pending = e.pending
  else if (e.type === 'budget') budget.value.left = e.left_micro
  else if (e.type === 'error') err.value = { title: 'The agent stopped', text: e.message }
}

async function run(path: string, json?: unknown) {
  if (!conv.value) return
  streaming.value = true; err.value = null; liveText.value = ''; liveStep.value = null
  try {
    await stream(`/vibe/sessions/${conv.value.id}/${path}`, { method: 'POST', json }, onEvent)
  } catch (e) {
    err.value = e instanceof ApiError ? plain(e.status, e.detail) : plain(0, null)
  } finally {
    streaming.value = false; liveText.value = ''; liveStep.value = null
    await refresh().catch(() => null)
    loadState(); loadStatus(); scrollDown()
  }
}

async function send(t: string, attachments: Attachment[] = []) {
  if (busy.value) return
  if (!conv.value) await newSession()
  if (!conv.value) return
  if (files.value && !(await files.value.saveIfDirty())) return
  localAsk.value = null
  conv.value.messages.push({ id: -1, role: 'user', text: t, at: new Date().toISOString(),
    attachments: attachments.map(a => ({ name: a.name, size: (a.text || '').length })) })
  text.value = ''
  scrollDown(true)
  await run('messages', { text: t, attachments })
}

function regenerate() {
  if (busy.value || !confirm('Answer your last message again? The agent\'s answer and file changes since then are undone; your test runs and publishes stay.')) return
  run('regenerate')
}

async function stop() {
  if (!conv.value) return
  await api(`/vibe/sessions/${conv.value.id}/stop`, { method: 'POST' }).catch(() => null)
}

async function act(kind: string, extra: Record<string, unknown> = {}) {
  const c = conv.value
  if (!c) return
  if (kind === 'password-card') { localAsk.value = { id: 'local', kind: 'password' }; scrollDown(true); return }
  if (kind === 'auto') {
    const r = await api(`/vibe/sessions/${c.id}`, { method: 'PATCH', json: { auto_test: !!extra.on } })
    c.auto_test = r.auto_test; return
  }
  if (files.value && !(await files.value.saveIfDirty())) return
  acting.value = true; err.value = null; localAsk.value = null
  try {
    const r = await api(`/vibe/sessions/${c.id}/actions`, { method: 'POST', json: { kind, ...extra } })
    c.messages.push(r.event); c.pending = null; c.tool_id = r.tool_id; c.auto_test = r.auto_test
  } catch (e) {
    err.value = e instanceof ApiError ? plain(e.status, e.detail) : plain(0, null)
  } finally {
    acting.value = false
    scrollDown(true); loadState(); loadStatus()
    if (kind === 'publish') loadMyTools()
  }
}

async function revert(n: number) {
  if (!conv.value || busy.value) return
  try {
    const r = await api(`/vibe/sessions/${conv.value.id}/versions/${n}/restore`, { method: 'POST' })
    conv.value.draft = r.draft
    await refresh(); statusSoon(); scrollDown(true)
  } catch (e) { err.value = e instanceof ApiError ? plain(e.status, e.detail) : null }
}

async function useData(csv: string, name: string) {
  if (!conv.value) await newSession()
  if (!conv.value) return
  try {
    const r = await api(`/vibe/sessions/${conv.value.id}/draft`, { method: 'PUT', json: { data: csv } })
    conv.value.draft = r.draft
    files.value?.showFile('data')
    text.value = text.value || `I added ${name} as data.csv. Use it in the tool (ctx.data).`
    composer.value?.focus()
  } catch (e) { err.value = e instanceof ApiError ? plain(e.status, e.detail) : null }
}

function prefill(t: string) { text.value = t; composer.value?.focus() }
function onDraft(d: Draft) { if (conv.value) conv.value.draft = d; statusSoon() }

async function copyMessage(m: Msg) { try { await navigator.clipboard.writeText(m.text || '') } catch { /* no clipboard */ } }

function pickTeam(slug: string) {
  team.value = slug; setTeam(slug)
  try { localStorage.setItem('treg-vibe-team', slug) } catch { /* convenience */ }
  conv.value = null; tool.value = null; warnings.value = []
  loadState(); loadMyTools()
}

onMounted(async () => {
  const who = await me()
  if (!who) { state.value = 'signedout'; return }
  email.value = who.email
  myTeams.value = await teams()
  if (!myTeams.value.length) { state.value = 'noteam'; return }
  let saved = ''
  try { saved = localStorage.getItem('treg-vibe-team') || localStorage.getItem('treg-active') || '' } catch { /* none */ }
  const t = myTeams.value.find(x => x.slug === saved) || myTeams.value[0]
  team.value = t.slug; setTeam(t.slug)
  await loadState()
  loadMyTools()
  const id = Number(location.hash.slice(1))
  if (state.value === 'ready' && id) await open(id).catch(() => null)
  window.addEventListener('hashchange', onHash)
})
function onHash() {
  const id = Number(location.hash.slice(1))
  if (state.value === 'ready' && id && id !== conv.value?.id) open(id).catch(() => null)
}
onUnmounted(() => { clearInterval(poll); clearTimeout(statusTimer); window.removeEventListener('hashchange', onHash) })
</script>

<template>
  <div class="sa-page vb" :class="{ 'vb-dragging': ui.dragging.value }">
    <header class="sa-top vb-top">
      <div class="vb-top-left">
        <button v-if="state === 'ready'" type="button" class="vb-icon" :class="{ on: ui.narrow.value ? ui.tab.value === 'list' : ui.s.leftOpen }"
                :aria-pressed="ui.narrow.value ? ui.tab.value === 'list' : ui.s.leftOpen" aria-label="Conversations" title="Conversations (Ctrl/⌘ B)" @click="ui.toggle('left')">
          <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2.5" y="3.5" width="15" height="13" rx="2.5"/><path d="M7.5 3.5v13"/></svg>
        </button>
        <a class="sa-brand brand vb-brand" href="/app"><BrandMark/>treg</a>
        <span class="vb-crumb">vibe it</span>
      </div>
      <div class="sa-who">
        <div v-if="state === 'ready'" class="vb-budget" :title="`treg pays for the agent's model, up to ${usd(budget.budget)} per person`">
          <span class="vb-meter" aria-hidden="true"><span :style="{ width: budgetPct + '%' }" :class="{ low: budgetPct < 20 }"/></span>
          <span><b class="sa-num">{{ usd(budget.left) }}</b> <span class="sa-muted">of {{ usd(budget.budget) }}</span></span>
          <span v-if="lastTurnCost" class="sa-muted vb-hide-sm">· last answer {{ usd(lastTurnCost) }}</span>
        </div>
        <label v-if="myTeams.length > 1" class="sa-team">Team
          <select :value="team" @change="pickTeam(($event.target as HTMLSelectElement).value)">
            <option v-for="t in myTeams" :key="t.slug" :value="t.slug">{{ t.name || t.slug }}</option>
          </select>
        </label>
        <span v-if="email" class="sa-muted sa-email vb-hide-sm">{{ email }}</span>
        <button v-if="state === 'ready'" type="button" class="vb-icon" :class="{ on: ui.narrow.value ? ui.tab.value === 'files' : ui.s.rightOpen }"
                :aria-pressed="ui.narrow.value ? ui.tab.value === 'files' : ui.s.rightOpen" aria-label="Files" title="Files (Ctrl/⌘ \)" @click="ui.toggle('right')">
          <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2.5" y="3.5" width="15" height="13" rx="2.5"/><path d="M12.5 3.5v13"/></svg>
        </button>
      </div>
    </header>

    <main v-if="state === 'loading'" class="sa-main"><p class="sa-muted" role="status">Loading…</p></main>
    <main v-else-if="state === 'signedout'" class="sa-main sa-narrow vb-gate">
      <h1>Vibe it</h1>
      <p>Describe a tool in plain words and build it with treg's agent.</p>
      <a class="sa-btn primary" :href="signInUrl()">Sign in to start</a>
    </main>
    <main v-else-if="state === 'noteam'" class="sa-main sa-narrow vb-gate">
      <h1>Vibe it</h1>
      <p>A tool belongs to a team. <a href="/app">Create a team</a>, then come back here.</p>
    </main>
    <main v-else-if="state === 'off'" class="sa-main sa-narrow vb-gate">
      <h1>Vibe it</h1>
      <p class="sa-muted">Vibe-it is not available for this team yet.</p>
    </main>

    <div v-else class="vb-grid" :class="{ narrow: ui.narrow.value }" :style="{ gridTemplateColumns: ui.columns.value }">
      <div v-show="ui.narrow.value ? ui.tab.value === 'list' : ui.s.leftOpen" class="vb-pane vb-pane-left">
        <SessionList :sessions="sessions" :current-id="conv?.id ?? null" @open="id => { open(id); if (ui.narrow.value) ui.tab.value = 'chat' }"
                     @new="newChat" @edit="editATool" @remove="remove" @rename="rename" @pin="pin"/>
        <div v-if="!ui.narrow.value" class="vb-handle right" role="separator" aria-orientation="vertical" aria-label="Resize conversations"
             :aria-valuenow="ui.left.value" :aria-valuemin="LIMITS.left.min" :aria-valuemax="LIMITS.left.max" tabindex="0"
             @pointerdown="e => ui.startDrag('left', e)" @dblclick="ui.reset('left')" @keydown="e => ui.keyDrag('left', e)"/>
      </div>

      <section v-show="!ui.narrow.value || ui.tab.value === 'chat'" class="vb-chat">
        <StatusBar :tool="tool" :warnings="ask?.kind === 'test' ? [] : warnings"/>
        <div ref="chatEl" class="vb-scroll" @click="onCopyClick">
          <div ref="threadEl" class="vb-thread">
            <EmptyState v-if="!conv || !messages.length" :tools="conv ? [] : myTools" :drafts="conv ? [] : drafts"
                        @idea="prefill" @from-tool="id => newSession({ tool: id })" @from-draft="id => newSession({ draft: id })"/>
            <p v-if="conv?.summary" class="vb-note-line sa-muted">Older messages were trimmed; the agent keeps a summary and your files.</p>
            <template v-for="it in items" :key="it.kind === 'group' ? it.key : it.m.id">
              <StepGroup v-if="it.kind === 'group'" :steps="it.steps" :session-id="conv!.id" :output-fields="outputFields" @revert="revert"/>
              <template v-else>
                <div v-if="it.m.role === 'user'" class="vb-msg user">
                  <div class="vb-user-text">{{ it.m.text }}</div>
                  <div v-if="it.m.attachments?.length" class="vb-chips">
                    <span v-for="a in it.m.attachments" :key="a.name" class="vb-chip"><code>{{ a.name }}</code></span>
                  </div>
                </div>
                <div v-else-if="it.m.role === 'assistant' && it.m.text" class="vb-agent">
                  <div class="vb-msg agent md" v-html="render(it.m.text)"/>
                  <div class="vb-msg-foot sa-muted">
                    <span v-if="it.m.stopped" class="sa-warn">stopped</span>
                    <span v-if="turnCost[it.m.id]">{{ usd(turnCost[it.m.id]) }}</span>
                    <button type="button" class="vb-link" @click="copyMessage(it.m)">Copy</button>
                    <button v-if="it.m.id === lastAgentId && !busy" type="button" class="vb-link" @click="regenerate">Regenerate</button>
                  </div>
                </div>
                <StepCard v-else-if="it.m.role === 'tool'" :msg="it.m" :session-id="conv!.id" :latest="it.m.id === lastWriteId"
                          :output-fields="outputFields" @revert="revert"/>
                <EventCard v-else-if="it.m.role === 'event'" :msg="it.m" :output-fields="outputFields" :has-app="!!tool?.app?.enabled"
                           :last="it.m.id === lastEventId" :busy="busy" @act="k => act(k)" @send="t => send(t)" @prefill="prefill"/>
              </template>
            </template>
            <div v-if="streaming && liveText" class="vb-agent"><div class="vb-msg agent md" v-html="render(liveText)"/></div>
            <div v-if="streaming && liveStep" class="vb-working" role="status">
              <span class="vb-spin" aria-hidden="true"/> {{ (STEP_WORDS[liveStep.name] || (() => liveStep!.name))(liveStep.args) }}…
            </div>
            <div v-else-if="busy && !liveText" class="vb-working" role="status"><span class="vb-spin" aria-hidden="true"/>
              {{ acting ? 'Running…' : 'The agent is working…' }}</div>
            <AskCard v-if="ask && !streaming" :pending="ask" :team="team" :name="conv?.draft?.manifest?.name || ''" :tool-id="conv?.tool_id || null"
                     :warnings="warnings" :busy="busy" @act="act"/>
          </div>
        </div>
        <div class="vb-bottom">
          <button v-if="!pinned && messages.length" type="button" class="vb-jump" @click="follow(true)">↓ Jump to latest</button>
          <div v-if="err" class="vb-err" role="alert">
            <span><b>{{ err.title }}.</b> {{ err.text }}</span>
            <button type="button" class="vb-link" @click="err = null">Dismiss</button>
          </div>
          <Composer ref="composer" v-model="text" :busy="busy" :can-data="true" @send="send" @stop="stop" @data="useData"/>
        </div>
      </section>

      <div v-show="ui.narrow.value ? ui.tab.value === 'files' : ui.s.rightOpen" class="vb-pane vb-pane-right">
        <div v-if="!ui.narrow.value" class="vb-handle left" role="separator" aria-orientation="vertical" aria-label="Resize files"
             :aria-valuenow="ui.right.value" :aria-valuemin="LIMITS.right.min" tabindex="0"
             @pointerdown="e => ui.startDrag('right', e)" @dblclick="ui.reset('right')" @keydown="e => ui.keyDrag('right', e)"/>
        <FilesPanel ref="files" :conv="conv" :team="team" :busy="busy" :has-app="!!tool?.app?.enabled" :agent-problem="agentProblem"
                    @draft="onDraft" @act="act" @restored="refresh"/>
      </div>
    </div>
  </div>
</template>

<style>
/* Space: generous between groups, tight within them. The chat reads in a centred column; the side
   panels are quiet surfaces that can be closed or resized. One scale: 4 · 8 · 12 · 16 · 24 · 32. */
.vb { height:100vh; overflow:hidden; }
.vb.vb-dragging, .vb.vb-dragging * { cursor:col-resize !important; user-select:none; }
.vb-top { padding:8px 14px; min-height:52px; }
.vb-top-left { display:flex; align-items:center; gap:10px; }
.vb-brand { font-size:15px; gap:8px; }
.vb-brand .brand-mark { width:24px; height:24px; border-radius:7px; }
.vb-crumb { font:500 12px var(--mono); color:var(--muted); padding-left:10px; border-left:1px solid var(--line2); }
.vb-icon { width:32px; height:32px; display:inline-flex; align-items:center; justify-content:center; border:0; border-radius:8px; background:none; color:var(--muted); cursor:pointer; }
.vb-icon svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.4; }
.vb-icon:hover { background:var(--hover); color:var(--ink); }
.vb-icon.on { color:var(--ink); }
.vb-budget { display:flex; align-items:center; gap:8px; font-size:12.5px; }
.vb-meter { width:56px; height:5px; border-radius:99px; background:var(--panel2); overflow:hidden; display:inline-block; }
.vb-meter span { display:block; height:100%; background:var(--ink); border-radius:inherit; transition:width .3s; }
.vb-meter span.low { background:var(--bad); }
.vb-gate { padding-top:12vh; }

.vb .vb-grid { display:grid; height:calc(100vh - 52px); min-height:0; }
.vb-pane { position:relative; min-width:0; min-height:0; display:flex; flex-direction:column; background:var(--surface); }
.vb-pane-left { border-right:1px solid var(--line); grid-column:1; grid-row:1; }
.vb-pane-right { border-left:1px solid var(--line); grid-column:3; grid-row:1; }
.vb-pane > :not(.vb-handle) { flex:1; min-height:0; }
.vb-handle { position:absolute; top:0; bottom:0; width:9px; z-index:4; cursor:col-resize; touch-action:none; }
.vb-handle.right { right:-5px; } .vb-handle.left { left:-5px; }
.vb-handle::after { content:""; position:absolute; top:0; bottom:0; left:4px; width:1px; background:transparent; transition:background .15s; }
.vb-handle:hover::after, .vb-handle:focus-visible::after, .vb-dragging .vb-handle::after { background:var(--ink); }
.vb-handle:focus-visible { outline:none; }

.vb-list { padding:16px 12px; overflow:auto; display:flex; flex-direction:column; gap:12px; }
.vb-new-row { display:flex; flex-direction:column; gap:8px; align-items:stretch; }
.vb-new { width:100%; gap:6px; }
.vb-new span { font-size:16px; line-height:1; }
.vb-edit-link { align-self:center; font-size:12.5px; color:var(--muted); text-decoration:none; }
.vb-edit-link:hover { color:var(--ink); text-decoration:underline; }
.vb-sub { margin:-6px 0 12px; }
.vb-search { font-size:13px; padding:7px 10px; border-radius:9px; }
.vb-group { display:flex; flex-direction:column; gap:2px; }
.vb-group-label { margin:10px 8px 4px; font:600 10.5px var(--sans); color:var(--muted); text-transform:uppercase; letter-spacing:.06em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.vb-list ul { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:1px; }
.vb-list li { display:flex; align-items:center; border-radius:9px; }
.vb-list li.on { background:var(--panel2); }
.vb-list li:hover:not(.on) { background:var(--hover); }
.vb-item { flex:1; min-width:0; text-align:left; border:0; background:none; padding:8px 10px; cursor:pointer; color:var(--ink); display:flex; flex-direction:column; gap:4px; }
.vb-item-title { font-weight:550; font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.vb-item-meta { font-size:11.5px; display:flex; gap:6px; align-items:center; white-space:nowrap; overflow:hidden; }
.vb-item-actions { display:flex; opacity:0; transition:opacity .12s; padding-right:4px; }
.vb-list li:hover .vb-item-actions, .vb-item-actions:focus-within { opacity:1; }
.vb-item-actions button { border:0; background:none; color:var(--muted); cursor:pointer; font-size:13px; padding:4px 5px; border-radius:6px; }
.vb-item-actions button:hover { color:var(--ink); background:var(--hover); }
.vb-rename { margin:4px; font-size:13px; padding:5px 8px; }
.vb-badge { display:inline-block; font:500 10.5px var(--sans); padding:1px 7px; border-radius:99px; background:var(--panel2); color:var(--muted); }
.vb-badge.ok { color:var(--ok); background:color-mix(in srgb,var(--ok) 11%,transparent); }
.vb-empty { font-size:13px; padding:0 8px; }

.vb-chat { grid-column:2; grid-row:1; position:relative; display:flex; flex-direction:column; min-width:0; min-height:0; background:var(--bg); }
.vb-status { padding:12px 32px; display:flex; flex-direction:column; gap:8px; font-size:12.5px; border-bottom:1px solid var(--line); background:var(--bg); }
.vb-status-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; max-width:860px; width:100%; margin:0 auto; }
.vb-status .vb-warns { max-width:860px; width:100%; margin:0 auto; }
.vb-status-id { font-weight:600; font-size:12.5px; }
.vb-status-links { margin-left:auto; display:flex; gap:14px; align-items:center; }
.vb-status-links a, .vb-status-links .vb-link { color:var(--muted); text-decoration:none; font-size:12.5px; }
.vb-status-links a:hover, .vb-status-links .vb-link:hover { color:var(--ink); text-decoration:underline; }
.vb-pill { font:500 11px var(--sans); padding:2px 9px; border-radius:999px; border:1px solid var(--line2); color:var(--muted); }
.vb-pill.ok { color:var(--ok); border-color:color-mix(in srgb,var(--ok) 35%,transparent); }
.vb-pill.bad { color:var(--bad); border-color:color-mix(in srgb,var(--bad) 35%,transparent); }
.vb-warns { margin:0; padding:10px 14px 10px 30px; background:color-mix(in srgb,var(--warn) 9%,transparent); border-radius:12px; color:var(--ink); font-size:12.5px; display:flex; flex-direction:column; gap:4px; }
.vb-link { border:0; background:none; padding:0; color:var(--ink); text-decoration:underline; text-underline-offset:2px; cursor:pointer; font:inherit; }

.vb-scroll { flex:1; overflow:auto; padding:32px 32px 24px; scrollbar-gutter:stable; }
.vb-thread { max-width:860px; margin:0 auto; display:flex; flex-direction:column; gap:16px; }
.vb-intro { margin:6vh auto 0; width:100%; display:flex; flex-direction:column; }
.vb-intro h1 { font-size:30px; margin-bottom:12px; }
.vb-intro > p { max-width:60ch; margin:0; line-height:1.6; }
.vb-how { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; padding:0; list-style:none; counter-reset:how; margin:24px 0 0; }
.vb-how li { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:12px 14px; font-size:12.5px; line-height:1.45; counter-increment:how; }
.vb-how li::before { content:counter(how, decimal-leading-zero); font:500 11px var(--mono); color:var(--muted); display:block; margin-bottom:6px; }
.vb-intro h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:40px 0 12px; }
.vb-ideas { list-style:none; padding:0; margin:0; display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:8px; }
.vb-ideas button, .vb-mine button { border:1px solid var(--line); background:var(--surface); border-radius:12px; padding:12px 14px; text-align:left; width:100%; height:100%; cursor:pointer; font:12.5px/1.45 var(--sans); color:var(--ink); display:flex; flex-direction:column; gap:4px; transition:border-color .12s, transform .12s; }
.vb-ideas button:hover, .vb-mine button:hover { border-color:var(--line2); transform:translateY(-1px); }
.vb-mine { list-style:none; padding:0; margin:0; display:flex; flex-direction:column; gap:8px; }
.vb-mine-sum { font-size:12px; }
.vb-gap { margin-top:18px; }

.vb-msg { padding:12px 16px; border-radius:16px; overflow-wrap:anywhere; line-height:1.6; }
.vb-msg.user { align-self:flex-end; max-width:min(620px,85%); background:var(--inverse); color:var(--inverse-ink); border-bottom-right-radius:6px; display:flex; flex-direction:column; gap:6px; margin-top:8px; }
.vb-user-text { white-space:pre-wrap; }
.vb-msg.user .vb-chip { background:color-mix(in srgb,var(--inverse-ink) 15%,transparent); color:inherit; }
.vb-agent { align-self:stretch; display:flex; flex-direction:column; gap:6px; min-width:0; }
.vb-msg.agent { padding:2px 2px; border-radius:0; }
.vb-msg-foot { display:flex; gap:12px; font-size:11.5px; opacity:0; transition:opacity .15s; }
.vb-agent:hover .vb-msg-foot, .vb-agent:last-of-type .vb-msg-foot, .vb-msg-foot:focus-within { opacity:1; }
.vb-msg-foot .vb-link { color:var(--muted); text-decoration:none; }
.vb-msg-foot .vb-link:hover { color:var(--ink); }
.md > :first-child { margin-top:0; } .md > :last-child { margin-bottom:0; }
.md p { margin:0 0 10px; } .md ul, .md ol { margin:0 0 10px; padding-left:22px; } .md li { margin:3px 0; }
.md h1, .md h2, .md h3, .md h4 { font:600 14px var(--sans); margin:16px 0 6px; }
.md code { background:var(--panel2); padding:1px 5px; border-radius:5px; font-size:12.5px; }
.md a { text-decoration:underline; text-underline-offset:2px; }
.md table { border-collapse:collapse; margin:6px 0 12px; font-size:12.5px; display:block; overflow-x:auto; } .md th, .md td { border:1px solid var(--line); padding:5px 9px; text-align:left; white-space:nowrap; }
.md blockquote { margin:0 0 10px; padding-left:12px; border-left:3px solid var(--line2); color:var(--muted); }
.md-code { margin:8px 0 12px; border:1px solid var(--line); border-radius:12px; overflow:hidden; background:var(--surface); }
.md-code-bar { display:flex; justify-content:space-between; align-items:center; padding:5px 12px; background:var(--panel2); font:11px var(--mono); color:var(--muted); }
.md-copy { border:0; background:none; cursor:pointer; font:500 11px var(--sans); color:var(--muted); }
.md-copy:hover { color:var(--ink); }
.md-code pre { margin:0; padding:12px 14px; overflow:auto; font:12px/1.55 var(--mono); }
.md-code pre code { background:none; padding:0; }

.vb-card { align-self:stretch; border:1px solid var(--line); border-radius:14px; background:var(--surface); font-size:13px; min-width:0; }
.vb-card.bad { border-color:color-mix(in srgb,var(--bad) 30%,var(--line)); }
.vb-stepcard { background:transparent; border-color:transparent; }
.vb-stepcard:hover, .vb-stepcard:has(.vb-step-body) { border-color:var(--line); background:var(--surface); }
.vb-step-head, .vb-group-head { width:100%; display:flex; align-items:center; gap:10px; border:0; background:none; padding:6px 10px; cursor:pointer; color:var(--muted); text-align:left; font:12.5px var(--sans); border-radius:10px; }
.vb-step-head:hover, .vb-group-head:hover { color:var(--ink); }
.vb-step-text { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.vb-chev { color:var(--muted); font-size:10px; }
.vb-dot { color:var(--ok); font-weight:700; width:14px; text-align:center; flex:none; }
.bad > .vb-step-head .vb-dot, .vb-group-steps.bad .vb-group-head .vb-dot, .vb-event.bad .vb-dot { color:var(--bad); }
.vb-step-body { padding:4px 14px 14px; display:flex; flex-direction:column; gap:10px; }
.vb-group-steps { border-radius:14px; border:1px solid transparent; }
.vb-group-steps.open { border-color:var(--line); background:var(--surface); }
.vb-group-count { font-size:11.5px; flex:none; }
.vb-group-body { padding:0 6px 8px; display:flex; flex-direction:column; gap:2px; }
.vb-label { margin:0; font:600 10.5px var(--sans); color:var(--muted); text-transform:uppercase; letter-spacing:.06em; }
.vb-pre { max-height:280px; font-size:11.5px; }
.vb-event, .vb-ask { padding:16px 18px; display:flex; flex-direction:column; gap:12px; }
.vb-ask { border-color:var(--ink); box-shadow:0 8px 24px rgba(0,0,0,.06); }
.vb-event-head { margin:0; display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
.vb-inputs-line { margin:-6px 0 0; }
.vb-plain { margin:0; line-height:1.55; }
.vb-log summary { cursor:pointer; font-size:12px; color:var(--muted); }
.vb-pass { display:flex; gap:8px; }
.vb-pass input { max-width:280px; }
.vb-note-line { align-self:center; font-size:12px; padding:2px 10px; border-radius:99px; background:var(--panel2); }
.vb-working { align-self:flex-start; display:flex; align-items:center; gap:10px; font-size:12.5px; color:var(--muted); padding:6px 10px; }
.vb-spin { width:12px; height:12px; border:2px solid var(--line2); border-top-color:var(--ink); border-radius:50%; display:inline-block; animation:vb-spin .8s linear infinite; flex:none; }
@keyframes vb-spin { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .vb-spin { animation:none; } .vb-meter span, .vb-ideas button { transition:none; } }

.vb-bottom { position:relative; padding:0 32px 18px; background:linear-gradient(to bottom, transparent, var(--bg) 18px); }
.vb-bottom > * { max-width:860px; margin-left:auto; margin-right:auto; }
.vb-jump { position:absolute; left:50%; top:-44px; transform:translateX(-50%); border:1px solid var(--line2); background:var(--surface); color:var(--ink); border-radius:99px; padding:6px 14px; font:500 12px var(--sans); cursor:pointer; box-shadow:0 4px 14px rgba(0,0,0,.08); z-index:3; }
.vb-jump:hover { background:var(--panel2); }
.vb-err { margin-bottom:10px; padding:10px 14px; border-radius:12px; background:color-mix(in srgb,var(--bad) 9%,transparent); font-size:13px; display:flex; gap:12px; align-items:baseline; justify-content:space-between; }
.vb-compose { display:flex; flex-direction:column; gap:6px; border:1px solid var(--line2); border-radius:16px; background:var(--surface); padding:10px 10px 8px 14px; box-shadow:0 1px 2px rgba(0,0,0,.03); transition:border-color .12s; }
.vb-compose:focus-within { border-color:var(--ink); }
.vb-compose.over { background:var(--hover); border-style:dashed; }
.vb-compose-row { display:flex; gap:10px; align-items:flex-end; }
.vb-compose textarea { resize:none; min-height:24px; max-height:260px; line-height:1.5; border:0; padding:4px 0; background:none; box-shadow:none; outline:none; }
.vb-compose textarea:focus-visible { outline:none; }
.vb-hint { margin:0; font-size:11px; }
.vb-chips { display:flex; gap:6px; flex-wrap:wrap; }
.vb-chip { display:inline-flex; align-items:center; gap:6px; background:var(--panel2); border-radius:8px; padding:3px 8px; font-size:12px; }
.vb-chip button { border:0; background:none; cursor:pointer; color:var(--muted); padding:0 2px; }

.vb-files { padding:16px; overflow:auto; display:flex; flex-direction:column; gap:12px; min-width:0; }
.vb-panes { align-self:flex-start; }
.vb-tabs { display:flex; gap:2px; flex-wrap:wrap; }
.vb-tabs button { border:0; background:none; padding:5px 10px; border-radius:8px; font:12px var(--mono); color:var(--muted); cursor:pointer; }
.vb-tabs button.on { background:var(--panel2); color:var(--ink); }
.vb-tabs button.bad { color:var(--bad); }
.vb-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.vb-small { font-size:12.5px; margin:0; line-height:1.5; }
.vb-auto { display:flex; align-items:center; gap:6px; }
.vb-versions { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:8px; }
.vb-versions li { border:1px solid var(--line); border-radius:12px; padding:12px; display:flex; flex-direction:column; gap:10px; }
.vb-version-row { display:flex; flex-direction:column; gap:3px; font-size:13px; }
.vb-files h3 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }

.vb-grid.narrow .vb-pane, .vb-grid.narrow .vb-chat { border:0; grid-column:1; }
.vb-grid.narrow .vb-scroll, .vb-grid.narrow .vb-status, .vb-grid.narrow .vb-bottom { padding-left:16px; padding-right:16px; }
@media (max-width: 700px) { .vb-hide-sm { display:none; } .vb-how { grid-template-columns:repeat(2,minmax(0,1fr)); } }
</style>
