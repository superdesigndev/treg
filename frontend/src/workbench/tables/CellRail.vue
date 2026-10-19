<script setup lang="ts">
import { computed, ref } from 'vue'
import Inspector from '../../components/Inspector.vue'
import StatusPill from '../../components/StatusPill.vue'
import type { Row, Schemas } from '../api/client'
import { cellText, usd, when } from '../format'

const props = defineProps<{ row: Row, column: Schemas['ColumnMetadata'], rowIndex: number }>()
defineEmits<{ close: [] }>()

const value = computed(() => props.row.cells[props.column.id])
const text = computed(() => cellText(value.value))
const isLink = computed(() => /^(https?:\/\/|linkedin\.|www\.)/i.test(text.value))
const href = computed(() => /^https?:\/\//i.test(text.value) ? text.value : `https://${text.value}`)
const group = computed(() => {
  const meta = props.row.meta || {}
  if (!props.column.filled_by) return null
  return meta[props.column.id] || Object.values(meta).find(m => m.value_source?.install === props.column.filled_by) || Object.values(meta)[0] || null
})
const source = computed(() => group.value?.value_source || null)
const attempt = computed(() => group.value?.latest_attempt || null)
const attemptDiffers = computed(() => !!attempt.value && (!source.value || attempt.value.run_id !== source.value.run_id || attempt.value.status !== 'succeeded'))

const copied = ref(false)
async function copy() { try { await navigator.clipboard.writeText(text.value); copied.value = true; setTimeout(() => (copied.value = false), 1200) } catch {} }
</script>

<template>
<Inspector :title="`${column.name} · row ${rowIndex}`" closable @close="$emit('close')">
  <div>
    <div class="wb-cellhead"><h4>Value</h4><span class="wb-spacer"></span>
      <button type="button" class="btn sm ghost" :disabled="value == null" @click="copy">{{ copied ? 'Copied' : 'Copy' }}</button></div>
    <a v-if="value != null && isLink" class="wb-cellfull link" :href="href" target="_blank" rel="noopener noreferrer">{{ text }}</a>
    <pre v-else-if="value != null">{{ text }}</pre>
    <p v-else class="wb-hint">Empty<template v-if="attempt">: {{ attempt.status === 'no_result' ? 'no result' : attempt.status.replace(/_/g, ' ') }}</template>.</p>
  </div>

  <div v-if="column.filled_by">
    <h4>Value source</h4>
    <p class="wb-hint" style="margin-top:4px">The call that produced the value stored now.</p>
    <dl v-if="source" class="kv">
      <dt>Install</dt><dd><a :href="`#tools/i/${source.install}`">{{ source.install }}</a></dd>
      <dt>Tool</dt><dd>{{ source.tool }}</dd>
      <dt>Provider</dt><dd>{{ source.provider }}</dd>
      <dt>Cost</dt><dd class="num">{{ usd(source.cost_micro) }} <span class="muted">for this column group</span></dd>
      <dt v-if="source.confidence != null">Confidence</dt><dd v-if="source.confidence != null" class="num">{{ Math.round(source.confidence * 100) }}%</dd>
      <dt>When</dt><dd>{{ when(source.at) }}</dd>
      <dt>Call</dt><dd class="mono">{{ source.call_id }}</dd>
      <dt>Run</dt><dd><a :href="`#workbench/runs/${source.run_id}`">{{ source.run_id }}</a></dd>
    </dl>
    <p v-else class="wb-hint" style="margin-top:6px">No value from a tool yet.</p>
  </div>

  <div v-if="column.filled_by && attempt && attemptDiffers">
    <h4>Latest attempt</h4>
    <dl class="kv">
      <dt>Status</dt><dd><StatusPill :status="attempt.status" /><span v-if="attempt.status === 'no_result'" class="muted"> · a miss is data, not a failure</span></dd>
      <dt v-if="attempt.error">Error</dt><dd v-if="attempt.error">{{ attempt.error }}</dd>
      <dt>When</dt><dd>{{ when(attempt.at) }}</dd>
      <dt v-if="attempt.run_id">Run</dt><dd v-if="attempt.run_id"><a :href="`#workbench/runs/${attempt.run_id}`">{{ attempt.run_id }}</a></dd>
    </dl>
    <p class="wb-hint" style="margin-top:8px">A failed attempt never overwrites a good value's source.</p>
  </div>
  <p v-else-if="!column.filled_by" class="wb-hint">A column you typed or imported. Tools map to it by its type<template v-if="column.type"> ({{ column.type.replace(/_/g, ' ') }})</template>.</p>
</Inspector>
</template>
