<script setup lang="ts">
defineProps<{ align?: 'left' | 'right' | 'center' }>()
</script>

<template>
  <th class="ui-th" :class="align && align !== 'left' ? 'ui-' + align : undefined"><slot /></th>
</template>
