<script setup lang="ts">
import { DataTable, type Column } from '../../components/ui/table'
import StatusPill from '../../components/StatusPill.vue'
import type { Trigger, UnattendedBudget } from '../api/client'
import AgentLine from '../AgentLine.vue'
import { ago, usd, when } from '../format'

type Row = Trigger & { install: string }
const props = defineProps<{ triggers: Row[] | null, unattended: UnattendedBudget | null }>()
const emit = defineEmits<{ toast: [string] }>()

const columns: Column<Row>[] = [
  { key: 'install', header: 'Install', mobile: 'primary', value: r => r.install },
  { key: 'key', header: 'Trigger', mobile: 'field' },
  { key: 'when', header: 'When', mobile: 'wide', width: '210px', truncate: true, value: r => `${r.kind === 'schedule' ? r.cron : r.webhook_url} ${r.timezone || ''}` },
  { key: 'mode', header: 'Mode', mobile: 'field', value: r => r.mode || '' },
  { key: 'last', header: 'Last run', mobile: 'field' },
  { key: 'next', header: 'Next', mobile: 'wide', width: '170px', truncate: true, value: r => r.status === 'paused' ? `paused · ${r.pause_detail || ''}` : r.kind === 'webhook' ? 'on delivery' : when(r.next_fire_at) },
  { key: 'status', header: 'Status', mobile: 'field' },
  { key: 'actions', header: '', align: 'right', mobile: 'wide' },
]
const pct = () => props.unattended && props.unattended.cap_micro ? Math.min(100, Math.round(props.unattended.spent_micro / props.unattended.cap_micro * 100)) : 0
</script>

<template>
<section v-if="triggers === null" class="pl-empty" aria-busy="true">Loading…</section>
<section v-else-if="!triggers.length" class="pl-empty">
  <p>No triggers yet. A trigger starts an install on a schedule or from a signed webhook, with a budget for every run.</p>
  <AgentLine cli='treg install <name> --trigger weekly --cron "0 9 * * MON"' mcp="install_save { name, triggers: [...] }" />
</section>
<template v-else>
  <DataTable :columns="columns" :rows="triggers" :row-key="r => r.install + '/' + r.key" surface>
    <template #cell-install="{ row: r }"><a :href="`#tools/i/${r.install}?tab=triggers`"><b>{{ r.install }}</b></a></template>
    <template #cell-key="{ row: r }"><b>{{ r.key }}</b><span class="pl-sub">{{ r.kind }}</span></template>
    <template #cell-when="{ row: r }"><span class="mono">{{ (r.kind === 'schedule' ? r.cron : r.webhook_url) || '' }}</span><span v-if="r.timezone" class="muted small" style="margin-left:6px">{{ r.timezone }}</span></template>
    <template #cell-last="{ row: r }"><template v-if="r.last_run"><StatusPill :status="r.last_run.status || 'queued'" /> <span class="muted small">{{ ago(r.last_run.at) }}<template v-if="r.last_run.spent_micro != null"> · {{ usd(r.last_run.spent_micro) }}</template></span></template><span v-else class="muted">never</span></template>
    <template #cell-next="{ row: r }"><template v-if="r.status === 'paused'"><span class="wb-warn">paused</span><span v-if="r.pause_detail" class="muted"> · {{ r.pause_detail }}</span></template><template v-else-if="r.kind === 'webhook'"><span class="muted">on delivery</span></template><template v-else>{{ when(r.next_fire_at) }}</template></template>
    <template #cell-status="{ row: r }"><StatusPill :status="r.status" /></template>
    <template #cell-actions="{ row: r }">
      <button v-if="r.status === 'paused'" type="button" class="btn sm primary" @click="emit('toast', 'Resume arrives with the server')">Resume</button>
      <button v-else type="button" class="btn sm" @click="emit('toast', 'Pause arrives with the server')">Pause</button>
    </template>
  </DataTable>
  <div v-if="unattended" class="wb-budget">
    <div><h4 class="wb-h4">Unattended spend this month</h4><span class="wb-bar wide"><i :style="{ width: pct() + '%' }"></i></span><span class="num">{{ usd(unattended.spent_micro) }} of {{ usd(unattended.cap_micro) }}</span></div>
    <p class="wb-hint">Reaching the cap blocks every trigger in the team and notifies it. Set in Team settings.</p>
  </div>
  <AgentLine cli='treg install <name> --trigger weekly --cron "0 9 * * MON" --net-new' mcp="install_save { name, triggers: [{ key, cron, timezone, mode }] }" />
</template>
</template>
