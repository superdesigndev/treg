<script setup lang="ts">
defineProps<{ align?: 'left' | 'right' | 'center' }>()
</script>

<template>
  <td class="ui-td" :class="align && align !== 'left' ? 'ui-' + align : undefined"><slot /></td>
</template>
