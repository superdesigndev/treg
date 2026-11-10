<script>
import billing from '../../state/billing.js'

// An amount read to the cent (cut down, never rounded up), whose exact micro-USD figure appears
// at once on hover or keyboard focus. The bubble is fixed to the viewport, so a table or card that
// clips its overflow never cuts it off. When the short figure already is the exact one, there is
// nothing to reveal and it renders as plain text.
let uid = 0

export default {
  props: {
    micro: { type: Number, required: true },
  },
  data: () => ({ open: false, at: { top: 0, left: 0 }, id: 'money-' + (++uid) }),
  computed: {
    short() { return billing.moneyGlance.call(billing, this.micro) },
    exact() { return billing.money(this.micro) },
    differs() { return this.short !== this.exact },
  },
  beforeUnmount() { window.removeEventListener('scroll', this.hide, true) },
  methods: {
    show() {
      const r = this.$el.getBoundingClientRect()
      this.at = { top: r.top, left: r.right }
      this.open = true
      window.addEventListener('scroll', this.hide, true)
    },
    hide() {
      this.open = false
      window.removeEventListener('scroll', this.hide, true)
    },
  },
}
</script>

<template>
  <span v-if="!differs" class="money">{{short}}</span>
  <span v-else class="money" tabindex="0" :aria-describedby="id" @mouseenter="show" @mouseleave="hide" @focus="show" @blur="hide">{{short}}<span
    :id="id" role="tooltip" class="money-tip" :class="{ open }" :style="{ top: at.top + 'px', left: at.left + 'px' }">{{exact}}</span></span>
</template>

<style scoped>
/* Looks exactly like the text around it (a card's or table's own rules for spans must not restyle
   it); only the hover or focus reveals anything. */
.money{font:inherit;color:inherit;letter-spacing:inherit;outline:none}
.money:focus-visible{border-radius:3px;box-shadow:0 0 0 2px var(--ink)}
.money-tip{position:fixed;z-index:60;transform:translate(-100%,calc(-100% - 6px)) scale(var(--tt-scale,.98));transform-origin:bottom right;
  padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);box-shadow:var(--shadow-md,0 6px 24px rgba(0,0,0,.14));
  font:500 12px var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap;pointer-events:none;visibility:hidden;opacity:0;
  transition:opacity var(--tt-out-dur,50ms) var(--tt-out-ease,ease-out),visibility 0s linear var(--tt-out-dur,50ms)}
.money-tip.open{visibility:visible;opacity:1;transform:translate(-100%,calc(-100% - 6px));
  transition:opacity var(--tt-in-dur,150ms) var(--tt-in-ease,ease-out),transform var(--tt-in-dur,150ms) var(--tt-in-ease,ease-out)}
@media (prefers-reduced-motion: reduce){.money-tip,.money-tip.open{transition:none}}
</style>
