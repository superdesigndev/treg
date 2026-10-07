<script setup lang="ts">
// A hub tool's web page, `/apps/<team>/<name>` (docs/context/architecture/hub-apps.md). The visitor
// fills the form, runs the tool as their own team through the ordinary call road, reads the result and
// their own runs. Nothing here decides price or access: the server does, and this page words its answer.
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ApiError, api, me, setTeam, signInUrl, teams, usd, when, type Team } from '../standalone/api'
import { build, initial } from '../standalone/form'
import InputForm from '../standalone/InputForm.vue'
import ResultView from '../standalone/ResultView.vue'

const base = location.pathname.replace(/\/+$/, '')
const state = ref<'loading' | 'missing' | 'locked' | 'ready' | 'error'>('loading')
const contract = ref<any>(null)
const lockedInfo = ref<any>(null)
const password = ref('')
const unlockError = ref('')
const unlocking = ref(false)
const email = ref<string | null>(null)
const myTeams = ref<Team[]>([])
const team = ref('')
const values = ref<Record<string, unknown>>({})
const errors = ref<Record<string, string>>({})
const running = ref(false)
const elapsed = ref(0)
const result = ref<any>(null)
const runError = ref<{ title: string, body: string, action?: { label: string, href: string } } | null>(null)
const runs = ref<any[]>([])
const openRun = ref<any>(null)
let timer: number | undefined

const teamKey = `treg-app-team:${base}`
const duration = (ms?: number) => !ms ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
const fields = computed<string[]>(() => contract.value?.output || [])

async function loadContract() {
  try {
    const c = await api(`${base}/contract`)
    if (c.locked) { lockedInfo.value = c; state.value = 'locked'; return }
    contract.value = c
    values.value = initial(c.inputs)
    document.title = `${c.name} · treg`
    state.value = 'ready'
  } catch (e) {
    state.value = e instanceof ApiError && e.status === 404 ? 'missing' : 'error'
  }
}

async function loadRuns() {
  if (!email.value || !team.value) return
  try { runs.value = (await api(`${base}/runs`)).runs } catch { runs.value = [] }
}

function pickTeam(slug: string) {
  team.value = slug
  setTeam(slug)
  try { localStorage.setItem(teamKey, slug) } catch { /* a convenience only */ }
  openRun.value = null
  loadRuns()
}

async function unlock() {
  unlocking.value = true
  unlockError.value = ''
  try {
    await api(`${base}/unlock`, { method: 'POST', json: { password: password.value } })
    password.value = ''
    await loadContract()
    await loadRuns()
  } catch (e) {
    unlockError.value = e instanceof ApiError && e.status === 429 ? 'Too many tries. Wait a few minutes and try again.' : 'That password is not right.'
  } finally {
    unlocking.value = false
  }
}

function explain(e: unknown) {
  if (!(e instanceof ApiError)) return { title: 'The run did not finish', body: 'The connection dropped. Check your runs on the right in a moment; it may still have completed.' }
  const d = e.detail || {}
  const kind = typeof d === 'object' ? d.error : ''
  if (e.status === 401 && kind === 'app_locked') { state.value = 'locked'; return null }
  if (e.status === 401) return { title: 'Sign in to run this', body: 'Runs are paid by your own treg team.', action: { label: 'Sign in', href: signInUrl() } }
  if (e.status === 402) return { title: 'Not enough balance', body: (typeof d === 'object' && d.message) || `Team ${team.value} needs more balance for this run.`, action: { label: 'Add balance', href: '/app#orgs' } }
  if (e.status === 422 && typeof d === 'object' && d.field) { errors.value = { ...errors.value, [d.field]: d.rule || d.message || 'Not accepted' }; return { title: 'Check the highlighted field', body: d.message || '' } }
  if (e.status === 424) return { title: 'The tool could not finish', body: `Step ${d.step || ''} failed${d.status ? ` (${d.status})` : ''}. No seller price was charged; steps that completed are paid.` }
  if (e.status === 429) return { title: 'Busy', body: d.message || `Your team has several runs in flight. Try again in ${d.retry_after_s || 5} s.` }
  if (e.status === 403) return { title: 'Not allowed', body: (typeof d === 'object' && d.message) || String(d) }
  return { title: `The run failed (${e.status})`, body: (typeof d === 'object' && (d.message || d.error)) || String(d || '') }
}

async function run() {
  const built = build(contract.value.inputs, values.value)
  errors.value = built.errors
  if (Object.keys(built.errors).length) return
  if (!email.value) { location.href = signInUrl(); return }
  if (!team.value) return
  running.value = true
  runError.value = null
  result.value = null
  openRun.value = null
  elapsed.value = 0
  const started = Date.now()
  timer = window.setInterval(() => (elapsed.value = Math.round((Date.now() - started) / 1000)), 500)
  try {
    result.value = await api(`${base}/run`, { method: 'POST', json: built.body })
  } catch (e) {
    runError.value = explain(e)
  } finally {
    running.value = false
    clearInterval(timer)
    loadRuns()
  }
}

async function showRun(r: any) {
  try { openRun.value = await api(`${base}/runs/${r.run_id}`); result.value = null; runError.value = null } catch { openRun.value = null }
}

onMounted(async () => {
  const who = await me()
  email.value = who?.email || null
  if (email.value) {
    myTeams.value = await teams()
    let saved = ''
    try { saved = localStorage.getItem(teamKey) || localStorage.getItem('treg-active') || '' } catch { /* none */ }
    const first = myTeams.value.find(t => t.slug === saved) || myTeams.value[0]
    if (first) { team.value = first.slug; setTeam(first.slug) }
  }
  await loadContract()
  loadRuns()
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="sa-page">
    <header class="sa-top">
      <a class="sa-brand" href="/" aria-label="treg home">treg</a>
      <div class="sa-who">
        <template v-if="email">
          <label v-if="myTeams.length > 1" class="sa-team">Paid by
            <select :value="team" @change="pickTeam(($event.target as HTMLSelectElement).value)">
              <option v-for="t in myTeams" :key="t.slug" :value="t.slug">{{ t.name || t.slug }}</option>
            </select>
          </label>
          <span v-else-if="team" class="sa-muted">Paid by {{ team }}</span>
          <span class="sa-muted sa-email">{{ email }}</span>
        </template>
        <a v-else class="sa-btn" :href="signInUrl()">Sign in</a>
      </div>
    </header>

    <main v-if="state === 'loading'" class="sa-main"><p class="sa-muted" role="status">Loading…</p></main>

    <main v-else-if="state === 'missing'" class="sa-main sa-narrow">
      <h1>No app here</h1>
      <p class="sa-muted">This app does not exist or its maker turned it off.</p>
    </main>

    <main v-else-if="state === 'error'" class="sa-main sa-narrow">
      <h1>Something went wrong</h1>
      <p class="sa-muted">The app could not load. Reload the page to try again.</p>
    </main>

    <main v-else-if="state === 'locked'" class="sa-main sa-narrow">
      <p class="sa-kicker">by {{ lockedInfo?.maker }}</p>
      <h1>{{ lockedInfo?.name }}</h1>
      <form class="sa-card sa-lock" @submit.prevent="unlock">
        <label for="app-password">This app is password protected</label>
        <input id="app-password" v-model="password" type="password" autocomplete="current-password" required autofocus/>
        <p v-if="unlockError" class="sa-error-text" role="alert">{{ unlockError }}</p>
        <button class="sa-btn primary" type="submit" :disabled="unlocking || !password">{{ unlocking ? 'Checking…' : 'Unlock' }}</button>
      </form>
    </main>

    <main v-else class="sa-main sa-grid">
      <div class="sa-col">
        <p class="sa-kicker">by {{ contract.maker }} · v{{ contract.version }}<span v-if="contract.health === 'failing'" class="sa-warn"> · recent runs failed</span></p>
        <h1>{{ contract.name }}</h1>
        <p class="sa-summary">{{ contract.summary }}</p>
        <p v-if="contract.price.free" class="sa-price"><b>Free to run</b></p>
        <p v-else class="sa-price">
          <b>{{ contract.price.price_range }}</b>
          <span class="sa-muted"> · {{ contract.price.seller }}<template v-if="contract.price.fees"> · {{ contract.price.fees }}</template> · {{ contract.price.note }}</span>
        </p>

        <form class="sa-card" @submit.prevent="run">
          <InputForm v-model="values" :inputs="contract.inputs" :errors="errors" :disabled="running"/>
          <div class="sa-run-row">
            <button class="sa-btn primary" type="submit" :disabled="running || (!!email && !team)">
              {{ running ? `Running… ${elapsed}s` : email ? 'Run' : 'Sign in to run' }}
            </button>
            <span v-if="running" class="sa-muted">Runs can take up to {{ contract.limits.wall_s }} s.</span>
            <span v-else-if="email && team" class="sa-muted">Charged to {{ team }}.</span>
          </div>
          <p v-if="email && !team" class="sa-note">
            Runs are paid by a treg team, and you are not in one yet.
            <a href="/app">Create a team</a>, then come back to this page.
          </p>
        </form>

        <div v-if="runError" class="sa-card sa-error" role="alert">
          <b>{{ runError.title }}</b>
          <p>{{ runError.body }}</p>
          <a v-if="runError.action" class="sa-btn" :href="runError.action.href">{{ runError.action.label }}</a>
        </div>

        <div v-if="result" class="sa-card">
          <p class="sa-run-meta">
            <span class="sa-ok">Done</span> in {{ duration(result.usage?.ms) }} · {{ usd(result.usage?.cost_micro) }}
            <template v-if="result.usage?.steps"> · {{ result.usage.steps }} tool call{{ result.usage.steps === 1 ? '' : 's' }}</template>
          </p>
          <ResultView :output="result.output" :fields="fields" :name="contract.app"/>
        </div>

        <div v-if="openRun" class="sa-card">
          <p class="sa-run-meta">
            <span :class="openRun.status === 'ok' ? 'sa-ok' : 'sa-bad'">{{ openRun.status === 'ok' ? 'Done' : 'Failed' }}</span>
            {{ when(openRun.started_at) }} · {{ usd(openRun.cost_micro) }}
            <button class="sa-btn sm" type="button" @click="openRun = null">Close</button>
          </p>
          <details class="sa-inputs"><summary>Inputs</summary><pre class="sa-json">{{ JSON.stringify(openRun.inputs, null, 2) }}</pre></details>
          <ResultView v-if="openRun.output" :output="openRun.output" :fields="fields" :name="contract.app"/>
          <p v-else-if="openRun.error" class="sa-muted">Step {{ openRun.error.step || '' }} failed{{ openRun.error.status ? ` (${openRun.error.status})` : '' }}.</p>
        </div>

        <details v-if="contract.readme" class="sa-card sa-about">
          <summary>About this tool</summary>
          <pre class="sa-readme">{{ contract.readme }}</pre>
          <p class="sa-muted"><a :href="contract.share_page">Tool page</a> · callable by agents as <code>{{ contract.tool_id }}</code></p>
        </details>
      </div>

      <aside class="sa-side">
        <h2>Your runs</h2>
        <p v-if="!email" class="sa-muted">Sign in to see your runs.</p>
        <p v-else-if="!runs.length" class="sa-muted">No runs yet. Kept for 30 days.</p>
        <ul v-else class="sa-runs">
          <li v-for="r in runs" :key="r.run_id">
            <button type="button" :class="{ on: openRun?.run_id === r.run_id }" @click="showRun(r)">
              <span :class="r.status === 'ok' ? 'sa-ok' : 'sa-bad'">{{ r.status === 'ok' ? 'Done' : 'Failed' }}</span>
              <span class="sa-muted">{{ when(r.started_at) }}</span>
              <span class="sa-num">{{ usd(r.cost_micro) }}</span>
            </button>
          </li>
        </ul>
      </aside>
    </main>

    <footer class="sa-foot"><a href="/">Made with treg</a></footer>
  </div>
</template>
