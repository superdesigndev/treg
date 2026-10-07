<script setup lang="ts">
// One thing the agent did, as a small card: a line that opens to its arguments and result. A file
// change opens to its diff with Revert; a test run the maker allowed opens to the result table.
import { computed, ref, watch } from 'vue'
import { api } from '../standalone/api'
import ResultView from '../standalone/ResultView.vue'
import DiffView from './DiffView.vue'
import type { Msg } from './types'

const props = defineProps<{ msg: Msg, sessionId: number, latest: boolean, outputFields: string[] }>()
const emit = defineEmits<{ revert: [n: number] }>()
const open = ref(false)
const diff = ref<{ before: any, after: any } | null>(null)
const kept = ref(false)

const isWrite = computed(() => props.msg.name === 'write_files' && !!props.msg.version)
const parsed = computed(() => { try { return JSON.parse(props.msg.result || '') } catch { return null } })
const testOutput = computed(() => props.msg.name === 'test_run' && parsed.value?.ok ? parsed.value.output : undefined)
const pretty = (v: unknown) => typeof v === 'string' ? v : JSON.stringify(v, null, 2)

async function loadDiff() {
  if (diff.value || !props.msg.version) return
  const v = await api(`/vibe/sessions/${props.sessionId}/versions/${props.msg.version}`)
  diff.value = { before: v.prev, after: v.files }
}

watch(() => props.latest, l => { if (l && isWrite.value) { open.value = true; loadDiff() } }, { immediate: true })
function toggle() { open.value = !open.value; if (open.value && isWrite.value) loadDiff() }
</script>

<template>
  <div class="vb-card vb-stepcard" :class="{ bad: !msg.ok }">
    <button type="button" class="vb-step-head" :aria-expanded="open" @click="toggle">
      <span class="vb-dot" aria-hidden="true">{{ msg.ok ? '✓' : '!' }}</span>
      <span class="vb-step-text">{{ msg.summary }}</span>
      <span class="vb-chev" aria-hidden="true">{{ open ? '▾' : '▸' }}</span>
    </button>
    <div v-if="open" class="vb-step-body">
      <template v-if="isWrite">
        <DiffView v-if="diff" :before="diff.before" :after="diff.after"/>
        <p v-else class="sa-muted vb-small">Loading the changes…</p>
        <div v-if="msg.prev_version && !kept" class="vb-actions">
          <button class="sa-btn sm" type="button" @click="kept = true; open = false">Keep</button>
          <button class="sa-btn sm" type="button" @click="emit('revert', msg.prev_version!)">Revert this change</button>
        </div>
      </template>
      <ResultView v-else-if="testOutput !== undefined" :output="testOutput" :fields="outputFields" name="test-run"/>
      <template v-else>
        <p v-if="msg.args && Object.keys(msg.args).length" class="vb-label">Asked with</p>
        <pre v-if="msg.args && Object.keys(msg.args).length" class="sa-json vb-pre">{{ pretty(msg.args) }}</pre>
        <p class="vb-label">Got back</p>
        <pre class="sa-json vb-pre">{{ parsed ? pretty(parsed) : msg.result }}</pre>
      </template>
    </div>
  </div>
</template>
