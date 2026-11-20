<script lang="ts">
import { defineComponent } from 'vue'
import { coverLogos } from '../pages/getting-started-art'

// Decode every logo once so a switch never shows a gradient without its mark.
const decoded = new Map<string, Promise<boolean>>()
function decode(src: string) {
  if (!decoded.has(src)) {
    const image = new Image()
    image.src = src
    decoded.set(src, image.decode().then(() => true, () => false))
  }
  return decoded.get(src)!
}

// Deterministic noise in [0, 1) for a grid cell.
const hash = (x: number, y: number) => { const n = Math.sin(x * 127.1 + y * 311.7) * 43758.5453; return n - Math.floor(n) }

export default defineComponent({
  props: { agent: { type: String, required: true }, name: { type: String, required: true } },
  data: () => ({ shown: '', shownName: '' }),
  computed: { logo(): string { return coverLogos[this.shown] || '' } },
  watch: { agent: { immediate: true, handler(this: any) { this.show() } } },
  mounted() {
    Object.values(coverLogos).forEach(decode)
    const canvas = this.$refs.canvas as HTMLCanvasElement
    const ctx = canvas.getContext('2d')!
    const reduced = matchMedia('(prefers-reduced-motion: reduce)')
    let w = 0, h = 0, frame = 0, last = 0, visible = false
    // Groups of 4-6 neighbouring dots share a pulse, each dot lagging slightly behind the last.
    const draw = (time: number) => {
      frame = 0
      if (time - last < 33 && !reduced.matches) { frame = requestAnimationFrame(draw); return }
      last = time
      const t = reduced.matches ? 0 : time * 0.001, spacing = 4
      ctx.clearRect(0, 0, w, h)
      for (let row = 0; row <= h / spacing; row++) {
        const size = 4 + Math.floor(hash(row, 91) * 3), offset = Math.floor(hash(row, 17) * size)
        const center = 1 - 0.3 * Math.exp(-(((row * spacing - h / 2) / (h * 0.1)) ** 2))
        for (let col = 0; col <= w / spacing; col++) {
          const seed = hash(col, row), group = Math.floor((col + offset) / size), member = (col + offset) % size
          const phase = (t - member * 0.018) * (0.8 + hash(group + 81, row - 29) * 1.3) + hash(group, row) * 19
          const step = Math.floor(phase), blend = phase - step, ease = blend * blend * (3 - 2 * blend)
          const pulse = hash(group + step * 13, row + step * 7) * (1 - ease) + hash(group + (step + 1) * 13, row + (step + 1) * 7) * ease
          const lit = Math.max(0, Math.min(1, (pulse * (0.88 + seed * 0.12) - 0.12) / 0.5))
          ctx.fillStyle = `rgba(255,255,255,${(0.1 + lit * lit * (3 - 2 * lit) * 0.86) * center})`
          const dot = seed > 0.85 ? 1.5 : 1
          ctx.fillRect(col * spacing, row * spacing, dot, dot)
        }
      }
      if (visible && !document.hidden && !reduced.matches) frame = requestAnimationFrame(draw)
    }
    const resume = () => {
      cancelAnimationFrame(frame); frame = 0
      last = 0
      if (visible && !document.hidden && w) draw(performance.now())
    }
    const host = this.$el as HTMLElement
    const resize = new ResizeObserver(() => {
      w = host.clientWidth; h = host.clientHeight
      const ratio = Math.min(devicePixelRatio, 2)
      canvas.width = w * ratio; canvas.height = h * ratio
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
      resume()
    })
    const seen = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; resume() })
    resize.observe(host); seen.observe(host)
    document.addEventListener('visibilitychange', resume)
    reduced.addEventListener('change', resume)
    ;(this as any).stop = () => {
      cancelAnimationFrame(frame); resize.disconnect(); seen.disconnect()
      document.removeEventListener('visibilitychange', resume)
      reduced.removeEventListener('change', resume)
    }
  },
  beforeUnmount() { (this as any).stop?.() },
  methods: {
    async show() {
      const agent = this.agent, name = this.name, src = coverLogos[agent]
      if (src) await decode(src)
      if (agent === this.agent) { this.shown = agent; this.shownName = name }
    },
  },
})
</script>

<template>
<div class="agent-cover" :data-agent="shown" role="img" :aria-label="shownName">
  <div :key="'bg-'+shown" class="agent-cover-bg" aria-hidden="true"></div>
  <canvas ref="canvas" aria-hidden="true"></canvas>
  <img v-if="logo" :key="logo" class="agent-cover-logo" :src="logo" alt="">
  <div class="agent-cover-dots" aria-hidden="true"></div>
  <div class="agent-cover-blur" aria-hidden="true"><i></i><i></i><i></i></div>
  <span :key="shown" class="agent-cover-title" aria-hidden="true">{{shownName}}</span>
  <svg width="0" height="0" aria-hidden="true" style="position:absolute">
    <!-- Geist Pixel has one weight: trim the glyph edges a little. -->
    <filter id="agent-cover-type" x="-5%" y="-10%" width="110%" height="120%"><feMorphology in="SourceGraphic" operator="erode" radius="0.35"/></filter>
  </svg>
</div>
</template>

<style scoped>
.agent-cover { position:relative; isolation:isolate; overflow:hidden; container-type:inline-size; background:#eee; color:#fff; }
.agent-cover-bg, .agent-cover canvas, .agent-cover-dots, .agent-cover-blur { position:absolute; inset:0; pointer-events:none; }
.agent-cover-bg { z-index:0; background:var(--cover-bg); animation:agent-cover-enter .45s ease both; }
.agent-cover canvas { z-index:1; width:100%; height:100%; }
.agent-cover-logo { position:absolute; z-index:2; animation:agent-cover-enter .45s ease both; width:108%; height:108%; right:-38%; bottom:-28%; object-fit:contain; transform:rotate(var(--logo-turn, -12deg)); transition:translate .65s cubic-bezier(.2,.6,.3,1); }
.agent-cover:hover .agent-cover-logo { translate:-5px -5px; }
/* Fixed dots over the opaque mark; the animated grid sits behind it. */
.agent-cover-dots { z-index:3; background:radial-gradient(circle,#ffffff55 .55px,transparent .8px) 0 0/4px 4px; }
/* A progressive blur that deepens toward the bottom edge. */
.agent-cover-blur { z-index:4; }
.agent-cover-blur i { position:absolute; inset:0; backdrop-filter:blur(var(--blur)); mask-image:linear-gradient(to bottom,transparent var(--from),#000 var(--to)); }
.agent-cover-blur i:nth-child(1) { --blur:4px; --from:64%; --to:86%; }
.agent-cover-blur i:nth-child(2) { --blur:10px; --from:78%; --to:100%; }
.agent-cover-blur i:nth-child(3) { --blur:18px; --from:90%; --to:110%; }
.agent-cover-title { position:absolute; inset:0; z-index:5; display:grid; place-content:center; padding:0 12px; text-align:center; font:400 clamp(28px,13.8cqw,55px)/1.05 var(--display); letter-spacing:-.035em; word-spacing:-.14em; filter:url(#agent-cover-type); animation:agent-cover-enter .45s ease both; }
@keyframes agent-cover-enter { from { opacity:0; filter:blur(4px) } }
@media (prefers-reduced-motion:reduce) { .agent-cover-bg, .agent-cover-logo, .agent-cover-title { animation:none; } .agent-cover-logo { transition:none; } }

/* Editorial palettes and crops per agent; not official brand palettes. */
[data-agent="grokbot"] { --cover-bg:radial-gradient(ellipse 95% 43% at 30% 51%,#7399c1 18%,transparent 100%),radial-gradient(ellipse 85% 38% at 80% 0%,#eef6ff,transparent 100%),linear-gradient(150deg,#b8d4ed 0%,#adcce7 56%,#dcecf8 100%); }
[data-agent="hermes"] { --cover-bg:radial-gradient(ellipse 70% 90% at 0% 15%,#809efa,transparent 90%),radial-gradient(ellipse 80% 65% at 85% 100%,#edf1ff,transparent 100%),linear-gradient(35deg,#b5c9ff,#d3e9ff); }
[data-agent="hermes"] .agent-cover-logo { width:104%; height:104%; right:-30%; bottom:-25%; --logo-turn:-8deg; }
[data-agent="claudeai"] { --cover-bg:radial-gradient(ellipse 85% 65% at 12% 0%,#ffe6c6 12%,transparent 100%),radial-gradient(ellipse 65% 110% at 110% 35%,#fff0de 10%,transparent 100%),radial-gradient(ellipse 70% 55% at 0% 100%,#f4c2c6,transparent 100%),linear-gradient(25deg,#e8a696 0%,#efb5a0 48%,#f9d4b6 100%); }
[data-agent="claudeai"] .agent-cover-logo { right:-28%; bottom:-30%; --logo-turn:14deg; }
[data-agent="claude-code"] { --cover-bg:radial-gradient(ellipse 90% 60% at 12% 0%,#efb28e,transparent 85%),radial-gradient(ellipse 60% 90% at 100% 65%,#e8ecd2,transparent 90%),linear-gradient(165deg,#fff0db 30%,#f6d6b7 100%); }
[data-agent="claude-code"] .agent-cover-logo { width:115%; height:115%; right:-27%; bottom:-36%; --logo-turn:0deg; image-rendering:pixelated; }
[data-agent="codex"] { --cover-bg:radial-gradient(ellipse 90% 75% at 90% 0%,#b5a2f5,transparent 85%),radial-gradient(ellipse 55% 110% at 0% 90%,#a7cdff,transparent 90%),linear-gradient(130deg,#e2dcff,#ecedff); }
[data-agent="codex"] .agent-cover-logo { width:94%; height:94%; right:-23%; bottom:-48%; --logo-turn:-8deg; }
[data-agent="opencode"] { --cover-bg:radial-gradient(ellipse 60% 110% at 0% 100%,#e4cbaa,transparent 95%),linear-gradient(125deg,#fff4dd 0%,#f0e7d8 40%,#e8edf2 70%,#faf8f0 100%); }
[data-agent="opencode"] .agent-cover-logo { right:-28%; bottom:-39%; --logo-turn:-8deg; }
[data-agent="pi"] { --cover-bg:radial-gradient(ellipse 90% 60% at 50% 0%,#f4d499,transparent 90%),radial-gradient(ellipse 70% 65% at 0% 100%,#9fc7df,transparent 90%),linear-gradient(160deg,#fff1d7,#e4eff7); }
[data-agent="pi"] .agent-cover-logo { width:96%; height:96%; right:-28%; bottom:-28%; --logo-turn:0deg; }
[data-agent="cursor"] { --cover-bg:radial-gradient(ellipse 45% 120% at 65% 15%,#fff9e8,transparent 100%),linear-gradient(115deg,#b3cbdf 0%,#e2eaf1 48%,#f2e7ce 100%); }
[data-agent="cursor"] .agent-cover-logo { width:100%; height:100%; right:-29%; bottom:-29%; --logo-turn:-16deg; }
[data-agent="gemini-cli"] { --cover-bg:radial-gradient(ellipse 85% 48% at 20% -10%,#fff0bf 18%,transparent 100%),radial-gradient(ellipse 45% 32% at 0% 105%,#d4eddf 10%,transparent 100%),radial-gradient(ellipse 65% 50% at 100% 100%,#fff1db,transparent 100%),linear-gradient(115deg,#b0d4ef 0%,#c4e0f5 45%,#edf4fa 80%); }
[data-agent="gemini-cli"] .agent-cover-logo { width:120%; height:120%; right:-37%; bottom:-40%; }
[data-agent="other"] { --cover-bg:radial-gradient(ellipse 65% 95% at 100% 5%,#f8bd8d,transparent 90%),radial-gradient(ellipse 80% 60% at 15% 100%,#bda0ee,transparent 95%),linear-gradient(30deg,#e7cefa,#ffe9cd); }
[data-agent="other"] .agent-cover-logo { width:94%; height:94%; right:-27%; bottom:-48%; --logo-turn:-10deg; }
</style>
