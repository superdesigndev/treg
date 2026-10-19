<script setup lang="ts">
import type { Trigger, UnattendedBudget } from '../api/client'
import AgentLine from '../AgentLine.vue'

defineProps<{ triggers: (Trigger & { install: string })[] | null, unattended: UnattendedBudget | null }>()
</script>

<template>
<section v-if="triggers === null" class="pl-empty" aria-busy="true">Loading…</section>
<section v-else-if="!triggers.length" class="pl-empty">
  <p>No triggers yet. A trigger starts an install on a schedule or from a signed webhook, with a budget for every run.</p>
  <AgentLine cli='treg install <name> --trigger weekly --cron "0 9 * * MON"' mcp="install_save { name, triggers: [...] }" />
</section>
<section v-else class="pl-empty">
  <p>{{ triggers.length }} {{ triggers.length === 1 ? 'trigger' : 'triggers' }} across your installs. The list lands in the next change.</p>
  <AgentLine cli='treg install <name> --trigger weekly --cron "0 9 * * MON"' mcp="install_save { name, triggers: [...] }" />
</section>
</template>
