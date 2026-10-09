<script>
import { useDashboard } from '../state/context'
import PageTabs from '../components/PageTabs.vue'
import { DataTable } from '../components/ui/table'
import SpendBars from '../components/SpendBars.vue'
import SelectMenu from '../components/ui/SelectMenu.vue'
import DateRangePicker from '../components/ui/DateRangePicker.vue'
import InfoTip from '../components/ui/InfoTip.vue'
import { NO_KEY } from '../state/constants.js'

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
const BY_SERIES = [{ key: 'name', header: 'Name', mobile: 'primary' }, num('calls', 'Calls'), { key: 'share', header: 'Share', width: '22%' }, num('spend', 'Spend')]
const GROUPS = [{ value: 'day', label: 'By day' }, { value: 'week', label: 'By week' }, { value: 'month', label: 'By month' }]
const STACKS = [{ value: 'key', label: 'API keys' }, { value: 'tool', label: 'Tools' }]
const PAGE = 10
const dayLabel = (iso, opts) => new Date(iso + 'T00:00:00Z').toLocaleDateString(undefined, { timeZone: 'UTC', ...opts })

const BY_TAG = [{ key: 'value', header: 'Value', mobile: 'primary' }, num('calls', 'Calls'), num('spend', 'Spend')]

export default {
  components: { PageTabs, DataTable, SpendBars, SelectMenu, DateRangePicker, InfoTip },
  setup: useDashboard,
  data: () => ({ feedColumns: FEED, memberColumns: BY_MEMBER, toolColumns: BY_TOOL, dayColumns: BY_DAY, tagColumns: BY_TAG, spendColumns: BY_SERIES, GROUPS, STACKS, PAGE, NO_KEY }),
  computed: {
    actTabs() { return [...(this.canAdmin ? [{ key: 'usage', label: 'Usage' }] : []), { key: 'feed', label: 'Calls' }] },
    daysNewestFirst() { return [...(this.usage?.by_day || [])].reverse() },
    // The filter lists keep the current choice even when the new range has no spend for it.
    spendKeyOptions() {
      return [{ value: '', label: 'All API keys' }, ...(this.usageSpend?.options.keys || []).map(k => ({ value: String(k.id), label: k.name }))]
    },
    spendProviderOptions() {
      return [{ value: '', label: 'All providers' }, ...(this.usageSpend?.options.providers || []).map(p => ({ value: p.id, label: p.name || p.id }))]
    },
    spendSeries() {
      const noun = this.usageSpend?.stack === 'tool' ? 'tools' : 'keys'
      return (this.usageSpend?.series || []).map(s => s.id === 'none' ? { ...s, info: NO_KEY }
        : s.id === '__other' ? { ...s, name: `Other (${s.members} ${noun})`, folded: this.foldedRanking } : s)
    },
    // What the Other slice holds over the whole range: every ranked series without its own color.
    foldedRanking() {
      const shown = new Set((this.usageSpend?.series || []).map(s => s.id))
      return (this.usageSpend?.ranking || []).filter(r => !shown.has(r.id)).map(r => ({ id: r.id, name: r.name, value: this.money(r.spend_micro) }))
    },
    rankNames() { return Object.fromEntries((this.usageSpend?.ranking || []).map(r => [r.id, r.name])) },
    activityKeyOptions() {
      return [{ value: '', label: 'All API keys' }, ...this.apiKeys.map(k => ({ value: String(k.id), label: this.keyLabel(k) }))]
    },
    spendBuckets() {
      const s = this.usageSpend
      if (!s) return []
      const month = s.group === 'month', week = s.group === 'week'
      return s.buckets.map(b => ({
        id: b.start, parts: b.parts, calls: b.calls,
        others: Object.entries(b.others || {}).sort((x, y) => y[1] - x[1]).map(([id, value]) => ({ name: this.rankNames[id] || id, value })),
        label: month ? dayLabel(b.start, { month: 'long', year: 'numeric' }) : (week ? 'Week of ' : '') + dayLabel(b.start, { month: 'short', day: 'numeric', year: 'numeric' }),
        tick: month ? dayLabel(b.start, { month: 'short' }) : dayLabel(b.start, { month: 'short', day: 'numeric' }),
      }))
    },
  },
  methods: {
    pickTab(key) { this.actTab = key; if (key === 'usage') this.loadUsage() },
    page(rows, n) { return rows.slice(n * PAGE, (n + 1) * PAGE) },
    share(r) { const t = this.usageSpend?.spend_micro || 0; return t ? Math.round((r.spend_micro / t) * 1000) / 10 : 0 },
    // A ranked row wears its chart color when it has its own slice, and the Other gray when folded.
    rankColor(r) {
      const s = this.spendSeries.find(x => x.id === r.id)
      return s ? `var(--sb-${(s.slot ?? 0) + 1})` : 'var(--sb-other)'
    },
    pickRankRow(r) {
      if (this.usageSpend?.stack !== 'key' || r.id === 'none') return
      this.spendFilter.key = r.id
      this.loadUsageSpend()
    },
    // The CSV is what the card shows: the current range and filters, one row per period and series,
    // nothing folded into Other. Built here from the answer already on screen; no second request.
    exportSpendCsv() {
      const s = this.usageSpend
      if (!s) return
      const names = this.rankNames, kind = s.stack === 'tool' ? 'tool' : 'api_key'
      const rows = [['period_start', 'group', kind + '_id', kind, 'spend_usd', 'spend_micro']]
      for (const b of s.buckets) {
        const parts = { ...b.parts, ...(b.others || {}) }
        delete parts.__other
        for (const [id, micro] of Object.entries(parts).sort((x, y) => y[1] - x[1])) {
          rows.push([b.start, s.group, id === 'none' ? '' : id, names[id] || id, (micro / 1e6).toFixed(6), micro])
        }
      }
      const cell = v => /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v)
      const blob = new Blob([rows.map(r => r.map(cell).join(',')).join('\n') + '\n'], { type: 'text/csv' })
      const a = Object.assign(document.createElement('a'), {
        href: URL.createObjectURL(blob),
        download: `treg-spend-${this.activeSlugNow || 'team'}-${s.from}-to-${s.to}-by-${s.group}${this.spendFilter.provider ? '-' + this.spendFilter.provider : ''}.csv`,
      })
      a.click()
      setTimeout(() => URL.revokeObjectURL(a.href), 0)
    },
    pageLabel(rows, n) { return `${n * PAGE + 1}–${Math.min((n + 1) * PAGE, rows.length)} of ${rows.length}` },
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
    <p v-if="actTab==='usage'" class="pl-lede">What {{activeName}} spent and how much it used, by day, API key and provider.</p>
    <p v-else class="pl-lede">Every call and run in {{activeName}}. Open a call to see its request and response.</p>
  </header>
  <!-- One page, two readings of the same traffic: the RAW FEED (every call, newest first)
       and the ROLLUPS (who/what/when, and spend per caller tag). They were separate sidebar
       entries, which made you leave one to answer a question about the other. -->
  <PageTabs label="Activity" :tabs="actTabs" :current="actTab" @select="pickTab" />

  <template v-if="actTab==='feed'">
  <div class="cn-filters">
    <SelectMenu v-model="activityKey" class="act-key" size="lg" icon="key" label="API key" :options="activityKeyOptions" @change="loadCalls" />
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
  <p v-else-if="callsLoaded && !activityShown.length" class="pl-empty">{{activityHasOlder?"None of the loaded calls succeeded.":"No successful calls yet."}} <button class="pl-link" @click="actOkOnly=false">Show all {{activityRows.length}}</button></p>
  <button v-if="callsLoaded && activityHasOlder" class="pl-link pl-more-rows" :disabled="activityOlderBusy" @click="loadOlderActivity">{{activityOlderBusy?'Loading…':'Load older activity'}}</button>
  </template>

  <template v-if="canAdmin && actTab==='usage'">
  <div class="cn-filters us-range">
    <DateRangePicker :model-value="usageRange" @change="setUsageRange" />
    <span class="pl-meta pl-push">Days are UTC. No request or response content is stored.</span>
    <button type="button" class="btn us-export" :disabled="!usageSpend || !usageSpend.ranking.length" @click="exportSpendCsv">
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M5 7.5l3 3 3-3M3 12.5v1h10v-1" /></svg>Export CSV</button>
  </div>

  <!-- SPEND: money from the LEDGER, never from audit costs. Its own request: the counts below may
       be slow on a busy team, and the chart must not wait for them. -->
  <section class="us-card" :aria-busy="usageSpendBusy">
    <header class="us-card-head">
      <div>
        <h2>Spend</h2>
        <p class="muted">Billed charges on treg's keys, from the ledger.</p>
      </div>
      <div v-if="usageSpend" class="us-totals">
        <div><b>{{money(usageSpend.spend_micro)}}</b><span>spent</span></div>
        <div><b>{{usageSpend.calls}}</b><span>billed calls</span></div>
      </div>
    </header>
    <div class="us-filters">
      <div class="us-filter"><span>Group by</span>
        <SelectMenu v-model="spendFilter.group" icon="clock" label="Group by" :options="GROUPS" @change="loadUsageSpend" /></div>
      <div class="us-filter"><span>API key</span>
        <SelectMenu v-model="spendFilter.key" icon="key" label="API key" :options="spendKeyOptions" @change="loadUsageSpend" /></div>
      <div class="us-filter"><span>Provider</span>
        <SelectMenu v-model="spendFilter.provider" icon="plug" label="Provider" :options="spendProviderOptions" @change="loadUsageSpend" /></div>
      <!-- Stacking by tool only reads within one provider: across all of them it is hundreds of series. -->
      <div v-if="spendFilter.provider" class="us-filter"><span>Stack by</span>
        <SelectMenu v-model="spendFilter.stack" icon="layers" label="Stack by" :options="STACKS" @change="loadUsageSpend" /></div>
      <button v-if="spendFilter.key || spendFilter.provider || spendFilter.group!=='day'" class="pl-link us-clear"
              @click="Object.assign(spendFilter,{group:'day',key:'',provider:'',stack:'key'}); loadUsageSpend()">Clear filters</button>
    </div>
    <div class="us-card-body" :class="{ busy: usageSpendBusy && usageSpend }">
      <p v-if="usageSpendErr" class="pl-empty">{{usageSpendErr}}</p>
      <p v-else-if="!usageSpend" class="pl-empty">Loading spend…</p>
      <p v-else-if="!usageSpend.spend_micro" class="pl-empty">No billed calls in this range.</p>
      <template v-else>
        <SpendBars :buckets="spendBuckets" :series="spendSeries"
                   :aria-label="`Billed spend by ${usageSpend.group}: ${money(usageSpend.spend_micro)} from ${usageSpend.from} to ${usageSpend.to}`" />
        <!-- Every series, none folded: the chart can color seven, a list can name them all. A key's
             row filters the chart to it; the chart answers "when", this list "who". -->
        <button type="button" class="us-rank-toggle" :aria-expanded="spendRankOpen" aria-controls="us-ranking" @click="spendRankOpen=!spendRankOpen">
          {{usageSpend.stack==='tool' ? 'Spend per tool' : 'Spend per API key'}}
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" /></svg>
        </button>
        <div id="us-ranking" class="us-ranking" :class="{ open: spendRankOpen }" :inert="!spendRankOpen"><div class="us-ranking-inner">
          <DataTable :columns="spendColumns" :rows="page(usageSpend.ranking, spendRankPage)" :row-key="r => r.id" surface
                     :interactive="usageSpend.stack==='key'" @row-click="pickRankRow">
            <template #head-name>{{usageSpend.stack==='tool' ? 'Tool' : 'API key'}}</template>
            <template #cell-name="{ row: r }"><span class="us-rank-name"><s :style="{ background: rankColor(r) }"></s><span :title="r.name">{{r.name}}</span><InfoTip v-if="r.id==='none'" :title="NO_KEY.title" :text="NO_KEY.text" /></span></template>
            <template #cell-share="{ row: r }"><span class="us-share"><i :style="{ width: share(r) + '%' }"></i></span><span class="muted">{{share(r)}}%</span></template>
            <template #cell-spend="{ row: r }"><b>{{money(r.spend_micro)}}</b></template>
          </DataTable>
          <div v-if="usageSpend.ranking.length > PAGE" class="us-pager">
            <span class="muted">{{pageLabel(usageSpend.ranking, spendRankPage)}}</span>
            <button class="btn sm" :disabled="!spendRankPage" @click="spendRankPage--">Previous</button>
            <button class="btn sm" :disabled="(spendRankPage+1)*PAGE >= usageSpend.ranking.length" @click="spendRankPage++">Next</button>
          </div>
        </div></div>
      </template>
    </div>
  </section>

  <p v-if="usageErr" class="pl-empty">{{usageErr}}</p>
  <p v-else-if="!usage" class="pl-empty">Loading counts…</p>
  <template v-else>
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
        <h2 class="pl-h"><span>Top tools</span><i></i><em>{{usage.by_tool.length}}</em></h2>
        <DataTable :columns="toolColumns" :rows="page(usage.by_tool, usageToolPage)" :row-key="t => t.name" surface />
        <div v-if="usage.by_tool.length > PAGE" class="us-pager">
          <span class="muted">{{pageLabel(usage.by_tool, usageToolPage)}}</span>
          <button class="btn sm" :disabled="!usageToolPage" @click="usageToolPage--">Previous</button>
          <button class="btn sm" :disabled="(usageToolPage+1)*PAGE >= usage.by_tool.length" @click="usageToolPage++">Next</button>
        </div>
      </section>
      <section v-if="usage.by_day.length" class="pl-sec">
        <h2 class="pl-h"><span>Per day</span><i></i><em>{{usage.by_day.length}}</em></h2>
        <DataTable :columns="dayColumns" :rows="page(daysNewestFirst, usageDayPage)" :row-key="d => d.day" surface>
          <template #cell-day="{ row: d }"><span class="mono muted">{{d.day}}</span></template>
        </DataTable>
        <div v-if="usage.by_day.length > PAGE" class="us-pager">
          <span class="muted">{{pageLabel(usage.by_day, usageDayPage)}}</span>
          <button class="btn sm" :disabled="!usageDayPage" @click="usageDayPage--">Newer</button>
          <button class="btn sm" :disabled="(usageDayPage+1)*PAGE >= usage.by_day.length" @click="usageDayPage++">Older</button>
        </div>
      </section>
    </div>
  </template>

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
  </template>
</div>
</template>
