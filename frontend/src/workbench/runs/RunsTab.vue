<script setup lang="ts">
import { computed, ref } from 'vue'
import { useDashboard } from '../../state/context'
import { DataTable, type Column } from '../../components/ui/table'
import StatusPill from '../../components/StatusPill.vue'
import type { Run } from '../api/client'
import AgentLine from '../AgentLine.vue'
import { ago, usd } from '../format'

const props = defineProps<{ runs: Run[] | null }>()
const dash = useDashboard()

const FILTERS = [['all', 'All'], ['running', 'Running'], ['partial', 'Partial'], ['failed', 'Failed'], ['succeeded', 'Succeeded']] as const
const filter = ref<string>('all')
const shown = computed(() => (props.runs || []).filter(r => filter.value === 'all' || r.status === filter.value || (filter.value === 'running' && ['queued', 'stopping', 'finalizing'].includes(r.status))))
const n = (s: string) => (props.runs || []).filter(r => s === 'all' || r.status === s || (s === 'running' && ['queued', 'stopping', 'finalizing'].includes(r.status))).length

const columns: Column<Run>[] = [
  { key: 'started', header: 'Started', mobile: 'field', value: r => ago(r.started_at) },
  { key: 'install', header: 'Install', mobile: 'primary', wrap: true, value: r => r.install || r.tool || 'inline script' },
  { key: 'by', header: 'Started by', mobile: 'field', value: r => r.started_by },
  { key: 'progress', header: 'Progress', mobile: 'wide', minWidth: '220px' },
  { key: 'spent', header: 'Spent', align: 'right', mobile: 'field', value: r => usd(r.spent_micro) },
  { key: 'status', header: 'Status', mobile: 'field' },
]
const pct = (r: Run) => r.progress.total ? Math.round(r.progress.completed / r.progress.total * 100) : 0
</script>

<template>
<section v-if="runs === null" class="pl-empty" aria-busy="true">Loading…</section>
<section v-else-if="!runs.length" class="pl-empty">
  <p>No runs yet. A run is a script or workflow working through rows on the server; it starts from a call, an install or a trigger.</p>
  <AgentLine cli="treg run status <run_id>" mcp="run_status { run_id }" />
</section>
<template v-else>
  <div class="seg pl-seg wb-seg" role="radiogroup" aria-label="Run status">
    <button v-for="[k, l] in FILTERS" :key="k" role="radio" :aria-checked="filter === k" :class="{ on: filter === k }" @click="filter = k">{{ l }} <span>{{ n(k) }}</span></button>
  </div>
  <DataTable :columns="columns" :rows="shown" :row-key="r => r.id" interactive surface empty="No runs in this state." @row-click="r => dash.goWorkbench({ tab: 'runs', runId: r.id })">
    <template #cell-install="{ row: r }"><b>{{ r.install || r.tool || 'inline script' }}</b><span class="pl-sub mono">{{ r.tool }}<template v-if="r.tables?.length"> → {{ r.tables.length }} {{ r.tables.length === 1 ? 'table' : 'tables' }}</template></span></template>
    <template #cell-progress="{ row: r }">
      <span class="wb-bar" :class="r.status"><i :style="{ width: pct(r) + '%' }"></i></span>
      <span class="num">{{ r.progress.completed.toLocaleString() }} / {{ r.progress.total == null ? '?' : r.progress.total.toLocaleString() }}</span>
      <span v-if="r.progress.failed" class="muted small"> · {{ r.progress.failed }} failed</span>
    </template>
    <template #cell-status="{ row: r }"><StatusPill :status="r.status" /><span v-if="r.status === 'failed' && r.trigger" class="muted small"> · trigger paused</span></template>
  </DataTable>
  <AgentLine cli="treg run status <run_id>" mcp="run_status { run_id }" />
</template>
</template>
