<script setup lang="ts">
import type { TableSummary } from '../api/client'
import AgentLine from '../AgentLine.vue'

defineProps<{ tables: TableSummary[] | null }>()
</script>

<template>
<section v-if="tables === null" class="pl-empty" aria-busy="true">Loading…</section>
<section v-else-if="!tables.length" class="pl-empty">
  <p>No tables yet. Start one from a search or a CSV, or let your agent write rows into a new table.</p>
  <AgentLine cli="treg tables" mcp="tables {}" />
</section>
<section v-else class="pl-empty">
  <p>{{ tables.length }} {{ tables.length === 1 ? 'table' : 'tables' }}. The table list and the sheet land in the next change.</p>
  <AgentLine cli="treg tables" mcp="tables {}" />
</section>
</template>
