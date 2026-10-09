<script>
import billing from '../state/billing.js'

// Billed spend as bars, one per bucket, stacked by series when there is more than one. Bars, not
// a smoothed area: spend is often idle for days, and a curve would draw money between charges
// that never happened.
//
// The tooltip has one rule: beside the hovered bar (right of it, left near the right edge), at the
// pointer's height clamped inside the plot, so it never covers the card's header and always sits
// next to its bar. A band behind the hovered bucket ties it to its bar even when the bar is tiny.
const W = 600, H = 160, R = 4, GAP = 2

export default {
  props: {
    // [{ id, label, tick, parts: { [seriesId]: micro }, calls? }]
    buckets: { type: Array, required: true },
    // [{ id, name, color? }] in slot order; colors follow the slot, never the rank.
    series: { type: Array, required: true },
    ariaLabel: { type: String, default: 'Billed spend' },
  },
  data: () => ({ hover: null, pointerY: 0, tipH: 80, W, H }),
  computed: {
    totals() { return this.buckets.map(b => Object.values(b.parts || {}).reduce((a, v) => a + v, 0)) },
    max() { return Math.max(...this.totals, 1) },
    slot() { return W / Math.max(this.buckets.length, 1) },
    grid() { return [1, 0.5].map(f => ({ y: H - f * H, text: this.money(Math.round(this.max * f)) })) },
    colors() {
      return Object.fromEntries(this.series.map((s, i) =>
        [s.id, s.color || (s.id === '__other' ? 'var(--sb-other)' : `var(--sb-${(i % 7) + 1})`)]))
    },
    stacks() {
      const width = Math.max(this.slot - 2, 1)
      return this.buckets.map((b, i) => {
        const x = i * this.slot + 1, segs = []
        let base = H
        const present = this.series.filter(s => (b.parts || {})[s.id] > 0)
        present.forEach((s, k) => {
          const h = Math.max((b.parts[s.id] / this.max) * H, 2), top = k === present.length - 1
          const y = base - h
          // A 2px surface gap between stacked segments; only the top segment is rounded.
          const gap = k > 0 ? GAP : 0
          segs.push({ id: s.id, color: this.colors[s.id], path: top ? this.rounded(x, y, width, base - gap) : null,
            x, y, w: width, h: Math.max(base - gap - y, 1) })
          base = y
        })
        return { i, segs }
      })
    },
    ticks() {
      const n = this.buckets.length, step = n <= 8 ? 1 : Math.ceil(n / 6)
      return Array.from({ length: Math.ceil(n / step) }, (_, k) => k * step)
        .map(i => ({ i, left: ((i + 0.5) / n) * 100, text: this.buckets[i].tick }))
    },
    tip() {
      if (this.hover === null) return null
      const b = this.buckets[this.hover], n = this.buckets.length
      const center = (this.hover + 0.5) / n, half = 0.5 / n
      const flip = center > 0.62
      const rows = this.series.length > 1
        ? this.series.filter(s => (b.parts || {})[s.id] > 0).map(s => ({ id: s.id, name: s.name, color: this.colors[s.id], value: this.money(b.parts[s.id]) }))
        : []
      return {
        flip, left: (flip ? center - half : center + half) * 100,
        top: Math.min(Math.max(this.pointerY - this.tipH / 2, 0), Math.max(H - this.tipH, 0)),
        label: b.label, rows, total: this.money(this.totals[this.hover]), calls: b.calls,
      }
    },
  },
  updated() { const el = this.$refs.tip; if (el && el.offsetHeight && el.offsetHeight !== this.tipH) this.tipH = el.offsetHeight },
  methods: {
    money: billing.money,
    rounded(x, y, w, bottom) {
      const rr = Math.min(R, w / 2, bottom - y)
      return `M${x},${bottom}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${bottom}Z`
    },
    move(e) {
      const box = e.currentTarget.getBoundingClientRect()
      if (!box.width) return
      const i = Math.floor(((e.clientX - box.left) / box.width) * this.buckets.length)
      this.hover = Math.min(Math.max(i, 0), this.buckets.length - 1)
      this.pointerY = e.clientY - box.top
    },
  },
}
</script>

<template>
  <div class="spend-bars">
    <div class="sb-plot" @mousemove="move" @mouseleave="hover = null">
      <span v-for="g in grid" :key="g.y" class="sb-ylabel muted" :style="{ top: (g.y / H) * 100 + '%' }">{{g.text}}</span>
      <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" role="img" :aria-label="ariaLabel">
        <rect v-if="hover !== null" :x="hover * slot" y="0" :width="slot" :height="H" class="sb-band" />
        <line v-for="g in grid" :key="g.y" :x1="0" :x2="W" :y1="g.y" :y2="g.y" class="sb-grid" />
        <line :x1="0" :x2="W" :y1="H" :y2="H" class="sb-base" />
        <g v-for="s in stacks" :key="s.i" :class="{ dim: hover !== null && hover !== s.i }" class="sb-stack">
          <template v-for="seg in s.segs" :key="seg.id">
            <path v-if="seg.path" :d="seg.path" :style="{ fill: seg.color }" />
            <rect v-else :x="seg.x" :y="seg.y" :width="seg.w" :height="seg.h" :style="{ fill: seg.color }" />
          </template>
        </g>
      </svg>
      <div v-if="tip" ref="tip" class="sb-tip" :class="{ flip: tip.flip }" :style="{ left: tip.left + '%', top: tip.top + 'px' }">
        <b>{{tip.label}}</b>
        <span v-for="r in tip.rows" :key="r.id"><i><s :style="{ background: r.color }"></s>{{r.name}}</i><em>{{r.value}}</em></span>
        <span :class="{ total: tip.rows.length }"><i>{{tip.rows.length ? 'Total' : 'Spent'}}</i><em>{{tip.total}}</em></span>
        <span v-if="tip.calls != null"><i>Calls</i><em>{{tip.calls}}</em></span>
      </div>
    </div>
    <div class="sb-ticks muted"><span v-for="t in ticks" :key="t.i" :style="{ left: t.left + '%' }">{{t.text}}</span></div>
    <div v-if="series.length > 1" class="sb-legend">
      <span v-for="s in series" :key="s.id"><s :style="{ background: colors[s.id] }"></s>{{s.name}}</span>
    </div>
  </div>
</template>

<style scoped>
.sb-plot{position:relative;margin-left:64px}
.sb-plot svg{display:block;width:100%;height:160px;overflow:visible}
.sb-ylabel{position:absolute;right:calc(100% + 8px);transform:translateY(-50%);font-size:11px;white-space:nowrap;font-variant-numeric:tabular-nums}
.sb-band{fill:var(--ink);opacity:.06}
.sb-grid{stroke:var(--line);stroke-dasharray:3 3;vector-effect:non-scaling-stroke}
.sb-base{stroke:var(--line);vector-effect:non-scaling-stroke}
.sb-stack{transition:opacity .12s}
.sb-stack.dim{opacity:.4}
.sb-tip{position:absolute;transform:translateX(10px);display:grid;gap:4px;min-width:140px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);box-shadow:var(--shadow-md,0 4px 14px rgba(0,0,0,.12));font-size:12px;pointer-events:none;z-index:2;white-space:nowrap}
.sb-tip.flip{transform:translateX(calc(-100% - 10px))}
.sb-tip span{display:flex;justify-content:space-between;gap:16px}
.sb-tip span.total{border-top:1px solid var(--line);padding-top:4px;margin-top:2px}
.sb-tip i{font-style:normal;color:var(--muted);display:inline-flex;align-items:center;gap:6px}
.sb-tip em{font-style:normal;font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink)}
.sb-tip s,.sb-legend s{display:inline-block;width:9px;height:9px;border-radius:2px;text-decoration:none}
.sb-ticks{position:relative;height:16px;margin:8px 0 0 64px;font-size:11px}
.sb-ticks span{position:absolute;transform:translateX(-50%);white-space:nowrap}
.sb-legend{display:flex;flex-wrap:wrap;justify-content:center;gap:6px 16px;margin-top:12px;font-size:12px}
.sb-legend span{display:inline-flex;align-items:center;gap:6px}
</style>
