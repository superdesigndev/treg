<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useDashboard } from '../../state/context'
import PageTabs from '../../components/PageTabs.vue'
import StatusPill from '../../components/StatusPill.vue'
import { DataTable, type Column } from '../../components/ui/table'
import { workbenchClient, type Install, type Run, type TableSummary, type ToolDefinition, type Schemas } from '../api/client'
import AgentLine from '../AgentLine.vue'
import RunsTab from '../runs/RunsTab.vue'
import { ago, usd, when } from '../format'
import '../workbench.css'

type Version = Schemas['ToolVersion']
const dash = useDashboard()
const client = workbenchClient((path, opts) => dash.api(path, opts))
const id = computed<string>(() => dash.wbTools.id)
const tab = computed<string>(() => dash.wbTools.tab)

const tool = ref<ToolDefinition | null>(null)
const installs = ref<Install[] | null>(null)
const runs = ref<Run[] | null>(null)
const tables = ref<TableSummary[]>([])
const tableName = (id: string) => tables.value.find(t => t.id === id)?.name || id
const error = ref('')

async function load() {
  error.value = ''
  try {
    const [t, i, r, tl] = await Promise.all([client.getTool(id.value), client.listInstalls(), client.listRuns(), client.listTables()])
    tool.value = t
    tables.value = tl.tables
    installs.value = i.installs.filter(x => x.tool === t.id)
    runs.value = r.runs.filter(x => x.tool?.startsWith(t.id + '@'))
  } catch (e: any) { error.value = e?.status === 404 ? 'No tool with that id.' : e?.message || 'Could not load the tool' }
}
onMounted(load)
watch(id, load)

const TABS = [['overview', 'Overview'], ['installs', 'Installs'], ['runs', 'Runs'], ['versions', 'Versions']].map(([key, label]) => ({ key, label }))
const latest = computed(() => tool.value?.versions.find(v => v.version === tool.value!.latest_version) || null)
const spent = computed(() => (runs.value || []).reduce((n, r) => n + r.spent_micro, 0))

const installColumns: Column<Install>[] = [
  { key: 'name', header: 'Install', mobile: 'primary' },
  { key: 'version', header: 'Version', mobile: 'field' },
  { key: 'bindings', header: 'Table bindings', mobile: 'wide', wrap: true },
  { key: 'budget', header: 'Budget per run', align: 'right', mobile: 'field', value: i => usd(i.max_cost_micro) },
  { key: 'triggers', header: 'Triggers', mobile: 'field' },
  { key: 'last', header: 'Last run', mobile: 'field' },
]
const versionColumns: Column<Version>[] = [
  { key: 'version', header: 'Version', mobile: 'primary', value: v => `v${v.version}` },
  { key: 'status', header: 'Status', mobile: 'field' },
  { key: 'published', header: 'Published', mobile: 'field', value: v => `${when(v.published_at)}${v.published_by ? ' · ' + v.published_by : ''}` },
  { key: 'check', header: 'Check', mobile: 'field', value: v => v.check || '' },
  { key: 'slots', header: 'Table slots', mobile: 'wide', wrap: true },
  { key: 'installs', header: 'Installs on it', align: 'right', mobile: 'field', value: v => v.install_count ?? 0 },
]
const slotText = (slots: Record<string, Schemas['TableSlot']> | undefined) =>
  Object.entries(slots || {}).map(([s, v]) => `${s}: ${[...(v.read || []).map(c => c), ...(v.write || []).map(c => c + '↑')].join(', ') || v.access}`).join(' · ')
</script>

<template>
<div class="pl wb">
  <p class="wb-crumb" style="margin:18px 0 0"><a href="#workbench/tables" @click.prevent="dash.go('workbench')">← Workbench</a></p>
  <p v-if="error" class="banner cn-banner"><span>{{ error }}</span></p>
  <template v-else-if="tool">
    <header class="pl-hero pl-hero-row" style="padding-top:8px">
      <div>
        <h1>{{ tool.name }} <span class="wb-h1-meta">{{ tool.kind }} · v{{ tool.latest_version }}<template v-if="tool.listing === 'approved'"> · listed</template></span></h1>
        <p class="pl-lede mono">{{ tool.id }}</p>
      </div>
      <div class="pl-hero-a wb-actions">
        <button v-if="installs && installs.length" type="button" class="btn primary" @click="dash.goTools({ kind: 'install', id: installs[0].name, tab: 'settings' })">Open install {{ installs[0].name }}</button>
        <button type="button" class="btn" disabled title="Publish arrives with hub review">Publish</button>
      </div>
    </header>
    <PageTabs :label="tool.name" :tabs="TABS" :current="tab" @select="dash.goTools({ tab: $event })" />

    <template v-if="tab === 'overview'">
      <div class="statgrid wb-stats">
        <div class="stat"><div class="n">{{ usd(spent) }}</div><div class="l">spent, recent runs</div></div>
        <div class="stat"><div class="n">{{ runs ? runs.length : '–' }}</div><div class="l">runs</div></div>
        <div class="stat"><div class="n">{{ installs ? installs.length : '–' }}</div><div class="l">installs in your team</div></div>
        <div class="stat"><div class="n" style="font-size:16px"><StatusPill :status="tool.health === 'ok' ? 'active' : tool.health || 'queued'" :label="tool.health || 'unknown'" /></div><div class="l">health</div></div>
      </div>
      <div class="wb-two">
        <section class="wb-panel">
          <h4 class="wb-h4">What it does</h4><p>{{ tool.summary }}</p>
          <template v-if="tool.uses?.length"><h4 class="wb-h4">Uses</h4><div class="wb-chips"><span v-for="u in tool.uses" :key="u" class="chip">{{ u }}</span></div></template>
          <template v-if="latest && Object.keys(latest.table_slots || {}).length"><h4 class="wb-h4">Table slots v{{ latest.version }} declares</h4>
            <dl class="kv"><template v-for="(slot, name) in latest.table_slots" :key="name"><dt><code>{{ name }}</code></dt><dd>{{ slot.access.replace('_', ' + ') }}<template v-if="slot.read?.length"> · reads {{ slot.read.join(', ') }}</template><template v-if="slot.write?.length"> · writes {{ slot.write.join(', ') }}</template></dd></template></dl>
            <p class="wb-hint">Slots name what the code needs. Each install binds them to that team's own tables.</p></template>
        </section>
        <section class="wb-panel">
          <h4 class="wb-h4">Call it</h4>
          <pre>{{ installs && installs.length ? `call { tool: "${installs[0].name}" }           // an install: config already set\n` : '' }}call { tool: "{{ tool.id }}@{{ tool.latest_version }}", input: {…} }   // the version itself
POST {{ dash.proxy }}/call/{{ tool.id }}   X-Treg-Token</pre>
          <p class="wb-hint">A call that outlives the wait budget hands back a run handle. Every row's call keeps its own hold and settles once.</p>
        </section>
      </div>
    </template>

    <template v-else-if="tab === 'installs'">
      <p class="cat-hint">Each install is one configuration of this tool in your team: its own tables, budget and triggers. Changing an install never changes the code or its version.</p>
      <DataTable v-if="installs" :columns="installColumns" :rows="installs" :row-key="i => i.name" interactive surface empty="No installs yet." @row-click="i => dash.goTools({ kind: 'install', id: i.name, tab: 'settings' })">
        <template #cell-name="{ row: i }"><b>{{ i.name }}</b><StatusPill v-if="i.paused" status="paused" /></template>
        <template #cell-version="{ row: i }"><span class="num">v{{ i.version }}</span><span v-if="i.version < i.latest_version" class="muted small"> · v{{ i.latest_version }} available</span></template>
        <template #cell-bindings="{ row: i }"><span v-for="(t, s) in i.table_bindings" :key="s" class="wb-binding"><code>{{ s }}</code> → <a :href="`#workbench/tables/${t}`" @click.stop>{{ tableName(t) }}</a></span></template>
        <template #cell-triggers="{ row: i }"><template v-if="i.triggers.length"><span v-for="t in i.triggers" :key="t.key">{{ t.key }} <StatusPill :status="t.status" /> </span></template><span v-else class="muted">–</span></template>
        <template #cell-last="{ row: i }"><template v-if="i.last_run"><StatusPill :status="i.last_run.status || 'queued'" /> <span class="muted small">{{ ago(i.last_run.at) }}</span></template><span v-else class="muted">never</span></template>
      </DataTable>
      <div class="wb-actions" style="margin-top:12px"><button type="button" class="btn primary" disabled title="Arrives with the server">New install</button></div>
      <AgentLine :cli="`treg install ${tool.id}@${tool.latest_version} --name my-${tool.name} --bind leads=<table>`" :mcp="`install_save { name, tool: &quot;${tool.id}@${tool.latest_version}&quot;, table_bindings, max_cost_micro }`" />
    </template>

    <RunsTab v-else-if="tab === 'runs'" :runs="runs" />

    <template v-else>
      <DataTable :columns="versionColumns" :rows="tool.versions" :row-key="v => String(v.version)" surface>
        <template #cell-status="{ row: v }"><StatusPill :status="v.status === 'live' ? 'active' : 'archived'" :label="v.status" /></template>
        <template #cell-slots="{ row: v }"><span class="mono small">{{ slotText(v.table_slots) || '–' }}</span></template>
      </DataTable>
      <p class="wb-hint" style="margin-top:10px">Installs stay pinned. Upgrading one revalidates its table bindings against the new version's slots. An old version stays callable while an install pins it.</p>
    </template>
  </template>
</div>
</template>
