<script setup lang="ts">
// shadcn's TableEmpty: one row spanning the table, for "nothing to show".
defineProps<{ colspan: number }>()
</script>

<template>
  <tr class="ui-tr ui-tr-empty"><td class="ui-td ui-empty" :colspan="colspan"><slot /></td></tr>
</template>
