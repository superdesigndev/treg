<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useDashboard } from '../../state/context'
import { DataTable, type Column } from '../../components/ui/table'
import Inspector from '../../components/Inspector.vue'
import StatusPill from '../../components/StatusPill.vue'
import { workbenchClient, type RunDetail, type Schemas } from '../api/client'
import AgentLine from '../AgentLine.vue'
import { usd, when } from '../format'

type Step = Schemas['TraceStep']
const props = defineProps<{ runId: string }>()
const dash = useDashboard()
const client = workbenchClient((path, opts) => dash.api(path, opts))

const run = ref<RunDetail | null>(null)
const error = ref('')
const busy = ref(false)

async function load() {
  error.value = ''
  try { run.value = await client.getRun(props.runId) } catch (e: any) { error.value = e?.status === 404 ? 'No run with that id in this team.' : e?.message || 'Could not load the run' }
}
async function stop() { if (!run.value) return; busy.value = true; try { const r = await client.stopRun(run.value.id); run.value = { ...run.value, ...r } } catch (e: any) { error.value = e?.message } finally { busy.value = false } }
async function retry() { if (!run.value) return; busy.value = true; try { const h = await client.retryFailedRows(run.value.id); dash.goWorkbench({ tab: 'runs', runId: h.run_id }) } catch (e: any) { error.value = e?.message } finally { busy.value = false } }

const live = computed(() => !!run.value && ['queued', 'running', 'stopping', 'finalizing'].includes(run.value.status))
const retryable = computed(() => !!run.value && run.value.progress.failed > 0 && !!run.value.failed_rows_until && !live.value)
const duration = computed(() => {
  if (!run.value) return ''
  const ms = (run.value.finished_at ? new Date(run.value.finished_at).getTime() : Date.now()) - new Date(run.value.started_at).getTime()
  return ms < 90000 ? `${Math.round(ms / 1000)} s` : `${Math.round(ms / 60000)} min`
})
const columns: Column<Step>[] = [
  { key: 'row', header: 'Row', mobile: 'primary', value: s => `${s.row}${s.row_label ? ' · ' + s.row_label : ''}` },
  { key: 'call', header: 'Call', mobile: 'field', value: s => s.call },
  { key: 'outcome', header: 'Result', mobile: 'field' },
  { key: 'provider', header: 'Provider', mobile: 'field', value: s => s.provider || '' },
  { key: 'cost', header: 'Cost', align: 'right', mobile: 'field', value: s => s.cost_micro ? usd(s.cost_micro) : '' },
  { key: 'ms', header: 'ms', align: 'right', mobile: 'hide', value: s => s.ms ?? '' },
]

onMounted(load)
watch(() => props.runId, load)
</script>

<template>
<div class="wb-sheet">
  <p class="wb-crumb"><a href="#workbench/runs" @click.prevent="dash.goWorkbench({ tab: 'runs', runId: null })">← Runs</a></p>
  <p v-if="error" class="banner cn-banner"><span>{{ error }}</span></p>
  <template v-if="run">
    <div class="wb-bar">
      <span class="wb-title">Run <span class="mono">{{ run.id }}</span></span>
      <StatusPill :status="run.status" />
      <span class="wb-spacer"></span>
      <button v-if="live" type="button" class="btn" :disabled="busy || run.status === 'stopping'" @click="stop">{{ run.status === 'stopping' ? 'Stopping…' : 'Stop' }}</button>
      <button v-if="retryable" type="button" class="btn primary" :disabled="busy" @click="retry">Retry {{ run.progress.failed }} failed rows</button>
    </div>
    <p class="cat-hint" style="margin:0 0 12px">
      <a v-if="run.install" :href="`#tools/i/${run.install}`">{{ run.install }}</a><template v-if="run.install"> · </template><span class="mono">{{ run.tool || 'inline script' }}</span>
      · started {{ when(run.started_at) }} by {{ run.started_by }}<template v-if="run.retry_of_run_id"> · retry of <a :href="`#workbench/runs/${run.retry_of_run_id}`">{{ run.retry_of_run_id }}</a></template>
    </p>
    <div class="statgrid wb-stats">
      <div class="stat"><div class="n">{{ run.progress.completed.toLocaleString() }} <small>/ {{ run.progress.total == null ? '?' : run.progress.total.toLocaleString() }}</small></div><div class="l">rows done</div></div>
      <div class="stat"><div class="n">{{ usd(run.spent_micro) }}</div><div class="l">spent · cap {{ usd(run.max_cost_micro) }}</div></div>
      <div class="stat"><div class="n">{{ run.progress.failed }}</div><div class="l">failed rows<template v-if="run.failed_rows_until"> · retry until {{ when(run.failed_rows_until) }}</template></div></div>
      <div class="stat"><div class="n">{{ duration }}</div><div class="l">{{ run.finished_at ? 'duration' : 'running for' }}</div></div>
    </div>
    <p v-if="run.error" class="wb-note bad"><b>{{ run.status === 'failed' ? 'Failed.' : 'Stopped.' }}</b> {{ run.error }}<template v-if="run.trigger"> Trigger <b>{{ run.trigger }}</b> is paused; nothing retries quietly.</template></p>
    <p v-else-if="run.status === 'partial'" class="wb-note warn"><b>Partial.</b> {{ run.progress.succeeded.toLocaleString() }} rows done, {{ run.progress.failed }} errored. Rows with no result count as done.</p>
    <div class="wb-layout">
      <div class="wb-gridcol">
        <h4 class="wb-h4">Trace <span class="muted">· {{ run.trace.length }} of {{ run.progress.completed.toLocaleString() }} rows shown</span></h4>
        <DataTable :columns="columns" :rows="run.trace" :row-key="s => String(s.row)" surface empty="No steps yet.">
          <template #cell-outcome="{ row: s }"><span :class="{ ok: s.outcome === 'ok', bad: s.outcome === 'failed', muted: s.outcome !== 'ok' && s.outcome !== 'failed' }">{{ s.outcome.replace(/_/g, ' ') }}</span><span v-if="s.detail" class="muted small"> · {{ s.detail }}</span></template>
        </DataTable>
        <p v-if="run.trace_next_cursor" class="wb-hint">More rows in the trace. Paging arrives with the server.</p>
      </div>
      <Inspector title="Run">
        <dl class="kv" style="margin:0">
          <dt>Handle</dt><dd class="mono">{{ run.id }}</dd>
          <dt>Source</dt><dd>{{ run.source }}<template v-if="run.trigger"> · trigger {{ run.trigger }}</template></dd>
          <dt v-if="run.snapshot?.event_key">Event</dt><dd v-if="run.snapshot?.event_key" class="mono">{{ run.snapshot.event_key }}</dd>
          <dt v-if="run.snapshot">Snapshot</dt><dd v-if="run.snapshot">rev {{ run.snapshot.config_revision }} · v{{ run.snapshot.version }}<template v-if="run.snapshot.table_bindings"> · {{ Object.entries(run.snapshot.table_bindings).map(([s, t]) => `${s} → ${t}`).join(', ') }}</template><template v-if="run.snapshot.mode"> · {{ run.snapshot.mode }}</template></dd>
          <dt>Idempotency</dt><dd>per call fingerprint: a resumed row replays, never pays twice</dd>
          <dt>Holds</dt><dd>one per call, settled once</dd>
        </dl>
        <div v-if="run.log.length"><h4>Log</h4><pre>{{ run.log.join('\n') }}</pre></div>
        <template #foot><AgentLine :cli="`treg run status ${run.id}`" :mcp="`run_status { run_id: &quot;${run.id}&quot; }`" /></template>
      </Inspector>
    </div>
  </template>
</div>
</template>
