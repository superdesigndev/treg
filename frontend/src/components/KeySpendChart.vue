<script>
import billing from '../state/billing.js'
import SpendBars from './SpendBars.vue'

// One key's billed spend per UTC day: a single series, so no legend - the title names it. Days
// the server left out are drawn empty, so the bars always span the whole window.
const WINDOWS = [[7, 'Last 7 days'], [30, 'Last 30 days'], [90, 'Last 3 months']]
const SERIES = [{ id: 'spend', name: 'Spent', color: 'var(--accent)' }]

const label = day => new Date(day + 'T00:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })

export default {
  components: { SpendBars },
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
      <select class="pl-select ks-range" :value="spend.days" :disabled="busy" aria-label="Date range"
              @change="$emit('days', Number($event.target.value))">
        <option v-for="[d, text] in WINDOWS" :key="d" :value="d">{{text}}</option>
      </select>
    </header>
    <div class="ks-body" :class="{ busy }">
      <p v-if="!total" class="muted ks-empty">No billed calls on this key in this range.</p>
      <template v-else>
        <SpendBars :buckets="buckets" :series="SERIES"
                   :aria-label="`Billed spend per day for ${keyName}: ${money(total)} over ${spend.days} days`" />
        <details class="ks-table">
          <summary class="muted">Show as table</summary>
          <table><tr><th>Day</th><th style="text-align:right">Calls</th><th style="text-align:right">Spent</th></tr>
            <tr v-for="d in days.filter(d => d.spend_micro)" :key="d.day"><td class="mono">{{d.day}}</td><td style="text-align:right">{{d.calls}}</td><td style="text-align:right">{{money(d.spend_micro)}}</td></tr>
          </table>
        </details>
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
.ks-table{margin-top:14px}
.ks-table summary{cursor:pointer;font-size:12px}
/* The chart sits inside the key list's table: undo its row padding and narrow last column. */
.ks-table table{margin-top:6px;width:auto;min-width:320px;border-collapse:collapse}
.ks-table th,.ks-table td{padding:6px 12px;width:auto;white-space:nowrap;vertical-align:middle}
.ks-table th:last-child,.ks-table td:last-child{width:auto}
</style>
