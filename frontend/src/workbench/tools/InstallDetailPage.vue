<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useDashboard } from '../../state/context'
import PageTabs from '../../components/PageTabs.vue'
import Inspector from '../../components/Inspector.vue'
import StatusPill from '../../components/StatusPill.vue'
import { workbenchClient, type InstallDetail, type Run, type TableSummary } from '../api/client'
import AgentLine from '../AgentLine.vue'
import RunsTab from '../runs/RunsTab.vue'
import TriggersTab from '../triggers/TriggersTab.vue'
import { usd } from '../format'
import '../workbench.css'

const dash = useDashboard()
const client = workbenchClient((path, opts) => dash.api(path, opts))
const name = computed<string>(() => dash.wbTools.id)
const tab = computed<string>(() => dash.wbTools.tab)

const install = ref<InstallDetail | null>(null)
const tables = ref<TableSummary[]>([])
const runs = ref<Run[] | null>(null)
const error = ref('')
const note = ref('')
const busy = ref(false)

const draft = ref({ inputs: {} as Record<string, string>, bindings: {} as Record<string, string>, maxUsd: '', version: 0 })
function reset() {
  if (!install.value) return
  draft.value = {
    inputs: Object.fromEntries(Object.entries(install.value.default_inputs).map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(v)])),
    bindings: { ...install.value.table_bindings },
    maxUsd: (install.value.max_cost_micro / 1e6).toFixed(2),
    version: install.value.version,
  }
}
async function load() {
  error.value = ''
  try {
    const [i, t, r] = await Promise.all([client.getInstall(name.value), client.listTables(), client.listRuns({ install: name.value })])
    install.value = i; tables.value = t.tables.filter(x => x.kind !== 'automatic'); runs.value = r.runs.filter(x => x.install === i.name)
    reset()
  } catch (e: any) { error.value = e?.status === 404 ? 'No install with that name in this team.' : e?.message || 'Could not load the install' }
}
onMounted(load)
watch(name, load)

const TABS = [['settings', 'Settings'], ['triggers', 'Triggers'], ['runs', 'Runs']].map(([key, label]) => ({ key, label }))
const credLabel = (s: string) => ({ set: 'set', missing: 'missing', treg_key: "treg's key · your balance", maker_key: "maker's key · maker's price", expired: 'expired' })[s] || s
const payload = computed(() => install.value ? {
  tool: `${install.value.tool}@${draft.value.version}`,
  default_inputs: draft.value.inputs,
  table_bindings: draft.value.bindings,
  max_cost_micro: Math.round(Number(draft.value.maxUsd || 0) * 1e6),
  config_revision: install.value.config_revision,
} : null)
async function save() {
  note.value = `Saved as revision ${(install.value?.config_revision || 0) + 1}. Runs already in progress keep their snapshot.`
}
const toast = (m: string) => { note.value = m }
</script>

<template>
<div class="pl wb">
  <p class="wb-crumb" style="margin:18px 0 0"><a href="#tools" @click.prevent="dash.go('tools')">← Your own tools</a><template v-if="install"> · <a :href="`#tools/t/${install.tool}?tab=installs`" @click.prevent="dash.goTools({ kind: 'tool', id: install.tool, tab: 'installs' })">{{ install.tool }}</a></template></p>
  <p v-if="error" class="banner cn-banner"><span>{{ error }}</span></p>
  <template v-else-if="install">
    <header class="pl-hero pl-hero-row" style="padding-top:8px">
      <div>
        <h1>{{ install.name }} <span class="wb-h1-meta">install · v{{ install.version }}<template v-if="install.origin === 'builtin'"> · made by Add column</template><template v-else-if="install.origin === 'hub'"> · from the hub</template></span><StatusPill v-if="install.paused" status="paused" /></h1>
        <p class="pl-lede mono">{{ install.tool }}</p>
      </div>
      <div class="pl-hero-a wb-actions">
        <button type="button" class="btn primary" @click="toast('dry_run first: the bound table has rows to do and an estimate; confirm to start')">Run now</button>
        <button type="button" class="btn" @click="toast(install.paused ? 'Resumed' : 'Paused: no trigger starts a run; running ones finish')">{{ install.paused ? 'Resume' : 'Pause' }}</button>
      </div>
    </header>
    <PageTabs :label="install.name" :tabs="TABS" :current="tab" @select="dash.goTools({ tab: $event })" />
    <p v-if="note" class="banner cn-banner"><span>{{ note }}</span><button class="btn sm ico" aria-label="Dismiss" @click="note = ''">✕</button></p>

    <div v-if="tab === 'settings'" class="wb-layout">
      <form class="wb-panel wb-form" @submit.prevent="save">
        <section>
          <h4 class="wb-h4">Default inputs</h4>
          <p class="wb-hint">Merged under every run. A trigger may override a field; a webhook event may supply the ones the tool declares.</p>
          <div v-for="(v, k) in draft.inputs" :key="k" class="wb-frow"><label :for="'in-' + k">{{ k }}</label><input :id="'in-' + k" v-model="draft.inputs[k]"></div>
          <p v-if="!Object.keys(draft.inputs).length" class="muted small">This version takes no input; it reads its bound table.</p>
        </section>
        <section>
          <h4 class="wb-h4">Table bindings</h4>
          <p class="wb-hint">The slots v{{ install.version }} declares, and which of your tables fill them. Bound by table id, so a rename never breaks it. The maker never sees these rows.</p>
          <div v-for="(slot, s) in install.table_slots" :key="s" class="wb-frow">
            <label :for="'slot-' + s"><code>{{ s }}</code> <em class="muted">{{ slot.access.replace('_', ' + ') }}</em></label>
            <select :id="'slot-' + s" v-model="draft.bindings[s]"><option v-for="t in tables" :key="t.id" :value="t.id">{{ t.name }}</option></select>
          </div>
        </section>
        <section>
          <h4 class="wb-h4">Budget</h4>
          <div class="wb-frow"><label for="maxusd">per run, USD</label><input id="maxusd" v-model="draft.maxUsd" inputmode="decimal" style="max-width:140px"></div>
          <p class="wb-hint">Checked before every call inside a run, counting in-flight holds. A run that reaches it stops and pauses the trigger that started it.</p>
        </section>
        <section>
          <h4 class="wb-h4">Version</h4>
          <div class="wb-frow"><label for="ver">pinned</label><select id="ver" v-model.number="draft.version"><option :value="install.version">v{{ install.version }} (current)</option><option v-if="install.latest_version > install.version" :value="install.latest_version">Upgrade to v{{ install.latest_version }} (revalidates bindings)</option></select></div>
        </section>
        <div class="wb-actions"><button type="submit" class="btn primary">Save settings</button><button type="button" class="btn ghost" @click="reset">Reset</button><button type="button" class="btn ghost danger" @click="toast('Remove asks inline; the tables stay, they are the team\'s')">Remove install</button></div>
      </form>
      <Inspector title="Same thing from your agent">
        <pre>install_save {{ JSON.stringify({ name: install.name, ...payload }, null, 2) }}</pre>
        <p class="wb-hint">Settings carry a revision, not a version. A stale revision is refused, so two agents cannot overwrite each other.</p>
        <div><h4>Credentials this run will use</h4>
          <dl class="kv"><template v-for="c in install.credentials" :key="c.name"><dt>{{ c.name }}</dt><dd><StatusPill :status="c.status === 'missing' || c.status === 'expired' ? 'failed' : 'active'" :label="credLabel(c.status)" /><span v-if="c.set_with" class="mono small"> {{ c.set_with }}</span></dd></template></dl>
          <p class="wb-hint">Catalog calls charge this team. A maker's own tool runs on the maker's credentials. Table reads and writes touch only your tables.</p></div>
      </Inspector>
    </div>

    <template v-else-if="tab === 'triggers'">
      <TriggersTab :triggers="install.triggers.map(t => ({ ...t, install: install!.name }))" :unattended="null" embedded @toast="toast" />
      <AgentLine :cli="`treg install ${install.name} --trigger weekly --cron &quot;0 9 * * MON&quot; --tz Europe/Amsterdam --net-new`" :mcp="`install_save { name: &quot;${install.name}&quot;, triggers: [{ key: &quot;weekly&quot;, cron, timezone, mode: &quot;net_new&quot; }] }`" />
    </template>

    <RunsTab v-else :runs="runs" />
  </template>
</div>
</template>
