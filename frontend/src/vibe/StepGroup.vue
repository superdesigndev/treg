<script setup lang="ts">
// A run of the agent's routine steps (searching, reading, listing) as one quiet line that opens to
// the steps. A failed step keeps the line visible and marked; one step alone is its own card.
import { computed, ref } from 'vue'
import StepCard from './StepCard.vue'
import type { Msg } from './types'

const props = defineProps<{ steps: Msg[], sessionId: number, outputFields: string[] }>()
const emit = defineEmits<{ revert: [n: number] }>()
const open = ref(false)
const failed = computed(() => props.steps.filter(s => !s.ok).length)
const VERBS: Record<string, string> = {
  catalog_search: 'searched the catalog', catalog_get: 'read tools', my_tools: 'listed your tools',
  load_my_tool: 'loaded a tool', read_files: 'read files', write_files: 'wrote files',
}
const line = computed(() => {
  const seen: string[] = []
  for (const s of props.steps) { const v = VERBS[s.name || ''] || s.name || ''; if (!seen.includes(v)) seen.push(v) }
  const text = seen.join(', ')
  return text.charAt(0).toUpperCase() + text.slice(1)
})
</script>

<template>
  <div class="vb-group-steps" :class="{ bad: failed, open }">
    <button type="button" class="vb-group-head" :aria-expanded="open" @click="open = !open">
      <span class="vb-dot" aria-hidden="true">{{ failed ? '!' : '✓' }}</span>
      <span class="vb-step-text">{{ line }}</span>
      <span class="sa-muted vb-group-count">{{ steps.length }} steps{{ failed ? ` · ${failed} failed` : '' }}</span>
      <span class="vb-chev" aria-hidden="true">{{ open ? '▾' : '▸' }}</span>
    </button>
    <div v-if="open" class="vb-group-body">
      <StepCard v-for="s in steps" :key="s.id" :msg="s" :session-id="sessionId" :latest="false" :output-fields="outputFields"
                @revert="n => emit('revert', n)"/>
    </div>
  </div>
</template>
