<script>
import billing from '../state/billing.js'
import InfoTip from './ui/InfoTip.vue'

// Billed spend as bars, one per bucket, stacked by series when there is more than one. Bars, not
// a smoothed area: spend is often idle for days, and a curve would draw money between charges
// that never happened.
//
// The tooltip has one rule: beside the hovered bar (right of it, left near the right edge), at the
// pointer's height clamped inside the plot, so it never covers the card's header and always sits
// next to its bar. A band behind the hovered bucket ties it to its bar even when the bar is tiny.
const W = 600, H = 160, R = 4, GAP = 2
let uid = 0

export default {
  components: { InfoTip },
  props: {
    // [{ id, label, tick, parts: { [seriesId]: micro }, calls?, others?: [{ name, value }] }]
    buckets: { type: Array, required: true },
    // [{ id, name, slot?, color?, info?, members?, folded? }]; `folded` lists what Other holds. `slot` is the series' own color (the server keeps
    // it stable across filters); without one, the series takes its position's.
    series: { type: Array, required: true },
    ariaLabel: { type: String, default: 'Billed spend' },
  },
  data: () => ({ hover: null, pointerY: 0, tipH: 80, W, H, uid: ++uid }),
  computed: {
    totals() { return this.buckets.map(b => Object.values(b.parts || {}).reduce((a, v) => a + v, 0)) },
    // The scale tops out at a round amount (1, 2, 2.5 or 5 × a power of ten) above the tallest bar,
    // so the guides read as $0.50 and $0.25 rather than $0.4106 and $0.2053.
    max() {
      const top = Math.max(...this.totals, 1), step = 10 ** Math.floor(Math.log10(top))
      return [1, 2, 2.5, 5, 10].map(m => m * step).find(v => v >= top)
    },
    slot() { return W / Math.max(this.buckets.length, 1) },
    grid() { return [1, 0.5].map(f => ({ y: H - f * H, text: this.money(Math.round(this.max * f)) })) },
    colors() {
      return Object.fromEntries(this.series.map((s, i) =>
        [s.id, s.color || (s.id === '__other' ? 'var(--sb-other)' : `var(--sb-${((s.slot ?? i) % 7) + 1})`)]))
    },
    stacks() {
      // A few wide buckets (weeks, months) get a narrower bar centered in its slot, not a slab.
      const width = this.slot > 24 ? Math.max(this.slot * 0.55, 22) : Math.max(this.slot - 2, 1)
      return this.buckets.map((b, i) => {
        const x = i * this.slot + (this.slot - width) / 2, segs = []
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
        ? this.series.filter(s => (b.parts || {})[s.id] > 0).map(s => ({
            id: s.id, name: s.name, color: this.colors[s.id], value: this.money(b.parts[s.id]),
            // What the Other slice holds in this bucket, biggest first; the rest is counted, not listed.
            inside: s.id === '__other' ? (b.others || []).slice(0, 4).map(o => ({ name: o.name, value: this.money(o.value) })) : [],
            more: s.id === '__other' ? Math.max((b.others || []).length - 4, 0) : 0,
          }))
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
        <template v-for="r in tip.rows" :key="r.id">
          <span><i><s :style="{ background: r.color }"></s>{{r.name}}</i><em>{{r.value}}</em></span>
          <span v-for="o in r.inside" :key="o.name" class="inside"><i>{{o.name}}</i><em>{{o.value}}</em></span>
          <span v-if="r.more" class="inside"><i>+ {{r.more}} more</i></span>
        </template>
        <span :class="{ total: tip.rows.length }"><i>{{tip.rows.length ? 'Total' : 'Spent'}}</i><em>{{tip.total}}</em></span>
        <span v-if="tip.calls != null"><i>Calls</i><em>{{tip.calls}}</em></span>
      </div>
    </div>
    <div class="sb-ticks muted"><span v-for="t in ticks" :key="t.i" :style="{ left: t.left + '%' }">{{t.text}}</span></div>
    <div v-if="series.length > 1" class="sb-legend">
      <template v-for="s in series" :key="s.id">
        <!-- Other names what it folded: hover or focus it for the list, which the pointer can enter and scroll. -->
        <span v-if="s.folded && s.folded.length" class="sb-folded" tabindex="0" :aria-describedby="'sb-folded-' + uid">
          <s :style="{ background: colors[s.id] }"></s><u>{{s.name}}</u>
          <span :id="'sb-folded-' + uid" role="tooltip" class="sb-folded-tip"><span class="sb-folded-box">
            <strong>{{s.name}}</strong>
            <span v-for="f in s.folded" :key="f.id" class="sb-folded-row"><i :title="f.name">{{f.name}}</i><em>{{f.value}}</em></span>
          </span></span>
        </span>
        <span v-else><s :style="{ background: colors[s.id] }"></s>{{s.name}}<InfoTip v-if="s.info" :title="s.info.title" :text="s.info.text" /></span>
      </template>
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
.sb-tip span.inside{padding-left:15px;font-size:11.5px}
.sb-tip span.inside i{max-width:180px;overflow:hidden;text-overflow:ellipsis;display:block}
.sb-tip span.total{border-top:1px solid var(--line);padding-top:4px;margin-top:2px}
.sb-tip i{font-style:normal;color:var(--muted);display:inline-flex;align-items:center;gap:6px}
.sb-tip em{font-style:normal;font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink)}
.sb-tip s,.sb-legend s{display:inline-block;width:9px;height:9px;border-radius:2px;text-decoration:none}
.sb-ticks{position:relative;height:16px;margin:8px 0 0 64px;font-size:11px}
.sb-ticks span{position:absolute;transform:translateX(-50%);white-space:nowrap}
.sb-legend{display:flex;flex-wrap:wrap;justify-content:center;gap:6px 16px;margin-top:12px;font-size:12px}
.sb-legend>span{display:inline-flex;align-items:center;gap:6px}
.sb-folded{position:relative;display:inline-flex;align-items:center;gap:6px;cursor:default;outline:none}
.sb-folded u{text-decoration:underline dotted color-mix(in srgb,var(--muted) 70%,transparent);text-underline-offset:3px}
.sb-folded:focus-visible u{outline:2px solid var(--ink);outline-offset:2px;border-radius:2px}
/* Opens above the label. The 10px padding under it bridges the gap, so the pointer can travel
   from the label into the list and scroll it without the hover dropping. Timing follows the
   transitions.dev tooltip: a short delay in, near-instant out. */
.sb-folded-tip{position:absolute;z-index:25;bottom:100%;right:-8px;padding-bottom:10px;width:280px;
  visibility:hidden;opacity:0;transform:scale(var(--tt-scale,.98));transform-origin:bottom right;
  transition:opacity var(--tt-out-dur,50ms) var(--tt-out-ease,ease-out),transform var(--tt-out-dur,50ms) var(--tt-out-ease,ease-out),visibility 0s linear var(--tt-out-dur,50ms)}
.sb-folded-box{display:grid;gap:4px;box-sizing:border-box;max-height:260px;overflow:auto;padding:12px 14px;border:1px solid var(--line);border-radius:12px;
  background:var(--panel);box-shadow:var(--shadow-md,0 6px 24px rgba(0,0,0,.14));font-size:12px;text-align:left}
.sb-folded:is(:hover,:focus-within) .sb-folded-tip{visibility:visible;opacity:1;transform:scale(1);
  transition:opacity var(--tt-in-dur,150ms) var(--tt-in-ease,ease-out) var(--tt-delay,80ms),transform var(--tt-in-dur,150ms) var(--tt-in-ease,ease-out) var(--tt-delay,80ms),visibility 0s linear var(--tt-delay,80ms)}
.sb-folded-tip strong{color:var(--ink);font-size:12.5px;font-weight:500;margin-bottom:2px}
.sb-folded-row{display:flex;justify-content:space-between;gap:14px}
.sb-folded-row i{font-style:normal;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-folded-row em{font-style:normal;font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink)}
@media (prefers-reduced-motion: reduce){.sb-folded-tip,.sb-folded:is(:hover,:focus-within) .sb-folded-tip{transition:none}}
</style>
