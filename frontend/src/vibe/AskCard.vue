<script setup lang="ts">
// The agent asks; the maker decides. A test run shows what it will cost and what to fix first; a
// password is typed here and goes to the app only, never to the agent.
import { computed, ref } from 'vue'
import { usd } from '../standalone/api'
import type { Pending, Warning } from './types'

const props = defineProps<{ pending: Pending, team: string, name: string, toolId: string | null, warnings: Warning[], busy: boolean }>()
const emit = defineEmits<{ act: [kind: string, extra: Record<string, unknown>] }>()
const password = ref('')
const confirmRemove = () => confirm('Remove the app password? Anyone signed in can open the app, other teams can call the tool, and it can show in search again.')
const show = ref(false)

const cost = computed(() => props.pending.est_cost_usd == null ? '' : props.pending.est_cost_usd === 0 ? 'free'
  : `about ${usd(Math.round(props.pending.est_cost_usd * 1e6))}`)
const inputs = computed(() => Object.entries(props.pending.inputs || {}))
</script>

<template>
  <div class="vb-card vb-ask" role="group" aria-label="The agent asks">
    <template v-if="pending.kind === 'test'">
      <p class="vb-event-head"><b>Run a test?</b> <span class="sa-muted vb-small">Real calls, on {{ team }}'s balance{{ cost ? ` · ${cost}` : '' }}</span></p>
      <p v-if="inputs.length" class="sa-muted vb-small vb-inputs-line">with
        <span v-for="([k, v], i) in inputs" :key="k">{{ i ? ' · ' : '' }}{{ k }}: <code>{{ typeof v === 'string' ? v : JSON.stringify(v) }}</code></span></p>
      <ul v-if="warnings.length" class="vb-warns">
        <li v-for="w in warnings" :key="w.kind === 'access' ? w.id : 'balance'">
          <template v-if="w.kind === 'access'">Your team can't call <code>{{ w.id }}</code> yet, so this run will fail.
            <span v-if="w.fix"> Fix: <code>{{ w.fix }}</code></span></template>
          <template v-else>Balance {{ usd(w.balance_micro) }} is under what a run costs ({{ usd(w.need_micro) }}).</template>
        </li>
      </ul>
      <div class="vb-actions">
        <button class="sa-btn sm primary" type="button" :disabled="busy" @click="emit('act', 'test', { inputs: pending.inputs || {} })">
          Run test{{ cost && cost !== 'free' ? ` · ${cost.replace('about ', '~')}` : '' }}</button>
        <button class="sa-btn sm" type="button" :disabled="busy" @click="emit('act', 'test', { inputs: pending.inputs || {}, always: true })">Run, and don't ask again here</button>
        <button class="sa-btn sm" type="button" :disabled="busy" @click="emit('act', 'skip', {})">Not now</button>
      </div>
    </template>

    <template v-else-if="pending.kind === 'publish'">
      <p class="vb-event-head"><b>Publish {{ toolId ? 'a new version of' : '' }} <code>{{ team }}.{{ name || '…' }}</code>?</b></p>
      <p class="sa-muted vb-small">check.json runs once for real; the tool goes live when it passes. Other teams can call it by id.</p>
      <div class="vb-actions">
        <button class="sa-btn sm primary" type="button" :disabled="busy" @click="emit('act', 'publish', {})">Publish</button>
        <button class="sa-btn sm" type="button" :disabled="busy" @click="emit('act', 'skip', {})">Not now</button>
      </div>
    </template>

    <template v-else-if="pending.kind === 'app'">
      <p class="vb-event-head"><b>Turn on the app?</b></p>
      <p class="sa-muted vb-small">A web page at <code>/apps/{{ team }}/{{ pending.name || name }}</code> where signed-in people fill a form and run the tool on their own team's balance.</p>
      <div class="vb-actions">
        <button class="sa-btn sm primary" type="button" :disabled="busy" @click="emit('act', 'app', pending.name ? { name: pending.name } : {})">Turn on the app</button>
        <button class="sa-btn sm" type="button" :disabled="busy" @click="emit('act', 'skip', {})">Not now</button>
      </div>
    </template>

    <template v-else-if="pending.kind === 'password'">
      <p class="vb-event-head"><b>Password for the app</b></p>
      <p class="sa-muted vb-small">Optional. With one, people need it to open the page, other teams need it to call the tool, and the tool leaves search.
        It goes to the app only; the agent never sees it.</p>
      <form class="vb-pass" @submit.prevent="password.length >= 8 && emit('act', 'password', { password })">
        <input v-model="password" :type="show ? 'text' : 'password'" autocomplete="new-password" placeholder="8 to 128 characters" minlength="8" maxlength="128" aria-label="App password"/>
        <button class="sa-btn sm" type="button" @click="show = !show">{{ show ? 'Hide' : 'Show' }}</button>
        <button class="sa-btn sm primary" type="submit" :disabled="busy || password.length < 8">Set password</button>
      </form>
      <div class="vb-actions">
        <button class="sa-btn sm" type="button" :disabled="busy" @click="confirmRemove() && emit('act', 'password', { password: null, clear: true })">Remove the password</button>
        <button class="sa-btn sm" type="button" :disabled="busy" @click="emit('act', 'skip', {})">Not now</button>
        <a class="sa-muted vb-small" href="/app#hub" target="_blank" rel="noopener">or in the dashboard's App tab</a>
      </div>
    </template>
  </div>
</template>
