<script setup lang="ts">
import { computed } from 'vue'
import { useDashboard } from '../state/context'

const dash = useDashboard()
const CATALOG = ['catalog', 'platform', 'provider']
const YOURS = ['tools', 'secrets', 'resources', 'detail', 'wbtool', 'wbinstall']
const HUB = ['hub', 'run']
const scope = computed(() => CATALOG.includes(dash.view) ? 'catalog' : HUB.includes(dash.view) ? 'hub' : YOURS.includes(dash.view) ? 'yours' : '')
const scopes = computed(() => [
  { key: 'catalog', label: 'Catalog', n: dash.toolCountText, go: 'catalog' },
  { key: 'yours', label: 'Yours', n: dash.ownToolCount || '', go: 'tools' },
  ...(dash.hubOn ? [{ key: 'hub', label: 'Hub', n: '', go: 'hub' }] : []),
])
</script>

<template>
<header class="pl tools-hero">
  <div>
    <h1>Tools</h1>
    <p class="pl-lede">Everything your agent can call: the catalog, your own tools and scripts, and what other makers published.</p>
  </div>
  <div class="seg pl-seg tools-scope" role="radiogroup" aria-label="Tools">
    <button v-for="s in scopes" :key="s.key" role="radio" :aria-checked="scope === s.key" :class="{ on: scope === s.key }" @click="scope === s.key || dash.go(s.go)">{{ s.label }} <span v-if="s.n">{{ s.n }}</span></button>
  </div>
</header>
</template>

<style>
.tools-hero{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:28px 0 0;margin-bottom:24px}
.tools-hero h1{margin:0;font-family:var(--display);font-weight:400;font-size:26px;line-height:1.25}
.tools-hero .pl-lede{margin:6px 0 0}
/* The scopes' pages keep their search, tabs and actions; their own title row folds into this one. */
.tools-merged .pl.cat > .pl-hero h1,.tools-merged .pl.own > .pl-hero > div,.tools-merged .hub-title{display:none}
.tools-merged .pl.cat > .pl-hero{padding-top:0;padding-bottom:28px}
.tools-merged .pl.own > .pl-hero{padding-top:0;padding-bottom:16px;justify-content:flex-end}
.tools-merged .pl.hub{padding-top:4px}
</style>
