<script setup lang="ts">
import { computed } from 'vue'
import { diffFiles } from './diff'

const props = defineProps<{ before: Record<string, unknown>, after: Record<string, unknown> }>()
const files = computed(() => diffFiles(props.before, props.after))
</script>

<template>
  <div class="vb-diff">
    <p v-if="!files.length" class="sa-muted vb-small">No changes.</p>
    <details v-for="f in files" :key="f.file" open>
      <summary><code>{{ f.file }}</code> <span class="vb-add">+{{ f.added }}</span> <span class="vb-del">−{{ f.removed }}</span></summary>
      <pre><template v-for="(l, i) in f.lines" :key="i"><span :class="`vb-l-${l.kind}`">{{ l.kind === 'add' ? '+ ' : l.kind === 'del' ? '− ' : l.kind === 'gap' ? '⋯ ' : '  ' }}{{ l.text }}
</span></template></pre>
    </details>
  </div>
</template>

<style>
.vb-diff { display:flex; flex-direction:column; gap:8px; }
.vb-diff summary { cursor:pointer; font-size:12.5px; }
.vb-diff pre { margin:6px 0 0; background:var(--panel2); border-radius:8px; padding:8px 0; font:11.5px/1.5 var(--mono); overflow:auto; max-height:360px; }
.vb-diff pre span { display:block; padding:0 10px; white-space:pre-wrap; overflow-wrap:anywhere; }
.vb-l-add { background:color-mix(in srgb,var(--ok) 14%,transparent); }
.vb-l-del { background:color-mix(in srgb,var(--bad) 13%,transparent); }
.vb-l-gap { color:var(--muted); font-style:italic; }
.vb-add { color:var(--ok); font:500 12px var(--mono); }
.vb-del { color:var(--bad); font:500 12px var(--mono); }
</style>
