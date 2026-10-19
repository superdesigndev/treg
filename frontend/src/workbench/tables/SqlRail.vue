<script setup lang="ts">
import { ref } from 'vue'
import Inspector from '../../components/Inspector.vue'
import AgentLine from '../AgentLine.vue'
import type { Table, WorkbenchClient, Schemas } from '../api/client'

const props = defineProps<{ table: Table, client: WorkbenchClient }>()
defineEmits<{ close: [] }>()

const sql = ref(`SELECT ${props.table.columns.slice(0, 3).map(c => c.id).join(', ')}\nFROM ${props.table.name}\nLIMIT 50`)
const result = ref<Schemas['QueryResult'] | null>(null)
const busy = ref(false)
const error = ref('')

async function run() {
  busy.value = true; error.value = ''
  try { result.value = await props.client.query(sql.value) } catch (e: any) { error.value = e?.message || 'The query was refused' } finally { busy.value = false }
}
</script>

<template>
<Inspector title="SQL" closable @close="$emit('close')">
  <textarea v-model="sql" class="wb-sql" rows="5" spellcheck="false" aria-label="SQL" @keydown.meta.enter="run" @keydown.ctrl.enter="run"></textarea>
  <div class="wb-actions">
    <button type="button" class="btn primary" :disabled="busy" @click="run">{{ busy ? 'Running…' : 'Run' }}</button>
    <span class="wb-hint">⌘↵ · read only · one statement · 1,000 rows at most</span>
  </div>
  <p v-if="error" class="banner cn-banner"><span>{{ error }}</span></p>
  <div v-if="result" class="wb-result">
    <p class="wb-hint">{{ result.rows.length.toLocaleString() }} rows<template v-if="result.truncated">, cut at the limit</template> · {{ result.elapsed_ms }} ms</p>
    <div class="wb-result-scroll">
      <table class="wb-result-table">
        <thead><tr><th v-for="c in result.columns" :key="c">{{ c }}</th></tr></thead>
        <tbody><tr v-for="(r, i) in result.rows.slice(0, 200)" :key="i"><td v-for="(v, j) in r" :key="j">{{ v == null ? '' : String(v) }}</td></tr></tbody>
      </table>
    </div>
  </div>
  <template #foot><AgentLine cli='treg query "SELECT …"' mcp="query { sql }" /></template>
</Inspector>
</template>
