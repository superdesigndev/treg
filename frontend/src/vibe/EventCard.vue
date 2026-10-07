<script setup lang="ts">
// Something the maker did with a button (a test run, a publish, the app, its password), shown in the
// conversation with what to do next: publish a good test, hand a failure to the agent.
import { computed } from 'vue'

import ResultView from '../standalone/ResultView.vue'
import { plain } from './errors'
import type { Msg } from './types'

const props = defineProps<{ msg: Msg, outputFields: string[], hasApp: boolean, last: boolean, busy: boolean }>()
const emit = defineEmits<{ act: [kind: string], send: [text: string], prefill: [text: string] }>()

const failure = computed(() => props.msg.ok ? null : plain(Number(props.msg.status) || 0, props.msg.detail))
const checkFailed = computed(() => props.msg.kind === 'publish' && props.msg.detail?.check?.status === 'failed')
const log = computed<string[]>(() => Array.isArray(props.msg.detail?.log) ? props.msg.detail.log.map(String) : [])
const inputsLine = computed(() => props.msg.inputs && Object.keys(props.msg.inputs).length
  ? Object.entries(props.msg.inputs).map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join(' · ') : '')

function fixText(): string {
  const d = props.msg.detail
  const why = failure.value ? `${failure.value.title}: ${failure.value.text}` : ''
  if (props.msg.kind === 'test') return `Fix it: the test run failed (${props.msg.status}). ${why}`.trim()
  if (checkFailed.value) return `Fix it: publishing ran check.json and it failed: ${JSON.stringify(d?.check?.error ?? d?.check).slice(0, 600)}`
  return `Fix it: ${props.msg.summary}. ${why}`.trim()
}
</script>

<template>
  <div v-if="['skip', 'load', 'restore'].includes(msg.kind || '')" class="vb-note-line sa-muted">{{ msg.summary }}</div>
  <div v-else class="vb-card vb-event" :class="{ bad: !msg.ok || checkFailed }">
    <p class="vb-event-head">
      <span class="vb-dot" aria-hidden="true">{{ msg.ok && !checkFailed ? '✓' : '!' }}</span>
      <b>{{ msg.summary }}</b>
    </p>
    <p v-if="inputsLine" class="sa-muted vb-small vb-inputs-line">with {{ inputsLine }}</p>

    <template v-if="msg.kind === 'test' && msg.ok">
      <ResultView :output="msg.detail?.output" :fields="outputFields" name="test-run"/>
      <details v-if="log.length" class="vb-log"><summary>Log ({{ log.length }})</summary><pre class="sa-json vb-pre">{{ log.join('\n') }}</pre></details>
      <div v-if="last" class="vb-actions">
        <button class="sa-btn sm primary" type="button" :disabled="busy" @click="emit('act', 'publish')">Looks good, publish</button>
        <button class="sa-btn sm" type="button" :disabled="busy" @click="emit('prefill', 'The test result isn\'t right: ')">Something's off</button>
      </div>
    </template>

    <template v-else-if="msg.kind === 'publish' && msg.ok">
      <p v-if="checkFailed" class="sa-error-text">The check failed, so this version is not live. {{ msg.detail?.check?.error?.message || '' }}</p>
      <div class="vb-actions">
        <a v-if="msg.url" class="sa-btn sm" :href="msg.url" target="_blank" rel="noopener">Tool page</a>
        <button v-if="!hasApp && !checkFailed && last" class="sa-btn sm primary" type="button" :disabled="busy" @click="emit('act', 'app')">Turn on the app</button>
        <button v-if="checkFailed" class="sa-btn sm primary" type="button" :disabled="busy" @click="emit('send', fixText())">Fix it</button>
      </div>
    </template>

    <template v-else-if="msg.kind === 'app' && msg.ok">
      <div class="vb-actions">
        <a class="sa-btn sm" :href="msg.url" target="_blank" rel="noopener">Open the app</a>
        <button v-if="last" class="sa-btn sm" type="button" :disabled="busy" @click="emit('act', 'password-card')">Set a password</button>
      </div>
    </template>

    <template v-else-if="failure">
      <p class="vb-plain"><b>{{ failure.title }}.</b> {{ failure.text }}</p>
      <details class="vb-log"><summary>Details</summary><pre class="sa-json vb-pre">{{ typeof msg.detail === 'string' ? msg.detail : JSON.stringify(msg.detail, null, 2) }}</pre></details>
      <div v-if="failure.fixable && last" class="vb-actions">
        <button class="sa-btn sm primary" type="button" :disabled="busy" @click="emit('send', fixText())">Fix it</button>
      </div>
    </template>
  </div>
</template>
