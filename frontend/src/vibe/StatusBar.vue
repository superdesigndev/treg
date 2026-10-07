<script setup lang="ts">
// The tool this conversation built: its version and state, the app, runs by others, where it lives,
// and what to fix before the next test run.
import { ref } from 'vue'
import { usd } from '../standalone/api'
import type { ToolStatus, Warning } from './types'

defineProps<{ tool: ToolStatus | null, warnings: Warning[] }>()
const copied = ref('')
async function copy(text: string) {
  try { await navigator.clipboard.writeText(text); copied.value = text; setTimeout(() => (copied.value = ''), 1400) } catch { /* no clipboard */ }
}
const stateWord: Record<string, string> = { live: 'live', checking: 'checking', failed: 'check failed', review: 'in review', retired: 'retired' }
</script>

<template>
  <div v-if="tool || warnings.length" class="vb-status">
    <div v-if="tool" class="vb-status-row">
      <code class="vb-status-id">{{ tool.tool_id }}</code>
      <span class="vb-pill">v{{ tool.version }}</span>
      <span class="vb-pill" :class="tool.status === 'live' ? 'ok' : tool.status === 'failed' ? 'bad' : ''">{{ stateWord[tool.status] || tool.status }}</span>
      <span v-if="tool.live_version && tool.live_version !== tool.version" class="sa-muted vb-small">callers get v{{ tool.live_version }}</span>
      <span v-if="tool.health === 'failing'" class="vb-pill bad">failing</span>
      <span class="vb-pill" :class="tool.app?.enabled ? 'ok' : ''">app {{ tool.app?.enabled ? (tool.app.locked ? 'on · locked' : 'on') : 'off' }}</span>
      <span class="sa-muted vb-small">{{ tool.runs_30d }} run{{ tool.runs_30d === 1 ? '' : 's' }} by others in 30 days</span>
      <span class="vb-status-links">
        <a v-if="tool.app?.enabled" :href="tool.app.url" target="_blank" rel="noopener">Open app</a>
        <a :href="tool.share_url" target="_blank" rel="noopener">Share page</a>
        <button type="button" class="vb-link" @click="copy(tool.call)">{{ copied === tool.call ? 'Copied' : 'Copy call line' }}</button>
      </span>
    </div>
    <ul v-if="warnings.length" class="vb-warns">
      <li v-for="w in warnings" :key="w.kind === 'access' ? w.id : 'balance'">
        <template v-if="w.kind === 'access'">
          Your team can't call <code>{{ w.id }}</code> yet, so a test run will fail.
          <template v-if="w.fix"> Fix: <code>{{ w.fix }}</code> <button type="button" class="vb-link" @click="copy(w.fix!)">{{ copied === w.fix ? 'Copied' : 'Copy' }}</button></template>
        </template>
        <template v-else>Your balance ({{ usd(w.balance_micro) }}) is under what one run of these tools costs (about {{ usd(w.need_micro) }}). <a href="/app#billing" target="_blank" rel="noopener">Top up</a></template>
      </li>
    </ul>
  </div>
</template>
