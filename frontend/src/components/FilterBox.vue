<script setup lang="ts">
// A page's filter box, in the catalog search box's look: typing filters, Esc or × clears. The
// catalog's own box (CatalogSearch) adds the finder on top; every other page's filter is this one.
defineProps<{ label: string, placeholder?: string, register?: (el: Element | null) => void }>()
const q = defineModel<string>({ required: true })
</script>

<template>
<div class="cat-find cn-find">
  <svg class="cat-find-i" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
  <input :ref="el => register && register(el as Element | null)" v-model="q" :aria-label="label" :placeholder="placeholder" @keydown.esc="q=''">
  <button v-if="q" class="cat-find-x" type="button" :aria-label="'Clear: '+label" @click="q=''">×</button>
</div>
</template>
