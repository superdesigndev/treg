<script>
import billing from '../state/billing.js'
import SpendBars from './SpendBars.vue'
import SelectMenu from './ui/SelectMenu.vue'

// One key's billed spend per UTC day: a single series, so no legend - the title names it. Days
// the server left out are drawn empty, so the bars always span the whole window.
const WINDOWS = [[7, 'Last 7 days'], [30, 'Last 30 days'], [90, 'Last 3 months']]
const SERIES = [{ id: 'spend', name: 'Spent', color: 'var(--accent)' }]

const label = day => new Date(day + 'T00:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })

export default {
  components: { SpendBars, SelectMenu },
  props: { spend: { type: Object, required: true }, keyName: { type: String, default: '' }, busy: { type: Boolean, default: false } },
  emits: ['days'],
  data: () => ({ WINDOWS, SERIES }),
  computed: {
    days() {
      const got = Object.fromEntries((this.spend.by_day || []).map(d => [d.day, d]))
      const start = Date.parse(this.spend.since.slice(0, 10) + 'T00:00:00Z')
      return Array.from({ length: this.spend.days }, (_, i) => {
        const day = new Date(start + i * 86_400_000).toISOString().slice(0, 10)
        return got[day] || { day, spend_micro: 0, calls: 0 }
      })
    },
    buckets() { return this.days.map(d => ({ id: d.day, label: label(d.day), tick: label(d.day), parts: { spend: d.spend_micro }, calls: d.calls })) },
    total() { return this.days.reduce((a, d) => a + d.spend_micro, 0) },
    calls() { return this.days.reduce((a, d) => a + d.calls, 0) },
    range() { return (WINDOWS.find(w => w[0] === this.spend.days) || [0, 'The last ' + this.spend.days + ' days'])[1].toLowerCase() },
  },
  methods: { money: billing.money },
}
</script>

<template>
  <div class="ks-card" :aria-busy="busy">
    <header class="ks-head">
      <div>
        <h3>Spend per day</h3>
        <p class="muted">{{keyName}} · <b>{{money(total)}}</b> over {{range}} · {{calls}} billed {{calls === 1 ? 'call' : 'calls'}}</p>
      </div>
      <SelectMenu class="ks-range" :model-value="spend.days" label="Date range" :disabled="busy"
                  :options="WINDOWS.map(([value, label]) => ({ value, label }))" @change="d => $emit('days', d)" />
    </header>
    <div class="ks-body" :class="{ busy }">
      <p v-if="!total" class="muted ks-empty">No billed calls on this key in this range.</p>
      <template v-else>
        <SpendBars :buckets="buckets" :series="SERIES"
                   :aria-label="`Billed spend per day for ${keyName}: ${money(total)} over ${spend.days} days`" />
      </template>
    </div>
  </div>
</template>

<style scoped>
.ks-card{border:1px solid var(--line);border-radius:var(--r);background:var(--panel)}
.ks-head{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line)}
.ks-head h3{margin:0;font-size:15px}
.ks-head p{margin:4px 0 0;font-size:12.5px}
.ks-head p b{color:var(--ink);font-variant-numeric:tabular-nums}
.ks-range{flex:none}
.ks-body{padding:24px 20px 14px;transition:opacity .12s}
.ks-body.busy{opacity:.5}
.ks-empty{margin:0}
</style>
