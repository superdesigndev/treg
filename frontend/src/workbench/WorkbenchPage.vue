<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useDashboard } from '../state/context'
import PageTabs from '../components/PageTabs.vue'
import { workbenchClient, type TableSummary, type Run, type Trigger, type UnattendedBudget } from './api/client'
import { WORKBENCH_TABS, type WorkbenchTab } from './routes'
import TablesTab from './tables/TablesTab.vue'
import RunsTab from './runs/RunsTab.vue'
import TriggersTab from './triggers/TriggersTab.vue'
import TableSheet from './tables/TableSheet.vue'
import './workbench.css'

const dash = useDashboard()
const client = workbenchClient((path, opts) => dash.api(path, opts))

const tab = computed<WorkbenchTab>(() => dash.wb.tab)
const tables = ref<TableSummary[] | null>(null)
const runs = ref<Run[] | null>(null)
const triggers = ref<(Trigger & { install: string })[] | null>(null)
const unattended = ref<UnattendedBudget | null>(null)
const error = ref('')

async function load(which: WorkbenchTab) {
  error.value = ''
  try {
    if (which === 'tables' && tables.value === null) tables.value = (await client.listTables()).tables
    if (which === 'runs' && runs.value === null) runs.value = (await client.listRuns()).runs
    if (which === 'triggers' && triggers.value === null) {
      const r = await client.listTriggers()
      triggers.value = r.triggers
      unattended.value = r.unattended
    }
  } catch (e: any) {
    error.value = e?.message || 'Could not load the Workbench'
  }
}

onMounted(() => load(tab.value))
watch(tab, load)
watch(() => dash.activeSlugNow, () => { tables.value = null; runs.value = null; triggers.value = null; load(tab.value) })
</script>

<template>
<div class="pl wb">
  <header class="pl-hero pl-hero-row">
    <div>
      <h1>Workbench</h1>
      <p class="pl-lede">Your team's tables, the runs that fill them, and the triggers that start those runs.</p>
    </div>
  </header>
  <PageTabs label="Workbench" :tabs="WORKBENCH_TABS" :current="tab" @select="dash.goWorkbench({ tab: $event })" />
  <p v-if="error" class="banner cn-banner"><span>{{ error }}</span></p>
  <TableSheet v-else-if="tab === 'tables' && dash.wb.tableId" :table-id="dash.wb.tableId" />
  <TablesTab v-else-if="tab === 'tables'" :tables="tables" />
  <RunsTab v-else-if="tab === 'runs'" :runs="runs" />
  <TriggersTab v-else :triggers="triggers" :unattended="unattended" />
</div>
</template>
