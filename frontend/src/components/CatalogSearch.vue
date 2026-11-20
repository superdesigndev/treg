<script>
import { useDashboard } from '../state/context'

// The catalog's one search box, on the Catalog page and on every platform shelf. Typing filters what
// the page shows at once; when typing pauses, the finder answers what was typed above the still-
// filtered page, and Enter asks for the full answer (GET /catalog/find, the
// relevance judge; state/find.js). On a shelf `scope` is its slug and the finder reads that shelf
// only. Clearing the box is how you leave an answer.
export default {
  props: {
    modelValue: { type: String, default: '' },
    scope: { type: String, default: '' },
    scopeLabel: { type: String, default: '' },
    placeholder: { type: String, default: '' },
  },
  emits: ['update:modelValue'],
  setup: useDashboard,
  beforeUnmount() { this.findUnschedule() },
}
</script>

<template>
<div class="cat-search">
  <div class="cat-find">
    <svg class="cat-find-i" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
    <input :ref="el => setElement(scope ? 'shelfSearch' : 'search', el)" :value="modelValue"
           :aria-label="scope ? 'Search '+scopeLabel : 'Search the catalog'" :placeholder="placeholder"
           @input="$emit('update:modelValue', $event.target.value); findSchedule($event.target.value, scope)"
           @keydown.enter="modelValue.trim() && findRun(modelValue, {scope})"
           @keydown.esc="$emit('update:modelValue', ''); findExit()">
    <button v-if="modelValue" class="cat-find-x" type="button" aria-label="Clear the search" @click="$emit('update:modelValue', ''); findExit()">×</button>
  </div>
  <!-- A sentence is a job, not a name: say so where the eye already is, as one clickable row. For a
       short query the row is quieter, because the page is already filtering by it as you type. -->
  <button v-if="modelValue.trim() && !(findFull && find.scope===scope)" class="cat-find-suggest" :class="{quiet:!findIsJob(modelValue)}"
          type="button" @click="findRun(modelValue, {scope})">
    <span class="cat-find-suggest-i" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg></span>
    <span class="cat-find-suggest-t">{{findIsJob(modelValue) ? 'Find tools for' : 'Search '+(scope ? scopeLabel : 'all tools')+' for'}} <b>“{{modelValue.trim()}}”</b><template
      v-if="scope && findIsJob(modelValue)"> in {{scopeLabel}}</template></span>
    <kbd>Enter</kbd>
  </button>
</div>
</template>
