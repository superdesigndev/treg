<script>
import motion from './dropdownMotion.js'

// A date range in one button: quick picks on the left, a two-month calendar on the right. Days are
// UTC, like every report it drives. Picking a quick range applies at once; a calendar range is
// two clicks (start, end) and Apply, so a half-made range never reloads the page.
const DAY = 86_400_000
const MAX_DAYS = 366
const iso = t => new Date(t).toISOString().slice(0, 10)
const at = s => Date.parse(s + 'T00:00:00Z')
const fmt = (s, opts) => new Date(at(s)).toLocaleDateString(undefined, { timeZone: 'UTC', ...opts })
const todayIso = () => iso(Date.now())

function monthRange(offset) {
  const now = new Date(), y = now.getUTCFullYear(), m = now.getUTCMonth() + offset
  const first = Date.UTC(y, m, 1), last = Math.min(Date.UTC(y, m + 1, 0), at(todayIso()))
  return { from: iso(first), to: iso(last) }
}

const QUICK = [
  { id: 7, label: 'Last 7 days' }, { id: 30, label: 'Last 30 days' }, { id: 90, label: 'Last 3 months' },
  { id: 'this_month', label: 'This month', range: () => monthRange(0) },
  { id: 'last_month', label: 'Last month', range: () => monthRange(-1) },
]

export default {
  mixins: [motion],
  props: {
    // { preset: 7|30|90|'custom', from, to }
    modelValue: { type: Object, required: true },
    label: { type: String, default: 'Date range' },
  },
  emits: ['update:modelValue', 'change'],
  data: () => ({ open: false, start: '', end: '', hoverDay: '', view: 0, QUICK, WEEK: ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'] }),
  computed: {
    today() { return todayIso() },
    buttonText() {
      const v = this.modelValue
      if (v.preset !== 'custom') return (QUICK.find(q => q.id === v.preset) || { label: `Last ${v.preset} days` }).label
      const sameYear = v.from.slice(0, 4) === v.to.slice(0, 4)
      return fmt(v.from, { month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }) }) + ' → ' + fmt(v.to, { month: 'short', day: 'numeric', year: 'numeric' })
    },
    // The two months on show: the month before `view` and `view` itself (0 = the current month).
    months() {
      const now = new Date()
      return [-1, 0].map(k => {
        const first = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + this.view + k, 1))
        const lead = (first.getUTCDay() + 6) % 7, days = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate()
        const cells = [...Array(lead).fill(null), ...Array.from({ length: days }, (_, d) => iso(first.getTime() + d * DAY))]
        return { key: iso(first.getTime()), title: fmt(iso(first.getTime()), { month: 'long', year: 'numeric' }), cells }
      })
    },
    pendingEnd() { return this.end || (this.start && this.hoverDay ? this.hoverDay : '') },
    span() { return this.start && this.end ? Math.round((at(this.end) - at(this.start)) / DAY) + 1 : 0 },
    tooLong() { return this.span > MAX_DAYS },
  },
  beforeUnmount() { document.removeEventListener('pointerdown', this.outside, true) },
  methods: {
    show() {
      const v = this.modelValue
      this.start = v.preset === 'custom' ? v.from : iso(at(todayIso()) - ((v.preset || 30) - 1) * DAY)
      this.end = v.preset === 'custom' ? v.to : todayIso()
      this.view = 0
      this.open = true
      this.motionOpen()
      document.addEventListener('pointerdown', this.outside, true)
    },
    close() { if (!this.open) return; this.open = false; this.motionClose(); document.removeEventListener('pointerdown', this.outside, true) },
    outside(e) { if (!this.$el.contains(e.target)) this.close() },
    emit(value) { this.$emit('update:modelValue', value); this.$emit('change', value) },
    quick(q) {
      this.close()
      this.emit(q.range ? { preset: 'custom', ...q.range() } : { preset: q.id, from: '', to: '' })
    },
    clickDay(day) {
      if (!day || day > this.today) return
      if (!this.start || this.end) { this.start = day; this.end = '' }
      else if (day < this.start) { this.end = this.start; this.start = day }
      else this.end = day
    },
    apply() { if (this.start && this.end && !this.tooLong) { this.close(); this.emit({ preset: 'custom', from: this.start, to: this.end }) } },
    dayClass(day) {
      const lo = this.start, hi = this.pendingEnd && this.pendingEnd >= lo ? this.pendingEnd : lo
      return { future: day > this.today, today: day === this.today, edge: day === this.start || day === this.end,
               inside: lo && hi && day > lo && day < hi }
    },
    isQuick(q) {
      const v = this.modelValue
      if (!q.range) return v.preset === q.id
      const r = q.range()
      return v.preset === 'custom' && v.from === r.from && v.to === r.to
    },
    dayNumber: day => Number(day.slice(8)),
  },
}
</script>

<template>
  <div class="drp" :class="{ open }">
    <button type="button" class="drp-button" aria-haspopup="dialog" :aria-expanded="open" :aria-label="label + ': ' + buttonText" @click="open ? close() : show()">
      <svg class="drp-icon" viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="2" /><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" /></svg>
      <span>{{buttonText}}</span>
      <svg class="drp-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" /></svg>
    </button>
    <div v-if="mounted" class="drp-panel t-dropdown" :class="phase" data-origin="top-left" role="dialog" :aria-label="label" @keydown.esc="close()">
      <ul class="drp-quick">
        <li v-for="q in QUICK" :key="q.id"><button type="button" :class="{ on: isQuick(q) }" @click="quick(q)">{{q.label}}</button></li>
      </ul>
      <div class="drp-cal">
        <div class="drp-months">
          <div v-for="(m, k) in months" :key="m.key" class="drp-month">
            <div class="drp-month-head">
              <button v-if="k === 0" type="button" class="drp-nav" aria-label="Earlier months" @click="view--">‹</button><span v-else />
              <b>{{m.title}}</b>
              <button v-if="k === 1" type="button" class="drp-nav" aria-label="Later months" :disabled="view >= 0" @click="view++">›</button><span v-else />
            </div>
            <div class="drp-grid">
              <span v-for="w in WEEK" :key="w" class="drp-wd">{{w}}</span>
              <template v-for="(day, i) in m.cells" :key="i">
                <span v-if="!day" />
                <button v-else type="button" class="drp-day" :class="dayClass(day)" :disabled="day > today"
                        :aria-pressed="day === start || day === end" :aria-label="day"
                        @click="clickDay(day)" @mouseenter="hoverDay = day">{{dayNumber(day)}}</button>
              </template>
            </div>
          </div>
        </div>
        <div class="drp-foot">
          <span class="muted">
            <template v-if="tooLong">A range can span at most {{366}} days.</template>
            <template v-else-if="start && end">{{span}} {{span === 1 ? 'day' : 'days'}} · UTC</template>
            <template v-else-if="start">Pick the last day</template>
            <template v-else>Pick the first day</template>
          </span>
          <button type="button" class="btn sm" @click="close()">Cancel</button>
          <button type="button" class="btn sm primary" :disabled="!start || !end || tooLong" @click="apply">Apply</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.drp{position:relative;display:inline-block}
.drp-button{display:flex;align-items:center;gap:10px;height:52px;padding:0 16px 0 18px;border:0;border-radius:14px;background:var(--surface,var(--panel));
  box-shadow:var(--shadow-sm);color:var(--ink);font:14px var(--sans);cursor:pointer;white-space:nowrap}
.drp.open .drp-button,.drp-button:focus-visible{outline:none;box-shadow:var(--shadow-sm),0 0 0 1.5px var(--ink)}
.drp-icon,.drp-chevron{flex:none;width:15px;height:15px;fill:none;stroke:var(--muted);stroke-width:1.4;stroke-linecap:round;stroke-linejoin:round}
.drp-chevron{width:14px;stroke-width:1.6;transition:transform var(--dropdown-open-dur,250ms) var(--dropdown-ease,ease)}
.drp.open .drp-chevron{transform:rotate(180deg)}
.drp-panel{position:absolute;z-index:30;top:calc(100% + 8px);left:0;display:flex;background:var(--panel);border:1px solid var(--line);border-radius:14px;
  box-shadow:var(--shadow-md,0 10px 30px rgba(0,0,0,.16));overflow:hidden}
.drp-quick{margin:0;padding:10px;list-style:none;border-right:1px solid var(--line);min-width:150px}
.drp-quick button{display:block;width:100%;padding:8px 10px;border:0;border-radius:8px;background:none;color:var(--ink);font:13.5px var(--sans);text-align:left;cursor:pointer}
.drp-quick button:hover,.drp-quick button:focus-visible{background:var(--panel2);outline:none}
.drp-quick button.on{background:var(--panel2);font-weight:600}
.drp-cal{padding:14px 16px 12px}
.drp-months{display:flex;gap:24px}
.drp-month-head{display:grid;grid-template-columns:28px 1fr 28px;align-items:center;margin-bottom:8px;text-align:center;font-size:13.5px}
.drp-nav{width:28px;height:28px;border:0;border-radius:8px;background:none;color:var(--ink);font-size:18px;line-height:1;cursor:pointer}
.drp-nav:hover:not(:disabled){background:var(--panel2)}
.drp-nav:disabled{opacity:.3;cursor:default}
.drp-grid{display:grid;grid-template-columns:repeat(7,34px);gap:2px 0}
.drp-wd{height:26px;display:grid;place-items:center;font-size:11px;color:var(--muted)}
.drp-day{height:34px;border:0;background:none;color:var(--ink);font:13px var(--sans);font-variant-numeric:tabular-nums;cursor:pointer;border-radius:8px}
.drp-day:hover:not(:disabled){background:var(--panel2)}
.drp-day.today{text-decoration:underline;text-underline-offset:3px}
.drp-day.inside{background:color-mix(in srgb,var(--ink) 8%,transparent);border-radius:0}
.drp-day.edge{background:var(--ink);color:var(--bg);border-radius:8px}
.drp-day:disabled{color:var(--muted);opacity:.4;cursor:default}
.drp-foot{display:flex;align-items:center;gap:8px;margin-top:12px;padding-top:10px;border-top:1px solid var(--line);font-size:12.5px}
.drp-foot .muted{margin-right:auto}
@media (max-width:760px){.drp-panel{flex-direction:column}.drp-quick{border-right:0;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap}.drp-months{flex-direction:column}}
</style>
