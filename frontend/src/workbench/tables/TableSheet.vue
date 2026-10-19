<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useDashboard } from '../../state/context'
import DataGrid from '../../components/DataGrid.vue'
import StatusPill from '../../components/StatusPill.vue'
import { workbenchClient, type Row, type Run, type Table } from '../api/client'
import { count, usd } from '../format'
import CellRail from './CellRail.vue'
import SqlRail from './SqlRail.vue'
import TableRail from './TableRail.vue'

const props = defineProps<{ tableId: string }>()
const dash = useDashboard()
const client = workbenchClient((path, opts) => dash.api(path, opts))

const table = ref<Table | null>(null)
const rows = ref<Row[]>([])
const nextCursor = ref<string | null>(null)
const runs = ref<Run[]>([])
const error = ref('')
const loadingMore = ref(false)

const picked = computed(() => dash.wb.cell as { row: string, column: string } | null)
const panel = computed(() => dash.wb.panel as 'sql' | 'add' | null)
const pickedRow = computed(() => picked.value ? rows.value.find(r => r.id === picked.value!.row) || null : null)
const pickedColumn = computed(() => picked.value && table.value ? table.value.columns.find(c => c.id === picked.value!.column) || null : null)
const live = computed(() => runs.value.filter(r => ['queued', 'running', 'stopping', 'finalizing'].includes(r.status)))
const retryable = computed(() => runs.value.find(r => r.progress.failed > 0 && r.failed_rows_until && !['queued', 'running'].includes(r.status)) || null)

async function load() {
  error.value = ''
  try {
    const [t, page, list] = await Promise.all([client.getTable(props.tableId), client.listRows(props.tableId), client.listRuns({ table: props.tableId })])
    table.value = t
    rows.value = page.rows
    nextCursor.value = page.next_cursor ?? null
    runs.value = list.runs
  } catch (e: any) {
    error.value = e?.status === 404 ? 'No table with that id in this team.' : e?.message || 'Could not load the table'
  }
}
async function more() {
  if (!nextCursor.value || loadingMore.value) return
  loadingMore.value = true
  try {
    const page = await client.listRows(props.tableId, nextCursor.value)
    rows.value = [...rows.value, ...page.rows]
    nextCursor.value = page.next_cursor ?? null
  } finally { loadingMore.value = false }
}
async function stop(run: Run) {
  try { const r = await client.stopRun(run.id); runs.value = runs.value.map(x => x.id === r.id ? r : x) } catch (e: any) { error.value = e?.message || 'Could not stop the run' }
}
const pick = (row: string, column: string) => dash.goWorkbench({ cell: { row, column }, panel: null })
const closeRail = () => dash.goWorkbench({ cell: null, panel: null })
const openSql = () => dash.goWorkbench({ cell: null, panel: 'sql' })

onMounted(load)
watch(() => props.tableId, load)
</script>

<template>
<div class="wb-sheet">
  <p class="wb-crumb"><a href="#workbench/tables" @click.prevent="dash.goWorkbench({ tab: 'tables', tableId: null, cell: null, panel: null })">← Tables</a></p>
  <p v-if="error" class="banner cn-banner"><span>{{ error }}</span></p>
  <template v-else-if="table">
    <div class="wb-bar">
      <span class="wb-title">{{ table.name }}</span>
      <span class="muted small num">{{ count(table.row_count, 'row') }} · {{ count(table.columns.length, 'column') }}</span>
      <span v-for="r in live" :key="r.id" class="wb-runchip">
        <StatusPill :status="r.status" :label="r.install || r.tool || 'run'" />
        <span class="num">{{ r.progress.completed.toLocaleString() }} / {{ (r.progress.total ?? r.progress.completed).toLocaleString() }}</span>
        <span class="num">{{ usd(r.spent_micro) }}</span>
        <button type="button" class="btn sm ghost" :disabled="r.status === 'stopping'" @click="stop(r)">{{ r.status === 'stopping' ? 'Stopping…' : 'Stop' }}</button>
      </span>
      <span class="wb-spacer"></span>
      <button v-if="retryable" type="button" class="btn" @click="dash.goWorkbench({ tab: 'runs', runId: retryable.id })">Retry {{ retryable.progress.failed }} failed rows</button>
      <button type="button" class="btn" :class="{ on: panel === 'sql' }" @click="openSql">SQL</button>
      <button type="button" class="btn" disabled title="Export arrives with the runs that produce files">Export</button>
      <button type="button" class="btn primary" disabled title="Add column arrives with installs">+ Add column</button>
    </div>
    <div class="wb-layout">
      <div class="wb-gridcol">
        <DataGrid :columns="table.columns" :rows="rows" :picked="picked" add-column @pick="pick" />
        <button v-if="nextCursor" type="button" class="pl-link pl-more-rows" :disabled="loadingMore" @click="more">{{ loadingMore ? 'Loading…' : 'Load more rows' }}</button>
      </div>
      <CellRail v-if="pickedRow && pickedColumn" :row="pickedRow" :column="pickedColumn" :row-index="rows.indexOf(pickedRow) + 1" @close="closeRail" />
      <SqlRail v-else-if="panel === 'sql'" :table="table" :client="client" @close="closeRail" />
      <TableRail v-else :table="table" :runs="runs" />
    </div>
  </template>
</div>
</template>
