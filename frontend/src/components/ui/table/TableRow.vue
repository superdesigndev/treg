<script setup lang="ts">
// `selected` is shadcn's data-state="selected"; `interactive` gives the row a pointer and a hover.
defineProps<{ selected?: boolean, interactive?: boolean }>()
</script>

<template>
  <tr class="ui-tr" :class="{ 'ui-tr-interactive': interactive }" :data-state="selected ? 'selected' : undefined"><slot /></tr>
</template>
