<script>
// A small ⓘ that explains a term on hover or keyboard focus: the same pattern as the arena pages'
// info tooltips, drawn with the dashboard's theme colors.
let uid = 0

export default {
  props: {
    title: { type: String, required: true },
    text: { type: String, required: true },
    align: { type: String, default: 'left' },  // left | right: which edge the tooltip hangs from
  },
  data: () => ({ id: 'tip-' + (++uid) }),
}
</script>

<template>
  <span class="it" :class="'it-' + align">
    <button type="button" class="it-trigger" :aria-label="'About ' + title" :aria-describedby="id">
      <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="10" r="7" /><path d="M10 9v5M10 6v1" /></svg>
    </button>
    <span :id="id" role="tooltip" class="it-tip"><strong>{{title}}</strong><span>{{text}}</span></span>
  </span>
</template>

<style scoped>
.it{position:relative;display:inline-flex;align-items:center;vertical-align:middle}
.it-trigger{display:grid;place-items:center;width:20px;height:20px;padding:3px;border:0;border-radius:50%;background:transparent;color:var(--muted);cursor:help}
.it-trigger:hover,.it-trigger:focus-visible{color:var(--ink);background:var(--panel2)}
.it-trigger svg{width:14px;height:14px;stroke:currentColor;stroke-width:1.4}
.it-tip{position:absolute;z-index:25;bottom:calc(100% + 8px);left:-6px;transform-origin:bottom left;box-sizing:border-box;width:280px;max-width:calc(100vw - 36px);padding:12px 14px;
  border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--muted);box-shadow:var(--shadow-md,0 6px 24px rgba(0,0,0,.14));
  font-size:12px;line-height:1.5;text-align:left;white-space:normal;visibility:hidden;opacity:0;pointer-events:none;
  transform:scale(var(--tt-scale,.98));
  /* transitions.dev tooltip timing: out is near-instant, in waits a beat and then fades + scales up. */
  transition:opacity var(--tt-out-dur,50ms) var(--tt-out-ease,ease-out),transform var(--tt-out-dur,50ms) var(--tt-out-ease,ease-out),visibility 0s linear var(--tt-out-dur,50ms)}
.it-right .it-tip{left:auto;right:-6px;transform-origin:bottom right}
.it-tip strong{display:block;margin-bottom:4px;color:var(--ink);font-size:12.5px;font-weight:500}
.it:is(:hover,:focus-within) .it-tip{visibility:visible;opacity:1;transform:scale(1);
  transition:opacity var(--tt-in-dur,150ms) var(--tt-in-ease,ease-out) var(--tt-delay,80ms),transform var(--tt-in-dur,150ms) var(--tt-in-ease,ease-out) var(--tt-delay,80ms),visibility 0s linear var(--tt-delay,80ms)}
@media (prefers-reduced-motion: reduce){.it-tip,.it:is(:hover,:focus-within) .it-tip{transition:none}}
</style>
