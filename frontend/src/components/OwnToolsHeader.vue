<script>
import { useDashboard } from '../state/context'
import PageTabs from './PageTabs.vue'

// The head of the three "Your own tools" pages (tools, secrets, team resources): the shelf's hero, and
// the three as one tab row, the catalog's. The lede is the family's, so the tabs never move; each
// page adds its own note under them, and its own actions.
export default {
  components: { PageTabs },
  props: { current: { type: String, required: true } },
  setup: useDashboard,
  computed: {
    tabs() {
      return [
        { key: 'tools', label: 'Skills & tools' },
        ...(this.canRegister ? [{ key: 'secrets', label: 'Secrets' }] : []),
        { key: 'resources', label: 'Team resources' },
      ]
    },
  },
}
</script>

<template>
<header class="pl-hero pl-hero-row">
  <div>
    <h1>Your own tools</h1>
    <p class="pl-lede">Your team's APIs, CLIs and skills, and the keys behind them. Agents never hold the keys.</p>
  </div>
  <div class="pl-hero-a"><slot name="actions" /></div>
</header>
<PageTabs label="Your own tools" :tabs="tabs" :current="current" @select="go" />
<p v-if="$slots.default" class="cat-hint own-note"><slot /></p>
</template>
