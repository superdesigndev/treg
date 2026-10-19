<script setup lang="ts">
import { computed } from 'vue'
import { useDashboard } from '../../state/context'
import type { TableSummary } from '../api/client'
import AgentLine from '../AgentLine.vue'
import { ago, count } from '../format'

const props = defineProps<{ tables: TableSummary[] | null }>()
const dash = useDashboard()

const own = computed(() => (props.tables || []).filter(t => t.kind !== 'automatic'))
const automatic = computed(() => (props.tables || []).filter(t => t.kind === 'automatic'))
const ICON: Record<string, string> = {
  people: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M22 21v-2a4 4 0 0 0-3-3.9 M16 3.1a4 4 0 0 1 0 7.8',
  companies: 'M3 21h18 M5 21V7l8-4v18 M19 21V11l-6-4 M9 9v.01 M9 12v.01 M9 15v.01 M9 18v.01',
  memory: 'M12 8v4l3 3 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
  automatic: 'M4 7c0-2 4-3 8-3s8 1 8 3-4 3-8 3-8-1-8-3z M4 7v10c0 2 4 3 8 3s8-1 8-3V7',
  other: 'M3 5h18v14H3z M3 10h18 M9 5v14',
}
const TINT: Record<string, string> = { people: 'var(--green)', companies: 'var(--teal)', memory: 'var(--muted)', automatic: 'var(--muted)', other: 'var(--amber)' }
const open = (t: TableSummary, panel: 'sql' | null = null) => dash.goWorkbench({ tab: 'tables', tableId: t.id, panel, cell: null })
</script>

<template>
<section v-if="tables === null" class="pl-empty" aria-busy="true">Loading…</section>
<template v-else>
  <div class="wb-toolbar">
    <p class="cat-hint" style="margin:0">Each row is one call, at the price of the provider that answers. Click a cell to see what filled it.</p>
    <span class="wb-spacer"></span>
    <button v-if="own.length" type="button" class="btn" @click="open(own[0], 'sql')">SQL</button>
  </div>

  <div class="wb-section"><h2>Your tables</h2><span class="wb-count">{{ own.length }}</span><i></i></div>
  <p v-if="!own.length" class="pl-empty">No tables yet. Your agent can create one with a write, or import a CSV.</p>
  <div v-else class="wb-cards">
    <a v-for="t in own" :key="t.id" class="wb-card" :href="'#workbench/tables/' + t.id" :style="{ '--tint': TINT[t.kind] }" @click.prevent="open(t)">
      <span class="wb-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path :d="ICON[t.kind] || ICON.other" /></svg></span>
      <span class="wb-card-text">
        <strong>{{ t.name }}</strong>
        <span class="wb-meta">{{ count(t.row_count, 'row') }} · {{ count(t.column_count, 'col') }} · {{ ago(t.updated_at) }}</span>
        <small v-if="t.status !== 'ready'">{{ t.status }}…</small>
        <small v-else-if="t.bound_by?.length">bound by {{ t.bound_by.join(', ') }}</small>
      </span>
    </a>
  </div>

  <template v-if="automatic.length">
    <div class="wb-section"><h2>Automatic</h2><span class="wb-count">kept by treg · retention {{ automatic[0].retention_days }} days</span><i></i></div>
    <div class="wb-cards">
      <a v-for="t in automatic" :key="t.id" class="wb-card" :href="'#workbench/tables/' + t.id + '/sql'" :style="{ '--tint': TINT.automatic }" @click.prevent="open(t, 'sql')">
        <span class="wb-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path :d="ICON.automatic" /></svg></span>
        <span class="wb-card-text"><strong>{{ t.name }}</strong><span class="wb-meta">{{ count(t.row_count, 'row') }}</span></span>
      </a>
    </div>
  </template>

  <AgentLine cli="treg tables" mcp="tables {}  ·  query { sql }  ·  write { table, rows }" />
</template>
</template>
