<script setup lang="ts">
// A page's own sections as the catalog's tab row: Your own tools' three pages, Activity's two readings.
defineProps<{ label: string, tabs: { key: string, label: string }[], current: string }>()
defineEmits<{ select: [key: string] }>()
</script>

<template>
<div class="mk-tabs-wrap pl-tabs">
  <div class="mk-tabs" role="tablist" :aria-label="label">
    <button v-for="t in tabs" :key="t.key" role="tab" :aria-selected="current===t.key"
            :class="{on:current===t.key}" @click="current===t.key || $emit('select', t.key)">{{t.label}}</button>
  </div>
</div>
</template>
