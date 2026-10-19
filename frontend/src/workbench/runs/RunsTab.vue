<script setup lang="ts">
import type { Run } from '../api/client'
import AgentLine from '../AgentLine.vue'

defineProps<{ runs: Run[] | null }>()
</script>

<template>
<section v-if="runs === null" class="pl-empty" aria-busy="true">Loading…</section>
<section v-else-if="!runs.length" class="pl-empty">
  <p>No runs yet. A run is a script or workflow working through rows on the server; it starts from a call, an install or a trigger.</p>
  <AgentLine cli="treg run status <run_id>" mcp="run_status { run_id }" />
</section>
<section v-else class="pl-empty">
  <p>{{ runs.length }} recent {{ runs.length === 1 ? 'run' : 'runs' }}. The list and the run page land in the next change.</p>
  <AgentLine cli="treg run status <run_id>" mcp="run_status { run_id }" />
</section>
</template>
