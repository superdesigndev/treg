<script>
import { useDashboard } from '../state/context'
import PageTabs from '../components/PageTabs.vue'
import { DataTable } from '../components/ui/table'

// The feed reads left to right as one sentence: when, who (the key and runtime under it), which call
// (the action and tags under it), how it went, what it cost.
const FEED = [
  { key: 'when', header: 'When' },
  { key: 'caller', header: 'Caller', width: '22%', wrap: true },
  { key: 'call', header: 'Call', wrap: true, mobile: 'primary' },
  { key: 'status', header: 'Status' },
  { key: 'cost', header: 'Cost', align: 'right' },
]
const num = (key, header) => ({ key, header, align: 'right' })
const BY_MEMBER = [{ key: 'member', header: 'Member', mobile: 'primary' }, num('call', 'API'), num('local_run', 'Local'), num('server_run', 'Server'), num('total', 'Total')]
const BY_TOOL = [{ key: 'name', header: 'Tool', mobile: 'primary' }, num('total', 'Events')]
const BY_DAY = [{ key: 'day', header: 'Day', mobile: 'primary' }, num('total', 'Events')]
const BY_TAG = [{ key: 'value', header: 'Value', mobile: 'primary' }, num('calls', 'Calls'), num('spend', 'Spend')]

export default {
  components: { PageTabs, DataTable },
  setup: useDashboard,
  data: () => ({ feedColumns: FEED, memberColumns: BY_MEMBER, toolColumns: BY_TOOL, dayColumns: BY_DAY, tagColumns: BY_TAG }),
  computed: {
    actTabs() { return [{ key: 'feed', label: 'Calls' }, ...(this.canAdmin ? [{ key: 'usage', label: 'Usage' }] : [])] },
  },
  methods: {
    pickTab(key) { this.actTab = key; if (key === 'usage') this.loadUsage() },
    // A run has no request to show, so only a call's row opens anything.
    feedRowClass(a) { return { 'act-static': a.kind !== 'call' } },
    // A tag's rows, with the untagged remainder as one more row: shown, never dropped.
    tagRows(key) {
      const u = this.tagUsage[key]
      return [...u.rows, ...(u.unattributed_micro ? [{ value: null, calls: null, charged_micro: u.unattributed_micro }] : [])]
    },
  },
}
</script>

<template>
<div class="pl act">
  <header class="pl-hero">
    <h1>Activity</h1>
    <p class="pl-lede">Every call and run in {{activeName}}. Open a call to see its request and response.</p>
  </header>
  <!-- One page, two readings of the same traffic: the RAW FEED (every call, newest first)
       and the ROLLUPS (who/what/when, and spend per caller tag). They were separate sidebar
       entries, which made you leave one to answer a question about the other. -->
  <PageTabs label="Activity" :tabs="actTabs" :current="actTab" @select="pickTab" />

  <template v-if="actTab==='feed'">
  <div class="cn-filters">
    <select class="pl-select act-key" v-model="activityKey" @change="loadCalls" aria-label="API key">
      <option value="">All API keys</option><option v-for="k in apiKeys" :key="k.id" :value="String(k.id)">{{k.identity}} - {{k.name}}</option>
    </select>
    <div class="seg pl-seg" role="radiogroup" aria-label="Which calls">
      <button role="radio" :aria-checked="actOkOnly" :class="{on:actOkOnly}" @click="actOkOnly=true" title="Hide failed and refused calls">Succeeded <span>{{activityOkRows.length}}</span></button>
      <button role="radio" :aria-checked="!actOkOnly" :class="{on:!actOkOnly}" @click="actOkOnly=false" title="Include failed and refused calls">All <span>{{activityRows.length}}</span></button>
    </div>
    <span v-if="activityCachedCount" class="pl-meta pl-push">{{activityCachedCount}} of {{activityCallCount}} loaded {{activityCallCount===1?'call was':'calls were'}} served from the archive</span>
  </div>

  <DataTable v-if="activityShown.length" class="act-feed" :columns="feedColumns" :rows="activityShown" :row-key="a => a.kind+'-'+a.id"
             :row-class="feedRowClass" interactive surface @row-click="a => a.kind==='call' && openCall(a)">
    <template #cell-when="{ row: a }"><span class="muted" :title="a.created_at">{{when(a.created_at)}}</span></template>
    <template #cell-caller="{ row: a }"><span class="act-who">{{activityWho(a)}}<span v-if="activityOwner(a)" class="act-dim" :title="'Agent owner: '+activityAgentKey(a).created_by"> · owner {{activityOwner(a)}}</span></span>
        <span class="pl-sub"><span v-if="a.api_key_name" class="mono">{{a.api_key_name}}<template v-if="a.api_key_prefix"> · {{a.api_key_prefix}}</template></span><span v-if="a.client && a.client!=='cli'" title="reported by the runtime: attribution, not authentication"><template v-if="a.api_key_name"> · </template>via {{a.client}}</span></span></template>
    <template #cell-call="{ row: a }"><b class="act-tool">{{a.tool}}</b><span v-if="a.kind==='run'" class="act-kind" :title="a.where==='local'?'ran on this member\'s machine':'ran on the registry server'">{{a.where||'run'}} run</span>
        <span class="pl-sub mono">{{a.action}}</span>
        <span v-if="a.tags || (a.task && (a.task.result_url||a.task.fetch_command))" class="act-extra">
          <span v-for="(v,k) in a.tags" :key="k" class="chip act-tag" :title="'X-Treg-Meta '+k+'='+v">{{k}}={{v}}</span>
          <!-- A generation task's artifact, once it succeeded: the provider's time-limited URL, or the CLI command that retrieves it (treg never downloads media). -->
          <template v-if="a.task"><a v-if="a.task.result_url" :href="a.task.result_url" target="_blank" rel="noopener" class="act-link" :title="taskArtifactTitle(a.task)" @click.stop>result ↗</a><span v-else-if="a.task.fetch_command" class="chip act-tag" :title="'retrieve it from the CLI: '+a.task.fetch_command">result via CLI</span><span v-if="a.task.result_url||a.task.fetch_command" class="act-dim"> {{a.task.ttl_note?'expires in '+a.task.ttl_note:'time-limited link'}}</span></template>
        </span></template>
    <template #cell-status="{ row: a }"><span class="act-st" :class="a.ok?'ok':'bad'">{{a.status}}</span><span v-if="a.task" class="act-st pending" :title="taskStateTitle(a.task)">{{taskStateLabel(a.task)}}</span><span v-if="a.has_result" class="act-view">Result ›</span></template>
    <template #cell-cost="{ row: a }"><span class="act-cost" :title="a.held?'reserved - settles when the task finishes, refunded if it fails':(a.tier==='platform'?'charged to team balance':(a.cost!=null?'estimated - billed to your own provider key':''))">
        <span v-if="a.cached" class="act-cached" title="Served from treg's archive instead of calling the provider.">Cached</span><span v-if="a.held" class="act-dim">hold </span>{{a.cost!=null?money(a.cost):'-'}}</span></template>
  </DataTable>
  <p v-if="callsLoaded && !activityRows.length" class="pl-empty">No activity yet. Calls your agents make show up here.</p>
  <p v-else-if="callsLoaded && !activityShown.length" class="pl-empty">No successful calls yet. <button class="pl-link" @click="actOkOnly=false">Show all {{activityRows.length}}</button></p>
  </template>

  <template v-if="canAdmin && actTab==='usage'">
  <div class="cn-filters">
    <div class="seg pl-seg" role="radiogroup" aria-label="Window">
      <button v-for="d in [7,30,90]" :key="d" role="radio" :aria-checked="usageDays===d" :class="{on:usageDays===d}" @click="usageDays=d; loadUsage()">{{d}} days</button>
    </div>
    <span class="pl-meta pl-push">Counts only. No request or response content is stored.</span>
  </div>

  <div v-if="usage">
    <div class="act-stats">
      <div v-for="[n, l] in [[usage.totals.total, 'Total events'], [usage.totals.call, 'API calls'], [usage.totals.local_run, 'Local runs'], [usage.totals.server_run, 'Server runs']]"
           :key="l" class="pl-card act-stat"><b>{{n}}</b><span>{{l}}</span></div>
    </div>

    <section class="pl-sec">
      <h2 class="pl-h"><span>By member</span><i></i><em>{{usage.by_user.length}}</em></h2>
      <DataTable :columns="memberColumns" :rows="usage.by_user" :row-key="u => u.user_email" surface empty="No usage in this window.">
        <template #cell-member="{ row: u }">{{short(u.user_email)}}</template>
        <template #cell-total="{ row: u }"><b>{{u.total}}</b></template>
      </DataTable>
    </section>

    <div class="act-pair">
      <section v-if="usage.by_tool.length" class="pl-sec">
        <h2 class="pl-h"><span>Top tools</span><i></i></h2>
        <DataTable :columns="toolColumns" :rows="usage.by_tool" :row-key="t => t.name" surface />
      </section>
      <section v-if="usage.by_day.length" class="pl-sec">
        <h2 class="pl-h"><span>Per day</span><i></i></h2>
        <DataTable :columns="dayColumns" :rows="usage.by_day" :row-key="d => d.day" surface>
          <template #cell-day="{ row: d }"><span class="mono muted">{{d.day}}</span></template>
        </DataTable>
      </section>
    </div>

    <!-- SPEND BY CALLER TAG - one section per key the team has ACTUALLY SENT, not a picker.
         A dropdown hides the other dimensions behind an interaction, and the whole point of
         tagging by both customer and workspace is seeing them side by side. The list comes from
         /tag-keys (observed), so a team that never tags sees nothing here at all.

         Money here comes from the LEDGER, unlike the per-member/tool/day counts above, which
         come from the audit table and are allowed to lose rows. This is what a reselling team
         invoices from, so it has to be complete. -->
    <template v-for="key in tagKeys" :key="key">
      <section v-if="tagUsage[key]" class="pl-sec">
        <h2 class="pl-h"><span>Spend by {{key}}</span><i></i><em title="a caller tag from X-Treg-Meta">X-Treg-Meta</em></h2>
        <p class="cat-hint">What each <b>{{key}}</b> spent, from the ledger. A call is credited in full to every
          tag it carries, so these sections are different slices of the same money: never add two of them together.</p>
        <DataTable :columns="tagColumns" :rows="tagRows(key)" :row-key="r => r.value ?? '(untagged)'" surface :empty="'Nothing tagged '+key+' in this window.'">
          <template #head-value>{{key}}</template>
          <template #cell-value="{ row: r }"><span v-if="r.value" class="chip">{{r.value}}</span><span v-else class="muted" title="calls carrying no value for this tag - shown, never dropped">untagged</span></template>
          <template #cell-calls="{ row: r }"><span class="muted">{{r.calls ?? '-'}}</span></template>
          <template #cell-spend="{ row: r }"><b v-if="r.value">{{money(r.charged_micro)}}</b><span v-else class="muted">{{money(r.charged_micro)}}</span></template>
        </DataTable>
      </section>
    </template>
  </div>
  <p v-else class="pl-empty">Loading…</p>
  </template>
</div>
</template>
